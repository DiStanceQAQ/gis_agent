from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
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


@pytest.fixture(autouse=True)
def _patched_message_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)
    monkeypatch.setattr(orchestrator, "generate_chat_reply", lambda **kwargs: "当然可以，我们先聊聊。")

    def _classify_intent(message: str, **kwargs) -> IntentResult:  # noqa: ANN003
        del kwargs
        if is_task_confirmation_message(message):
            return IntentResult(intent="task", confidence=0.99, reason="用户已明确确认执行任务。")
        if "NDVI" in message:
            return IntentResult(intent="task", confidence=0.95, reason="用户正在发起遥感分析任务。")
        return IntentResult(intent="chat", confidence=0.91, reason="用户正在进行普通对话。")

    def _parse_task_message(message: str, has_upload: bool) -> ParsedTaskSpec:
        assert has_upload is False
        assert message == TASK_REQUEST
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


def _post_message(client: TestClient, session_id: str, content: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/messages",
        json={"session_id": session_id, "content": content, "file_ids": []},
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
