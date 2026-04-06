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
from packages.domain.services.task_state import TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_CANCELLED
from packages.schemas.operation_plan import OperationNode, OperationPlan
from packages.schemas.task import ParsedTaskSpec


@pytest.fixture(autouse=True)
def _patched_plan_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)
    monkeypatch.setattr(orchestrator, "preflight_processing_pipeline_inputs", lambda **kwargs: None)
    monkeypatch.setattr(orchestrator, "generate_chat_reply", lambda **kwargs: "请确认是否开始执行任务。")

    def _classify_intent(message: str, **kwargs) -> IntentResult:  # noqa: ANN003
        del kwargs
        if is_task_confirmation_message(message):
            return IntentResult(intent="task", confidence=0.99, reason="用户确认执行任务。")
        return IntentResult(intent="task", confidence=0.96, reason="用户正在创建 GIS 分析任务。")

    monkeypatch.setattr(orchestrator, "classify_message_intent", _classify_intent)
    monkeypatch.setattr(
        orchestrator,
        "parse_task_message",
        lambda message, has_upload: ParsedTaskSpec(  # noqa: ARG005
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
        ),
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


def _create_message(client: TestClient, session_id: str) -> dict:
    response = client.post(
        "/api/v1/messages",
        json={
            "session_id": session_id,
            "content": "帮我把 2024 年 6 月的 NDVI 任务先生成计划再审批",
            "file_ids": [],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    if payload.get("task_id"):
        return payload

    assert payload["awaiting_task_confirmation"] is True
    confirm_response = client.post(
        "/api/v1/messages",
        json={
            "session_id": session_id,
            "content": "好的，继续",
            "file_ids": [],
        },
    )
    assert confirm_response.status_code == 200
    return confirm_response.json()


def test_create_message_stops_at_awaiting_approval_and_skips_queue(client: TestClient) -> None:
    session_id = _create_session()
    queued: list[str] = []
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(orchestrator, "_queue_or_run", lambda task_id: queued.append(task_id))
            payload = _create_message(client, session_id)

        assert payload["need_approval"] is True
        assert payload["need_clarification"] is False
        assert payload["task_status"] == TASK_STATUS_AWAITING_APPROVAL
        assert queued == []

        detail_response = client.get(f"/api/v1/tasks/{payload['task_id']}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["status"] == TASK_STATUS_AWAITING_APPROVAL
        assert detail["operation_plan"] is not None
    finally:
        _cleanup_session(session_id)


def test_patch_task_plan_updates_version_and_content(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail_before = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail_before["operation_plan"]
        assert operation_plan is not None

        updated_plan = OperationPlan(
            version=operation_plan["version"] + 1,
            status="draft",
            missing_fields=[],
            nodes=[
                OperationNode(
                    step_id="step-upload",
                    op_name="input.upload_raster",
                    depends_on=[],
                    inputs={"upload_id": "file-1"},
                    params={"source_path": "/tmp/source.tif"},
                    outputs={"raster": "raster-1"},
                    retry_policy={"max_retries": 1},
                ),
                OperationNode(
                    step_id="step-export",
                    op_name="artifact.export",
                    depends_on=["step-upload"],
                    inputs={"primary": "raster-1"},
                    params={"format": "geotiff"},
                    outputs={"artifact": "artifact-1"},
                    retry_policy={"max_retries": 0},
                ),
            ],
        )

        patch_response = client.patch(
            f"/api/v1/tasks/{payload['task_id']}/plan",
            json={"operation_plan": updated_plan.model_dump()},
        )
        assert patch_response.status_code == 200
        detail = patch_response.json()
        assert detail["operation_plan"]["version"] == updated_plan.version
        assert detail["operation_plan"]["nodes"][0]["op_name"] == "input.upload_raster"
        assert detail["status"] == TASK_STATUS_AWAITING_APPROVAL
    finally:
        _cleanup_session(session_id)


def test_approve_task_plan_queues_execution(client: TestClient) -> None:
    session_id = _create_session()
    queued: list[str] = []
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(orchestrator, "_queue_or_run", lambda task_id: queued.append(task_id))
            payload = _create_message(client, session_id)
            detail_before = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
            operation_plan = detail_before["operation_plan"]
            assert operation_plan is not None

            patch_response = client.patch(
                f"/api/v1/tasks/{payload['task_id']}/plan",
                json={
                    "operation_plan": {
                        "version": operation_plan["version"] + 1,
                        "status": "draft",
                        "missing_fields": [],
                        "nodes": [
                            {
                                "step_id": "step-upload",
                                "op_name": "input.upload_raster",
                                "depends_on": [],
                                "inputs": {"upload_id": "file-1"},
                                "params": {"source_path": "/tmp/source.tif"},
                                "outputs": {"raster": "raster-1"},
                                "retry_policy": {"max_retries": 1},
                            }
                        ],
                    }
                },
            )
            assert patch_response.status_code == 200

            approve_response = client.post(
                f"/api/v1/tasks/{payload['task_id']}/approve",
                json={"approved_version": operation_plan["version"] + 1},
            )
        assert approve_response.status_code == 200
        approved_detail = approve_response.json()
        assert queued == [payload["task_id"]]
        assert approved_detail["status"] in {"approved", "queued", "running", "success"}
    finally:
        _cleanup_session(session_id)


def test_approve_task_plan_response_includes_execution_submission_metadata(client: TestClient) -> None:
    session_id = _create_session()
    queued: list[str] = []
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(orchestrator, "_queue_or_run", lambda task_id: queued.append(task_id))
            payload = _create_message(client, session_id)
            detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
            operation_plan = detail["operation_plan"]
            assert operation_plan is not None

            approve_response = client.post(
                f"/api/v1/tasks/{payload['task_id']}/approve",
                json={"approved_version": operation_plan["version"]},
            )
        assert approve_response.status_code == 200
        approved_detail = approve_response.json()
        assert queued == [payload["task_id"]]
        assert approved_detail["execution_submitted"] is True
        assert isinstance(approved_detail["execution_mode"], str)
        assert approved_detail["approval_required"] is False
    finally:
        _cleanup_session(session_id)


def test_approve_task_plan_rejects_version_mismatch_with_plan_edit_invalid(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        response = client.post(
            f"/api/v1/tasks/{payload['task_id']}/approve",
            json={"approved_version": operation_plan["version"] + 1},
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "plan_edit_invalid"
    finally:
        _cleanup_session(session_id)


def test_approve_task_plan_rejects_preflight_failure(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        def _raise_preflight(**kwargs):  # noqa: ANN003
            del kwargs
            raise ValueError("missing source path")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(orchestrator, "preflight_processing_pipeline_inputs", _raise_preflight)
            response = client.post(
                f"/api/v1/tasks/{payload['task_id']}/approve",
                json={"approved_version": operation_plan["version"]},
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "plan_edit_invalid"
        assert "preflight" in body["error"]["message"].lower()
    finally:
        _cleanup_session(session_id)


def test_reject_task_plan_cancels_without_queue(client: TestClient) -> None:
    session_id = _create_session()
    queued: list[str] = []
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(orchestrator, "_queue_or_run", lambda task_id: queued.append(task_id))
            payload = _create_message(client, session_id)
            reject_response = client.post(
                f"/api/v1/tasks/{payload['task_id']}/reject",
                json={"reason": "这次先不执行"},
            )

        assert reject_response.status_code == 200
        rejected_detail = reject_response.json()
        assert queued == []
        assert rejected_detail["status"] == TASK_STATUS_CANCELLED
        assert rejected_detail["error_code"] == "task_cancelled_by_user"
        assert rejected_detail["rejected_reason"] == "这次先不执行"
    finally:
        _cleanup_session(session_id)


def test_reject_task_plan_writes_rejected_and_cancelled_events(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        reject_response = client.post(
            f"/api/v1/tasks/{payload['task_id']}/reject",
            json={"reason": "计划不合理，先终止"},
        )
        assert reject_response.status_code == 200

        events_response = client.get(f"/api/v1/tasks/{payload['task_id']}/events")
        assert events_response.status_code == 200
        event_types = [item["event_type"] for item in events_response.json()["events"]]
        assert "task_plan_rejected" in event_types
        assert "task_cancelled" in event_types
    finally:
        _cleanup_session(session_id)


def test_reject_task_plan_requires_awaiting_approval_status(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        first_reject = client.post(
            f"/api/v1/tasks/{payload['task_id']}/reject",
            json={"reason": "先不跑"},
        )
        assert first_reject.status_code == 200

        second_reject = client.post(
            f"/api/v1/tasks/{payload['task_id']}/reject",
            json={"reason": "再次拒绝"},
        )
        assert second_reject.status_code == 400
        assert second_reject.json()["error"]["code"] == "plan_approval_required"
    finally:
        _cleanup_session(session_id)


def test_approve_after_reject_is_blocked(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        reject_response = client.post(
            f"/api/v1/tasks/{payload['task_id']}/reject",
            json={"reason": "先暂停"},
        )
        assert reject_response.status_code == 200

        approve_response = client.post(
            f"/api/v1/tasks/{payload['task_id']}/approve",
            json={"approved_version": operation_plan["version"]},
        )
        assert approve_response.status_code == 400
        assert approve_response.json()["error"]["code"] == "plan_approval_required"
    finally:
        _cleanup_session(session_id)


def test_patch_task_plan_rejects_unknown_dependency_with_plan_edit_invalid(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        response = client.patch(
            f"/api/v1/tasks/{payload['task_id']}/plan",
            json={
                "operation_plan": {
                    "version": operation_plan["version"] + 1,
                    "status": "draft",
                    "missing_fields": [],
                    "nodes": [
                        {
                            "step_id": "export_1",
                            "op_name": "artifact.export",
                            "depends_on": ["missing_step"],
                            "inputs": {},
                            "params": {"format": "geotiff"},
                            "outputs": {"artifact": "output_1"},
                            "retry_policy": {"max_retries": 0},
                        }
                    ],
                }
            },
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "plan_edit_invalid"
    finally:
        _cleanup_session(session_id)


def test_patch_task_plan_rejects_empty_nodes_with_plan_edit_invalid(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        response = client.patch(
            f"/api/v1/tasks/{payload['task_id']}/plan",
            json={
                "operation_plan": {
                    "version": operation_plan["version"] + 1,
                    "status": "draft",
                    "missing_fields": [],
                    "nodes": [],
                }
            },
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "plan_edit_invalid"
    finally:
        _cleanup_session(session_id)


def test_patch_task_plan_rejects_cycle_with_plan_edit_invalid(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        response = client.patch(
            f"/api/v1/tasks/{payload['task_id']}/plan",
            json={
                "operation_plan": {
                    "version": operation_plan["version"] + 1,
                    "status": "draft",
                    "missing_fields": [],
                    "nodes": [
                        {
                            "step_id": "a",
                            "op_name": "input.upload_raster",
                            "depends_on": ["b"],
                            "inputs": {"upload_id": "file-a"},
                            "params": {},
                            "outputs": {"raster": "raster-a"},
                            "retry_policy": {"max_retries": 0},
                        },
                        {
                            "step_id": "b",
                            "op_name": "artifact.export",
                            "depends_on": ["a"],
                            "inputs": {"primary": "raster-a"},
                            "params": {"format": "geotiff"},
                            "outputs": {"artifact": "output-b"},
                            "retry_policy": {"max_retries": 0},
                        },
                    ],
                }
            },
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "plan_edit_invalid"
    finally:
        _cleanup_session(session_id)


def test_patch_task_plan_rejects_non_incremental_version(client: TestClient) -> None:
    session_id = _create_session()
    try:
        payload = _create_message(client, session_id)
        detail = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        operation_plan = detail["operation_plan"]
        assert operation_plan is not None

        response = client.patch(
            f"/api/v1/tasks/{payload['task_id']}/plan",
            json={"operation_plan": operation_plan},
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "plan_edit_invalid"
    finally:
        _cleanup_session(session_id)
