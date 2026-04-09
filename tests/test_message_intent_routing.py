from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.config import get_settings
from packages.domain.database import SessionLocal
from packages.domain.models import (
    AOIRecord,
    ArtifactRecord,
    DatasetCandidateRecord,
    MessageRecord,
    SessionMemoryEventRecord,
    SessionMemoryLinkRecord,
    SessionMemoryRetrievalCacheRecord,
    SessionMemorySummaryRecord,
    MessageUnderstandingRecord,
    SessionRecord,
    SessionStateSnapshotRecord,
    TaskEventRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskSpecRevisionRecord,
    TaskStepRecord,
    UploadedFileRecord,
)
from packages.domain.services import orchestrator
from packages.domain.services import chat as chat_service
from packages.domain.services import understanding as understanding_service
from packages.domain.services.aoi import AOIRefreshResult
from packages.domain.services.conversation_context import ConversationContextBundle
from packages.domain.services.intent import IntentResult, is_task_confirmation_message
from packages.domain.services.llm_client import LLMResponse, LLMUsage
from packages.domain.services.planner import TaskPlan, TaskPlanStep
from packages.domain.services.response_policy import ResponseDecision
from packages.domain.services.task_state import (
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_WAITING_CLARIFICATION,
)
from packages.domain.services.understanding import EvidenceItem, FieldConfidence, MessageUnderstanding
from packages.schemas.task import ParsedTaskSpec
from packages.domain.utils import make_id


TASK_REQUEST = "帮我分析 2024 年 6 月 bbox(116.1,39.8,116.5,40.1) 的 NDVI，并先生成计划。"
FOLLOWUP_REQUEST = "改成 2023 年 6 月"
UPLOAD_TASK_REQUEST = "请基于我刚上传的边界文件分析 2024 年 6 月 NDVI。"
CLIP_TASK_REQUEST = "请把 /Users/ljn/gis_data/CACD-2020.tif 裁剪到新疆范围，并先给我计划。"


@pytest.fixture(autouse=True)
def _patched_message_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)
    monkeypatch.setattr(get_settings(), "intent_task_confirmation_required", True)
    monkeypatch.setattr(
        orchestrator, "generate_chat_reply", lambda **kwargs: "当然可以，我们先聊聊。"
    )
    monkeypatch.setattr(orchestrator, "normalize_task_aoi", lambda **kwargs: None)

    def _classify_intent(message: str, **kwargs) -> IntentResult:  # noqa: ANN003
        del kwargs
        if is_task_confirmation_message(message):
            return IntentResult(intent="task", confidence=0.99, reason="用户已明确确认执行任务。")
        if "NDVI" in message or "改成" in message or "裁剪" in message:
            return IntentResult(intent="task", confidence=0.95, reason="用户正在发起遥感分析任务。")
        return IntentResult(intent="chat", confidence=0.91, reason="用户正在进行普通对话。")

    def _parse_task_message(message: str, has_upload: bool) -> ParsedTaskSpec:
        if message == TASK_REQUEST:
            assert has_upload is False
            return ParsedTaskSpec(
                aoi_input="bbox(116.1,39.8,116.5,40.1)",
                aoi_source_type="bbox",
                time_range={"start": "2024-06-01", "end": "2024-06-30"},
                requested_dataset="sentinel2",
                analysis_type="NDVI",
                preferred_output=["png_map", "methods_text"],
                user_priority="balanced",
                operation_params={
                    "operations": ["raster.band_math"],
                    "expression": "(nir-red)/(nir+red)",
                },
                need_confirmation=False,
                missing_fields=[],
                clarification_message=None,
                created_from="tests",
            )
        if message == FOLLOWUP_REQUEST:
            assert has_upload is False
            return ParsedTaskSpec(
                aoi_input=None,
                aoi_source_type=None,
                time_range={"start": "2023-06-01", "end": "2023-06-30"},
                requested_dataset=None,
                analysis_type="NDVI",
                preferred_output=["png_map", "methods_text"],
                user_priority="balanced",
                operation_params={
                    "operations": ["raster.band_math"],
                    "expression": "(nir-red)/(nir+red)",
                },
                need_confirmation=True,
                missing_fields=["aoi"],
                clarification_message="请补充 AOI。",
                created_from="tests",
            )
        if message == CLIP_TASK_REQUEST:
            assert has_upload is False
            return ParsedTaskSpec(
                aoi_input=None,
                aoi_source_type=None,
                time_range=None,
                requested_dataset=None,
                analysis_type="NDVI",
                preferred_output=["png_map", "methods_text"],
                user_priority="balanced",
                operation_params={
                    "operations": ["raster.band_math"],
                    "expression": "(nir-red)/(nir+red)",
                },
                need_confirmation=True,
                missing_fields=["aoi", "time_range"],
                clarification_message="请补充 AOI 和时间范围。",
                created_from="tests",
            )
        assert message == UPLOAD_TASK_REQUEST
        assert has_upload is True
        return ParsedTaskSpec(
            aoi_input="uploaded_aoi",
            aoi_source_type="file_upload",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            requested_dataset="sentinel2",
            analysis_type="NDVI",
            preferred_output=["png_map", "methods_text"],
            user_priority="balanced",
            operation_params={
                "operations": ["raster.band_math"],
                "expression": "(nir-red)/(nir+red)",
            },
            need_confirmation=False,
            missing_fields=[],
            clarification_message=None,
            created_from="tests",
        )

    monkeypatch.setattr(orchestrator, "classify_message_intent", _classify_intent)
    monkeypatch.setattr(orchestrator, "parse_task_message", _parse_task_message)
    monkeypatch.setattr(understanding_service, "classify_message_intent", _classify_intent)
    monkeypatch.setattr(
        understanding_service,
        "parse_task_message",
        lambda message, has_upload, **kwargs: _parse_task_message(message, has_upload),  # noqa: ARG005
    )
    monkeypatch.setattr(
        orchestrator,
        "build_task_plan",
        lambda parsed, task_id=None: TaskPlan(  # noqa: ARG005
            version="agent-v2",
            mode="llm_plan_execute_gis_workspace",
            status="ready",
            objective="完成 NDVI 自动处理并发布结果",
            reasoning_summary="按标准 GIS 计划执行。",
            missing_fields=[],
            steps=[
                TaskPlanStep(
                    step_name="plan_task",
                    tool_name="planner.build",
                    title="规划任务",
                    purpose="生成任务计划",
                )
            ],
        ),
    )
    yield


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _cleanup_session(session_id: str) -> None:
    with SessionLocal() as db:
        db.query(SessionMemoryEventRecord).filter(
            SessionMemoryEventRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionMemoryLinkRecord).filter(
            SessionMemoryLinkRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionMemoryRetrievalCacheRecord).filter(
            SessionMemoryRetrievalCacheRecord.session_id == session_id
        ).delete(synchronize_session=False)

        task_ids = [
            task_id
            for (task_id,) in db.query(TaskRunRecord.id)
            .filter(TaskRunRecord.session_id == session_id)
            .all()
        ]

        if task_ids:
            db.query(TaskSpecRevisionRecord).filter(
                TaskSpecRevisionRecord.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
            db.query(MessageUnderstandingRecord).filter(
                MessageUnderstandingRecord.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
            db.query(ArtifactRecord).filter(ArtifactRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskEventRecord).filter(TaskEventRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskStepRecord).filter(TaskStepRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(DatasetCandidateRecord).filter(
                DatasetCandidateRecord.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
            db.query(AOIRecord).filter(AOIRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskSpecRecord).filter(TaskSpecRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskRunRecord).filter(TaskRunRecord.id.in_(task_ids)).delete(
                synchronize_session=False
            )

        db.query(MessageRecord).filter(MessageRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(MessageUnderstandingRecord).filter(
            MessageUnderstandingRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionMemorySummaryRecord).filter(
            SessionMemorySummaryRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionStateSnapshotRecord).filter(
            SessionStateSnapshotRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(
            synchronize_session=False
        )
        db.commit()


def _create_session() -> str:
    with SessionLocal() as db:
        session = orchestrator.create_session(db)
        db.commit()
        return session.session_id


def _create_uploaded_vector_file(
    session_id: str,
    *,
    file_id: str = "file_test_vector",
    created_at: datetime | None = None,
) -> str:
    with SessionLocal() as db:
        db.query(UploadedFileRecord).filter(UploadedFileRecord.id == file_id).delete(
            synchronize_session=False
        )
        record = UploadedFileRecord(
            id=file_id,
            session_id=session_id,
            original_name=f"{file_id}.geojson",
            file_type="geojson",
            storage_key=f".data/uploads/{file_id}.geojson",
            size_bytes=128,
            checksum=f"checksum-{file_id}",
            created_at=created_at,
        )
        db.add(record)
        db.commit()
        return record.id


def _post_message(
    client: TestClient,
    session_id: str,
    content: str,
    *,
    file_ids: list[str] | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/messages",
        json={
            "session_id": session_id,
            "content": content,
            "file_ids": file_ids or [],
        },
    )
    assert response.status_code == 200
    return response.json()


def _context_bundle(session_id: str, message_id: str) -> ConversationContextBundle:
    return ConversationContextBundle(
        session_id=session_id,
        message_id=message_id,
        latest_active_task_id=None,
        latest_active_revision_id=None,
        latest_active_revision_summary=None,
        uploaded_files=[],
        relevant_messages=[],
        explicit_signals={},
        trace={"session_id": session_id, "message_id": message_id},
    )


def _field_confidence(score: float, detail: str) -> FieldConfidence:
    if score < 0.60:
        level = "low"
    elif score < 0.80:
        level = "medium"
    else:
        level = "high"
    return FieldConfidence(
        score=score,
        level=level,
        evidence=[
            EvidenceItem(
                field="analysis_type",
                source="current_message",
                weight=score,
                detail=detail,
            )
        ],
    )


def _understanding(
    *,
    intent: str,
    summary: str,
    parsed_spec: ParsedTaskSpec | None,
    field_scores: dict[str, float],
) -> MessageUnderstanding:
    return MessageUnderstanding(
        intent=intent,  # type: ignore[arg-type]
        intent_confidence=0.91,
        understanding_summary=summary,
        parsed_spec=parsed_spec,
        parsed_candidates=parsed_spec.model_dump() if parsed_spec is not None else {},
        field_confidences={
            field: _field_confidence(score, f"{field}:{score}")
            for field, score in field_scores.items()
        },
        ranked_candidates={},
        trace={"source": "test"},
    )


def test_chat_message_returns_chat_mode_and_persists_assistant_message(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _post_message(client, session_id, "你好，今天可以先随便聊聊吗？")

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["assistant_message"] == "当然可以，我们先聊聊。"
        assert payload["awaiting_task_confirmation"] is False

        with SessionLocal() as db:
            messages = db.query(MessageRecord).filter(MessageRecord.session_id == session_id).all()
            assistant_messages = [item for item in messages if item.role == "assistant"]
            assert len(assistant_messages) == 1
            assert assistant_messages[0].content == "当然可以，我们先聊聊。"
    finally:
        _cleanup_session(session_id)


def test_chat_message_includes_understanding_payload_and_persists_message_understanding(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    understanding = _understanding(
        intent="chat",
        summary="用户在普通聊天。",
        parsed_spec=None,
        field_scores={
            "aoi_input": 0.0,
            "aoi_source_type": 0.0,
            "time_range": 0.0,
            "analysis_type": 0.0,
        },
    )
    decision = ResponseDecision(
        mode="chat_reply",
        assistant_message="当然，我们可以继续聊这个。",
        editable_fields=[],
        response_payload={
            "response_mode": "chat_reply",
            "requires_execution": False,
            "execution_blocked": False,
        },
        execution_blocked=False,
        requires_task_creation=False,
        requires_revision_creation=False,
        requires_execution=False,
    )
    captured_message_id: str | None = None

    def _fake_context_builder(db, *, session_id: str, message_id: str, message_content: str, preferred_file_ids=None):  # noqa: ANN001, ARG001
        nonlocal captured_message_id
        captured_message_id = message_id
        return _context_bundle(session_id, message_id)

    monkeypatch.setattr(orchestrator, "build_conversation_context", _fake_context_builder, raising=False)
    monkeypatch.setattr(orchestrator, "understand_message", lambda *args, **kwargs: understanding, raising=False)
    monkeypatch.setattr(orchestrator, "decide_response", lambda *args, **kwargs: decision, raising=False)

    try:
        payload = _post_message(client, session_id, "你好，今天可以先随便聊聊吗？")

        assert payload["mode"] == "chat"
        assert payload["response_mode"] == "chat_reply"
        assert payload["understanding"]["intent"] == "chat"
        assert payload["understanding"]["understanding_summary"] == "用户在普通聊天。"
        assert payload["response_payload"]["requires_execution"] is False

        with SessionLocal() as db:
            row = (
                db.query(MessageUnderstandingRecord)
                .filter(MessageUnderstandingRecord.message_id == captured_message_id)
                .one_or_none()
            )
            assert row is not None
            assert row.response_mode == "chat_reply"
            assert row.intent == "chat"
    finally:
        _cleanup_session(session_id)


def test_medium_confidence_task_like_message_returns_confirm_understanding_without_creating_task(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    understanding = _understanding(
        intent="new_task",
        summary="用户想分析江西 2024 年 6 月 NDVI，但 AOI 来源还需要确认。",
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="NDVI",
        ),
        field_scores={
            "aoi_input": 0.88,
            "aoi_source_type": 0.72,
            "time_range": 0.91,
            "analysis_type": 0.93,
        },
    )
    decision = ResponseDecision(
        mode="confirm_understanding",
        assistant_message="我理解的是：分析江西 2024 年 6 月 NDVI。如果没问题，我就继续。",
        editable_fields=["aoi_source_type"],
        response_payload={
            "response_mode": "confirm_understanding",
            "editable_fields": ["aoi_source_type"],
            "requires_execution": False,
            "execution_blocked": False,
        },
        execution_blocked=False,
        requires_task_creation=True,
        requires_revision_creation=False,
        requires_execution=False,
    )

    monkeypatch.setattr(
        orchestrator,
        "build_conversation_context",
        lambda db, *, session_id, message_id, message_content, preferred_file_ids=None: _context_bundle(session_id, message_id),  # noqa: ANN001, ARG005
        raising=False,
    )
    monkeypatch.setattr(orchestrator, "understand_message", lambda *args, **kwargs: understanding, raising=False)
    monkeypatch.setattr(orchestrator, "decide_response", lambda *args, **kwargs: decision, raising=False)

    try:
        payload = _post_message(client, session_id, TASK_REQUEST)

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["awaiting_task_confirmation"] is False
        assert payload["response_mode"] == "confirm_understanding"
        assert payload["assistant_message"] == decision.assistant_message
        assert payload["understanding"]["intent"] == "new_task"
        assert payload["response_payload"]["editable_fields"] == ["aoi_source_type"]

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
            )
    finally:
        _cleanup_session(session_id)


def test_execute_now_task_response_carries_understanding_metadata(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "intent_task_confirmation_required", False)
    session_id = _create_session()
    understanding = _understanding(
        intent="new_task",
        summary="用户请求执行江西 2024 年 6 月 NDVI 分析。",
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="NDVI",
        ),
        field_scores={
            "aoi_input": 0.92,
            "aoi_source_type": 0.91,
            "time_range": 0.94,
            "analysis_type": 0.95,
        },
    )
    decision = ResponseDecision(
        mode="execute_now",
        assistant_message="关键信息齐了，我会直接开始执行。",
        editable_fields=[],
        response_payload={
            "response_mode": "execute_now",
            "requires_execution": True,
            "execution_blocked": False,
        },
        execution_blocked=False,
        requires_task_creation=True,
        requires_revision_creation=False,
        requires_execution=True,
    )

    monkeypatch.setattr(
        orchestrator,
        "build_conversation_context",
        lambda db, *, session_id, message_id, message_content, preferred_file_ids=None: _context_bundle(session_id, message_id),  # noqa: ANN001, ARG005
        raising=False,
    )
    monkeypatch.setattr(orchestrator, "understand_message", lambda *args, **kwargs: understanding, raising=False)
    monkeypatch.setattr(orchestrator, "decide_response", lambda *args, **kwargs: decision, raising=False)

    try:
        payload = _post_message(client, session_id, TASK_REQUEST)

        assert payload["mode"] == "task"
        assert payload["task_id"]
        assert payload["response_mode"] == "execute_now"
        assert payload["understanding"]["intent"] == "new_task"
        assert payload["response_payload"]["requires_execution"] is True

        with SessionLocal() as db:
            row = (
                db.query(MessageUnderstandingRecord)
                .filter(MessageUnderstandingRecord.message_id == payload["message_id"])
                .one_or_none()
            )
            assert row is not None
            assert row.task_id == payload["task_id"]
            assert row.response_mode == "execute_now"
    finally:
        _cleanup_session(session_id)


def test_second_chat_turn_replays_prior_assistant_message_into_generation_history(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    monkeypatch.setattr(orchestrator, "generate_chat_reply", chat_service.generate_chat_reply)

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        user_prompt = kwargs["user_prompt"]
        if "history-initial" in user_prompt:
            reply = "history-replayed"
        else:
            reply = "history-initial"
        return LLMResponse(
            model="fake-chat-model",
            request_id="req-test",
            content_text=f'{{"reply":"{reply}"}}',
            content_json={"reply": reply},
            usage=LLMUsage(),
            latency_ms=0,
            raw_payload={},
        )

    monkeypatch.setattr("packages.domain.services.chat.LLMClient.chat_json", _fake_chat_json)

    try:
        first_payload = _post_message(client, session_id, "你好，我们先随便聊聊。")
        assert first_payload["assistant_message"] == "history-initial"

        second_payload = _post_message(client, session_id, "那再聊一点别的吧。")
        assert second_payload["assistant_message"] == "history-replayed"
    finally:
        _cleanup_session(session_id)


def test_high_confidence_task_request_prompts_confirmation_without_creating_task(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        payload = _post_message(client, session_id, TASK_REQUEST)

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["intent"] == "task"
        assert payload["intent_confidence"] == 0.95
        assert payload["awaiting_task_confirmation"] is True
        assert payload["assistant_message"]

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
            )
    finally:
        _cleanup_session(session_id)


def test_high_confidence_task_request_executes_directly_when_confirmation_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "intent_task_confirmation_required", False)
    session_id = _create_session()
    try:
        payload = _post_message(client, session_id, TASK_REQUEST)

        assert payload["mode"] == "task"
        assert payload["task_id"]
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert payload["awaiting_task_confirmation"] is False
        assert payload["intent"] == "task"
        assert payload["intent_confidence"] == 0.95

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 1
            )
    finally:
        _cleanup_session(session_id)


def test_chat_with_uploaded_files_keeps_chat_mode_until_user_confirms_execution(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        file_id = _create_uploaded_vector_file(session_id)
        payload = _post_message(
            client,
            session_id,
            "你能读到我上传的文件吗？",
            file_ids=[file_id],
        )

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["awaiting_task_confirmation"] is False
        assistant_message = str(payload["assistant_message"] or "")
        assert assistant_message
        assert "可以" in assistant_message
        assert "file_test_vector.geojson" in assistant_message

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
            )
    finally:
        _cleanup_session(session_id)


def test_message_understanding_trace_contains_retrieval_context_builder_section(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        file_id = _create_uploaded_vector_file(session_id, file_id=make_id("file"))
        payload = _post_message(
            client,
            session_id,
            "你能读到我上传的文件吗？",
            file_ids=[file_id],
        )

        with SessionLocal() as db:
            row = (
                db.query(MessageUnderstandingRecord)
                .filter(MessageUnderstandingRecord.message_id == payload["message_id"])
                .one_or_none()
            )
            assert row is not None
            builder_trace = dict(row.context_trace_json or {}).get("context_builder", {})
            assert "retrieval" in builder_trace
            assert file_id in builder_trace["retrieval"].get("selected_upload_ids", [])
    finally:
        _cleanup_session(session_id)


def test_message_rejects_file_ids_from_other_session(client: TestClient) -> None:
    session_id = _create_session()
    other_session_id = _create_session()
    try:
        other_file_id = _create_uploaded_vector_file(other_session_id, file_id="file_other_session")
        response = client.post(
            "/api/v1/messages",
            json={
                "session_id": session_id,
                "content": "你能读到我上传的文件吗？",
                "file_ids": [other_file_id],
            },
        )
        assert response.status_code == 400
        payload = response.json()
        assert payload["error"]["code"] == "bad_request"
        assert other_file_id in payload["error"]["detail"]["file_ids"]
    finally:
        _cleanup_session(session_id)
        _cleanup_session(other_session_id)


def test_explicit_confirmation_uses_latest_request_and_creates_task(client: TestClient) -> None:
    session_id = _create_session()
    try:
        first_payload = _post_message(client, session_id, TASK_REQUEST)
        assert first_payload["awaiting_task_confirmation"] is True

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "task"
        assert payload["task_id"]
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert payload["need_approval"] is True
        assert payload["awaiting_task_confirmation"] is False
        assert payload["intent"] == "task"
        assert payload["intent_confidence"] == 0.99
        assert payload["response_mode"] == "execute_now"
        assert payload["understanding"]["intent"] == "task_confirmation"
        assert payload["response_payload"]["require_approval"] is True

        with SessionLocal() as db:
            row = (
                db.query(MessageUnderstandingRecord)
                .filter(MessageUnderstandingRecord.message_id == payload["message_id"])
                .one_or_none()
            )
            assert row is not None
            assert row.task_id == payload["task_id"]
            assert row.intent == "task_confirmation"
            assert row.response_mode == "execute_now"
    finally:
        _cleanup_session(session_id)


def test_incomplete_request_surfaces_missing_fields_without_legacy_confirmation_prompt(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        first_payload = _post_message(client, session_id, CLIP_TASK_REQUEST)
        assert first_payload["mode"] == "chat"
        assert first_payload["task_id"] is None
        assert first_payload["awaiting_task_confirmation"] is False
        assert first_payload["response_mode"] == "ask_missing_fields"
        assert first_payload["response_payload"]["missing_fields"] == [
            "aoi_input",
            "aoi_source_type",
            "time_range",
        ]

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["awaiting_task_confirmation"] is False
    finally:
        _cleanup_session(session_id)


def test_confirmation_after_confirm_understanding_creates_task_and_persists_metadata(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    understanding = _understanding(
        intent="new_task",
        summary="用户想分析江西 2024 年 6 月 NDVI，但 AOI 来源还需要确认。",
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="NDVI",
        ),
        field_scores={
            "aoi_input": 0.88,
            "aoi_source_type": 0.72,
            "time_range": 0.91,
            "analysis_type": 0.93,
        },
    )
    decision = ResponseDecision(
        mode="confirm_understanding",
        assistant_message="我理解的是：分析江西 2024 年 6 月 NDVI。如果没问题，我就继续。",
        editable_fields=["aoi_source_type"],
        response_payload={
            "response_mode": "confirm_understanding",
            "editable_fields": ["aoi_source_type"],
            "requires_execution": False,
            "execution_blocked": False,
        },
        execution_blocked=False,
        requires_task_creation=True,
        requires_revision_creation=False,
        requires_execution=False,
    )

    def _fake_context_builder(db, *, session_id: str, message_id: str, message_content: str, preferred_file_ids=None):  # noqa: ANN001, ARG001
        return _context_bundle(session_id, message_id)

    def _fake_understand_message(message: str, **kwargs):  # noqa: ANN001, ARG001
        if message == TASK_REQUEST:
            return understanding
        raise AssertionError("confirmation branch should not re-enter understand_message")

    def _fake_decide_response(understanding_obj, **kwargs):  # noqa: ANN001, ARG001
        if understanding_obj is understanding:
            return decision
        raise AssertionError("confirmation branch should synthesize its own response metadata")

    monkeypatch.setattr(orchestrator, "build_conversation_context", _fake_context_builder, raising=False)
    monkeypatch.setattr(orchestrator, "understand_message", _fake_understand_message, raising=False)
    monkeypatch.setattr(orchestrator, "decide_response", _fake_decide_response, raising=False)

    try:
        prompt_payload = _post_message(client, session_id, TASK_REQUEST)
        assert prompt_payload["mode"] == "chat"
        assert prompt_payload["response_mode"] == "confirm_understanding"

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "task"
        assert payload["task_id"]
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert payload["response_mode"] == "execute_now"
        assert payload["understanding"]["intent"] == "task_confirmation"
        assert payload["response_payload"]["require_approval"] is True

        with SessionLocal() as db:
            row = (
                db.query(MessageUnderstandingRecord)
                .filter(MessageUnderstandingRecord.message_id == payload["message_id"])
                .one_or_none()
            )
            assert row is not None
            assert row.task_id == payload["task_id"]
            assert row.intent == "task_confirmation"
            assert row.response_mode == "execute_now"
    finally:
        _cleanup_session(session_id)


def test_delayed_confirmation_after_confirm_understanding_and_chat_does_not_execute_old_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    understanding = _understanding(
        intent="new_task",
        summary="用户想分析江西 2024 年 6 月 NDVI，但 AOI 来源还需要确认。",
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="NDVI",
        ),
        field_scores={
            "aoi_input": 0.88,
            "aoi_source_type": 0.72,
            "time_range": 0.91,
            "analysis_type": 0.93,
        },
    )
    decision = ResponseDecision(
        mode="confirm_understanding",
        assistant_message="我理解的是：分析江西 2024 年 6 月 NDVI。如果没问题，我就继续。",
        editable_fields=["aoi_source_type"],
        response_payload={
            "response_mode": "confirm_understanding",
            "editable_fields": ["aoi_source_type"],
            "requires_execution": False,
            "execution_blocked": False,
        },
        execution_blocked=False,
        requires_task_creation=True,
        requires_revision_creation=False,
        requires_execution=False,
    )
    chat_understanding = _understanding(
        intent="chat",
        summary="用户在普通聊天。",
        parsed_spec=None,
        field_scores={
            "aoi_input": 0.0,
            "aoi_source_type": 0.0,
            "time_range": 0.0,
            "analysis_type": 0.0,
        },
    )
    chat_decision = ResponseDecision(
        mode="chat_reply",
        assistant_message="当然，我们可以继续聊这个。",
        editable_fields=[],
        response_payload={
            "response_mode": "chat_reply",
            "requires_execution": False,
            "execution_blocked": False,
        },
        execution_blocked=False,
        requires_task_creation=False,
        requires_revision_creation=False,
        requires_execution=False,
    )

    def _fake_context_builder(db, *, session_id: str, message_id: str, message_content: str, preferred_file_ids=None):  # noqa: ANN001, ARG001
        return _context_bundle(session_id, message_id)

    def _fake_understand_message(message: str, **kwargs):  # noqa: ANN001, ARG001
        if message == TASK_REQUEST:
            return understanding
        return chat_understanding

    def _fake_decide_response(understanding_obj, **kwargs):  # noqa: ANN001, ARG001
        if understanding_obj is understanding:
            return decision
        if understanding_obj is chat_understanding:
            return chat_decision
        raise AssertionError("unexpected understanding object")

    monkeypatch.setattr(orchestrator, "build_conversation_context", _fake_context_builder, raising=False)
    monkeypatch.setattr(orchestrator, "understand_message", _fake_understand_message, raising=False)
    monkeypatch.setattr(orchestrator, "decide_response", _fake_decide_response, raising=False)

    try:
        prompt_payload = _post_message(client, session_id, TASK_REQUEST)
        assert prompt_payload["response_mode"] == "confirm_understanding"

        chat_payload = _post_message(client, session_id, "今天先不做任务，聊聊别的。")
        assert chat_payload["mode"] == "chat"
        assert chat_payload["response_mode"] == "chat_reply"

        delayed_confirmation_payload = _post_message(client, session_id, "好的，继续")

        assert delayed_confirmation_payload["mode"] == "chat"
        assert delayed_confirmation_payload["task_id"] is None
        assert delayed_confirmation_payload["awaiting_task_confirmation"] is False

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
            )
    finally:
        _cleanup_session(session_id)


def test_task_correction_message_creates_and_activates_new_revision(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    try:
        prompt_payload = _post_message(client, session_id, TASK_REQUEST)
        assert prompt_payload["awaiting_task_confirmation"] is True

        initial_task_payload = _post_message(client, session_id, "好的，继续")
        task_id = str(initial_task_payload["task_id"])
        assert task_id

        with SessionLocal() as db:
            initial_task = db.get(TaskRunRecord, task_id)
            assert initial_task is not None
            initial_revision = next(revision for revision in initial_task.revisions if revision.is_active)
            initial_revision_id = initial_revision.id

        correction_understanding = _understanding(
            intent="task_correction",
            summary="把时间范围改成 2023 年 6 月，其他条件保持不变。",
            parsed_spec=ParsedTaskSpec(
                time_range={"start": "2023-06-01", "end": "2023-06-30"},
                analysis_type="NDVI",
            ),
            field_scores={
                "aoi_input": 0.93,
                "aoi_source_type": 0.91,
                "time_range": 0.96,
                "analysis_type": 0.95,
            },
        )
        correction_decision = ResponseDecision(
            mode="show_revision",
            assistant_message="这是我当前的理解：把时间范围改成 2023 年 6 月，其他条件保持不变。你可以直接修改。",
            editable_fields=["time_range"],
            response_payload={
                "response_mode": "show_revision",
                "requires_revision_creation": True,
                "requires_execution": False,
                "execution_blocked": False,
            },
            execution_blocked=False,
            requires_task_creation=False,
            requires_revision_creation=True,
            requires_execution=False,
        )

        original_understand_message = orchestrator.understand_message
        original_decide_response = orchestrator.decide_response
        original_context_builder = orchestrator.build_conversation_context

        def _patched_context_builder(db, *, session_id: str, message_id: str, message_content: str, preferred_file_ids=None):  # noqa: ANN001, ARG001
            if message_content == FOLLOWUP_REQUEST:
                bundle = _context_bundle(session_id, message_id)
                bundle.latest_active_task_id = task_id
                bundle.latest_active_revision_id = initial_revision_id
                bundle.latest_active_revision_summary = "初始版本"
                bundle.explicit_signals = {
                    "confirmation_hint": False,
                    "correction_hint": True,
                    "revision_field_overlap": {
                        "revision_id": initial_revision_id,
                        "fields": ["time_range"],
                        "matched_signals": ["correction_hint"],
                    },
                }
                return bundle
            return original_context_builder(
                db,
                session_id=session_id,
                message_id=message_id,
                message_content=message_content,
                preferred_file_ids=preferred_file_ids,
            )

        def _patched_understand_message(message: str, *args, **kwargs):  # noqa: ANN002, ANN003
            if message == FOLLOWUP_REQUEST:
                return correction_understanding
            return original_understand_message(message, *args, **kwargs)

        def _patched_decide_response(understanding_obj, *args, **kwargs):  # noqa: ANN002, ANN003
            if understanding_obj is correction_understanding:
                return correction_decision
            return original_decide_response(understanding_obj, *args, **kwargs)

        monkeypatch.setattr(orchestrator, "build_conversation_context", _patched_context_builder, raising=False)
        monkeypatch.setattr(orchestrator, "understand_message", _patched_understand_message, raising=False)
        monkeypatch.setattr(orchestrator, "decide_response", _patched_decide_response, raising=False)

        payload = _post_message(client, session_id, FOLLOWUP_REQUEST)

        assert payload["mode"] == "chat"
        assert payload["task_id"] == task_id
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert payload["response_mode"] == "show_revision"
        assert payload["understanding"]["intent"] == "task_correction"

        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            assert task is not None
            active_revision = next(revision for revision in task.revisions if revision.is_active)
            assert active_revision.id != initial_revision_id
            assert active_revision.revision_number == 2
            assert active_revision.base_revision_id == initial_revision_id
            assert active_revision.raw_spec_json["time_range"] == {
                "start": "2023-06-01",
                "end": "2023-06-30",
            }
            assert task.task_spec is not None
            assert task.task_spec.raw_spec_json["time_range"] == {
                "start": "2023-06-01",
                "end": "2023-06-30",
            }
            assert task.last_response_mode == "show_revision"

            row = (
                db.query(MessageUnderstandingRecord)
                .filter(MessageUnderstandingRecord.message_id == payload["message_id"])
                .one_or_none()
            )
            assert row is not None
            assert row.task_id == task_id
            assert row.derived_revision_id == active_revision.id
            assert row.intent == "task_correction"
            assert row.response_mode == "show_revision"
    finally:
        _cleanup_session(session_id)


def test_blocked_task_correction_clears_execution_blocked_and_activates_revision(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()
    try:
        prompt_payload = _post_message(client, session_id, TASK_REQUEST)
        assert prompt_payload["awaiting_task_confirmation"] is True

        initial_task_payload = _post_message(client, session_id, "好的，继续")
        task_id = str(initial_task_payload["task_id"])
        assert task_id

        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            assert task is not None
            active_revision = next(revision for revision in task.revisions if revision.is_active)
            active_revision.execution_blocked = True
            active_revision.execution_blocked_reason = "AOI normalization failed: test blocked."
            active_revision.response_mode = "ask_missing_fields"
            task.status = TASK_STATUS_WAITING_CLARIFICATION
            task.current_step = "normalize_aoi"
            task.interaction_state = "execution_blocked"
            task.last_response_mode = "ask_missing_fields"
            if task.task_spec is not None:
                task.task_spec.need_confirmation = True
                task.task_spec.raw_spec_json = {
                    **(task.task_spec.raw_spec_json or {}),
                    "need_confirmation": True,
                    "missing_fields": ["aoi_boundary"],
                    "clarification_message": "AOI normalization failed: test blocked.",
                }
            blocked_revision_id = active_revision.id
            db.commit()

        correction_understanding = _understanding(
            intent="task_correction",
            summary="把 AOI 改成江西，其他条件保持不变。",
            parsed_spec=ParsedTaskSpec(
                aoi_input="江西",
                aoi_source_type="admin_name",
                analysis_type="NDVI",
            ),
            field_scores={
                "aoi_input": 0.94,
                "aoi_source_type": 0.93,
                "time_range": 0.91,
                "analysis_type": 0.95,
            },
        )
        correction_decision = ResponseDecision(
            mode="show_revision",
            assistant_message="这是我当前的理解：把 AOI 改成江西，其他条件保持不变。你可以直接修改。",
            editable_fields=["aoi_input", "aoi_source_type"],
            response_payload={
                "response_mode": "show_revision",
                "requires_revision_creation": True,
                "requires_execution": False,
                "execution_blocked": False,
            },
            execution_blocked=False,
            requires_task_creation=False,
            requires_revision_creation=True,
            requires_execution=False,
        )

        original_context_builder = orchestrator.build_conversation_context
        original_understand_message = orchestrator.understand_message
        original_decide_response = orchestrator.decide_response

        def _patched_context_builder(db, *, session_id: str, message_id: str, message_content: str, preferred_file_ids=None):  # noqa: ANN001, ARG001
            if message_content == "把 AOI 改成江西":
                bundle = _context_bundle(session_id, message_id)
                bundle.latest_active_task_id = task_id
                bundle.latest_active_revision_id = blocked_revision_id
                bundle.latest_active_revision_summary = "阻塞版本"
                bundle.explicit_signals = {
                    "confirmation_hint": False,
                    "correction_hint": True,
                    "revision_field_overlap": {
                        "revision_id": blocked_revision_id,
                        "fields": ["aoi_input", "aoi_source_type"],
                        "matched_signals": ["correction_hint"],
                    },
                }
                return bundle
            return original_context_builder(
                db,
                session_id=session_id,
                message_id=message_id,
                message_content=message_content,
                preferred_file_ids=preferred_file_ids,
            )

        def _patched_understand_message(message: str, *args, **kwargs):  # noqa: ANN002, ANN003
            if message == "把 AOI 改成江西":
                return correction_understanding
            return original_understand_message(message, *args, **kwargs)

        def _patched_decide_response(understanding_obj, *args, **kwargs):  # noqa: ANN002, ANN003
            if understanding_obj is correction_understanding:
                return correction_decision
            return original_decide_response(understanding_obj, *args, **kwargs)

        def _patched_normalize_active_revision_aoi(db, *, task, revision, uploaded_files=None):  # noqa: ANN001, ARG001
            revision.execution_blocked = False
            revision.execution_blocked_reason = None
            return AOIRefreshResult(execution_blocked=False, blocked_reason=None, aoi_record=None)

        monkeypatch.setattr(orchestrator, "build_conversation_context", _patched_context_builder, raising=False)
        monkeypatch.setattr(orchestrator, "understand_message", _patched_understand_message, raising=False)
        monkeypatch.setattr(orchestrator, "decide_response", _patched_decide_response, raising=False)
        monkeypatch.setattr(
            orchestrator,
            "normalize_active_revision_aoi",
            _patched_normalize_active_revision_aoi,
            raising=False,
        )

        payload = _post_message(client, session_id, "把 AOI 改成江西")

        assert payload["mode"] == "chat"
        assert payload["task_id"] == task_id
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert payload["response_mode"] == "show_revision"

        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            assert task is not None
            active_revision = next(revision for revision in task.revisions if revision.is_active)
            assert active_revision.id != blocked_revision_id
            assert active_revision.revision_number == 2
            assert active_revision.execution_blocked is False
            assert task.status == TASK_STATUS_AWAITING_APPROVAL
            assert task.interaction_state == "understanding"
            assert task.last_response_mode == "show_revision"
            assert task.task_spec is not None
            assert task.task_spec.raw_spec_json["aoi_input"] == "江西"
            assert task.task_spec.raw_spec_json["aoi_source_type"] == "admin_name"
    finally:
        _cleanup_session(session_id)


def test_confirmation_without_prior_context_returns_chat_guidance(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["awaiting_task_confirmation"] is False
        assert payload["assistant_message"]

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
            )
            assistant_messages = (
                db.query(MessageRecord)
                .filter(MessageRecord.session_id == session_id, MessageRecord.role == "assistant")
                .all()
            )
            assert len(assistant_messages) == 1
    finally:
        _cleanup_session(session_id)


def test_stale_confirmation_after_successful_confirmation_does_not_create_second_task(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        first_payload = _post_message(client, session_id, TASK_REQUEST)
        assert first_payload["awaiting_task_confirmation"] is True

        task_payload = _post_message(client, session_id, "好的，继续")
        assert task_payload["mode"] == "task"
        assert task_payload["task_id"]

        stale_confirmation_payload = _post_message(client, session_id, "好的，继续")

        assert stale_confirmation_payload["mode"] == "chat"
        assert stale_confirmation_payload["task_id"] is None

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 1
            )
            source_message = (
                db.query(MessageRecord)
                .filter(
                    MessageRecord.session_id == session_id, MessageRecord.content == TASK_REQUEST
                )
                .one()
            )
            assert source_message.linked_task_id == task_payload["task_id"]
    finally:
        _cleanup_session(session_id)


def test_confirmation_reuses_recent_session_uploads_when_current_file_ids_are_empty(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        file_id = _create_uploaded_vector_file(session_id)
        prompt_payload = _post_message(
            client,
            session_id,
            UPLOAD_TASK_REQUEST,
            file_ids=[file_id],
        )
        assert prompt_payload["awaiting_task_confirmation"] is True

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "task"
        assert payload["task_id"]

        with SessionLocal() as db:
            task_spec = db.get(TaskSpecRecord, payload["task_id"])
            assert task_spec is not None
            assert task_spec.raw_spec_json["upload_slots"]["input_vector_file_id"] == file_id
    finally:
        _cleanup_session(session_id)


def test_followup_style_request_uses_revision_guidance_instead_of_legacy_confirmation(client: TestClient) -> None:
    session_id = _create_session()
    try:
        first_prompt = _post_message(client, session_id, TASK_REQUEST)
        assert first_prompt["awaiting_task_confirmation"] is True

        parent_payload = _post_message(client, session_id, "好的，继续")
        assert parent_payload["mode"] == "task"
        parent_task_id = parent_payload["task_id"]
        assert parent_task_id

        followup_prompt = _post_message(client, session_id, FOLLOWUP_REQUEST)
        assert followup_prompt["mode"] == "chat"
        assert followup_prompt["awaiting_task_confirmation"] is False
        assert followup_prompt["task_id"] == parent_task_id
        assert followup_prompt["response_mode"] == "ask_missing_fields"

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "chat"
        assert payload["task_id"] is None
        assert payload["awaiting_task_confirmation"] is False

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 1
            )
    finally:
        _cleanup_session(session_id)


def test_create_message_uses_single_shot_task_flow_when_intent_router_is_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "intent_router_enabled", False)
    session_id = _create_session()
    try:
        payload = _post_message(client, session_id, TASK_REQUEST)

        assert payload["mode"] == "task"
        assert payload["task_id"]
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert payload["awaiting_task_confirmation"] is False

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 1
            )
    finally:
        _cleanup_session(session_id)


def test_delayed_confirmation_after_unrelated_chat_does_not_execute_old_request(
    client: TestClient,
) -> None:
    session_id = _create_session()
    try:
        prompt_payload = _post_message(client, session_id, TASK_REQUEST)
        assert prompt_payload["awaiting_task_confirmation"] is True

        chat_payload = _post_message(client, session_id, "今天先不做任务，聊聊别的。")
        assert chat_payload["mode"] == "chat"
        assert chat_payload["awaiting_task_confirmation"] is False

        delayed_confirmation_payload = _post_message(client, session_id, "好的，继续")

        assert delayed_confirmation_payload["mode"] == "chat"
        assert delayed_confirmation_payload["task_id"] is None
        assert delayed_confirmation_payload["awaiting_task_confirmation"] is False

        with SessionLocal() as db:
            assert (
                db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
            )
    finally:
        _cleanup_session(session_id)


def test_confirmation_fallback_prefers_newest_uploaded_file_deterministically(
    client: TestClient,
) -> None:
    session_id = _create_session()
    base_time = datetime.now(timezone.utc)
    try:
        old_file_id = _create_uploaded_vector_file(
            session_id,
            file_id="file_old_vector",
            created_at=base_time - timedelta(minutes=2),
        )
        new_file_id = _create_uploaded_vector_file(
            session_id,
            file_id="file_new_vector",
            created_at=base_time,
        )

        prompt_payload = _post_message(
            client,
            session_id,
            UPLOAD_TASK_REQUEST,
            file_ids=[new_file_id],
        )
        assert prompt_payload["awaiting_task_confirmation"] is True

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "task"
        assert payload["task_id"]

        with SessionLocal() as db:
            task_spec = db.get(TaskSpecRecord, payload["task_id"])
            assert task_spec is not None
            assert task_spec.raw_spec_json["upload_slots"]["input_vector_file_id"] == new_file_id
            assert task_spec.raw_spec_json["upload_slots"]["input_vector_file_id"] != old_file_id
    finally:
        _cleanup_session(session_id)
