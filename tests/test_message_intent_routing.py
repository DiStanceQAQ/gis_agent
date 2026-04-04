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
    SessionRecord,
    TaskEventRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskStepRecord,
    UploadedFileRecord,
)
from packages.domain.services import orchestrator
from packages.domain.services.intent import IntentResult, is_task_confirmation_message
from packages.domain.services.planner import TaskPlan, TaskPlanStep
from packages.domain.services.task_state import TASK_STATUS_AWAITING_APPROVAL
from packages.schemas.task import ParsedTaskSpec


TASK_REQUEST = "帮我分析 2024 年 6 月 bbox(116.1,39.8,116.5,40.1) 的 NDVI，并先生成计划。"
FOLLOWUP_REQUEST = "改成 2023 年 6 月"
UPLOAD_TASK_REQUEST = "请基于我刚上传的边界文件分析 2024 年 6 月 NDVI。"


@pytest.fixture(autouse=True)
def _patched_message_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)
    monkeypatch.setattr(orchestrator, "generate_chat_reply", lambda **kwargs: "当然可以，我们先聊聊。")
    monkeypatch.setattr(orchestrator, "normalize_task_aoi", lambda **kwargs: None)

    def _classify_intent(message: str, **kwargs) -> IntentResult:  # noqa: ANN003
        del kwargs
        if is_task_confirmation_message(message):
            return IntentResult(intent="task", confidence=0.99, reason="用户已明确确认执行任务。")
        if "NDVI" in message or "改成" in message:
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
                operation_params={},
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
                operation_params={},
                need_confirmation=True,
                missing_fields=["aoi"],
                clarification_message="请补充 AOI。",
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
            operation_params={},
            need_confirmation=False,
            missing_fields=[],
            clarification_message=None,
            created_from="tests",
        )

    monkeypatch.setattr(orchestrator, "classify_message_intent", _classify_intent)
    monkeypatch.setattr(orchestrator, "parse_task_message", _parse_task_message)
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
        task_ids = [
            task_id
            for (task_id,) in db.query(TaskRunRecord.id).filter(TaskRunRecord.session_id == session_id).all()
        ]

        if task_ids:
            db.query(ArtifactRecord).filter(ArtifactRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskEventRecord).filter(TaskEventRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskStepRecord).filter(TaskStepRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(DatasetCandidateRecord).filter(DatasetCandidateRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(AOIRecord).filter(AOIRecord.task_id.in_(task_ids)).delete(synchronize_session=False)
            db.query(TaskSpecRecord).filter(TaskSpecRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskRunRecord).filter(TaskRunRecord.id.in_(task_ids)).delete(synchronize_session=False)

        db.query(MessageRecord).filter(MessageRecord.session_id == session_id).delete(synchronize_session=False)
        db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
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
        json={"session_id": session_id, "content": content, "file_ids": file_ids or []},
    )
    assert response.status_code == 200
    return response.json()


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


def test_second_chat_turn_replays_prior_assistant_message_into_generation_history(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session()

    def _generate_chat_reply(*, user_message: str, history: list[dict[str, str]], db_session: object) -> str:  # noqa: ARG001
        del db_session
        has_prior_assistant = any(item.get("role") == "assistant" for item in history)
        return "history-replayed" if has_prior_assistant else "history-initial"

    monkeypatch.setattr(orchestrator, "generate_chat_reply", _generate_chat_reply)

    try:
        first_payload = _post_message(client, session_id, "你好，我们先随便聊聊。")
        assert first_payload["assistant_message"] == "history-initial"

        second_payload = _post_message(client, session_id, "那再聊一点别的吧。")
        assert second_payload["assistant_message"] == "history-replayed"
    finally:
        _cleanup_session(session_id)


def test_high_confidence_task_request_prompts_confirmation_without_creating_task(client: TestClient) -> None:
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
            assert db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
    finally:
        _cleanup_session(session_id)


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
            assert db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
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
            assert db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 1
            source_message = (
                db.query(MessageRecord)
                .filter(MessageRecord.session_id == session_id, MessageRecord.content == TASK_REQUEST)
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


def test_followup_style_request_can_be_used_as_confirmation_context(client: TestClient) -> None:
    session_id = _create_session()
    try:
        first_prompt = _post_message(client, session_id, TASK_REQUEST)
        assert first_prompt["awaiting_task_confirmation"] is True

        parent_payload = _post_message(client, session_id, "好的，继续")
        assert parent_payload["mode"] == "task"
        parent_task_id = parent_payload["task_id"]
        assert parent_task_id

        followup_prompt = _post_message(client, session_id, FOLLOWUP_REQUEST)
        assert followup_prompt["awaiting_task_confirmation"] is True

        payload = _post_message(client, session_id, "好的，继续")

        assert payload["mode"] == "task"
        assert payload["task_id"]
        assert payload["task_id"] != parent_task_id

        with SessionLocal() as db:
            task = db.get(TaskRunRecord, payload["task_id"])
            assert task is not None
            assert task.parent_task_id == parent_task_id
            assert task.requested_time_range == {"start": "2023-06-01", "end": "2023-06-30"}
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
            assert db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 1
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
            assert db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).count() == 0
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
