from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.database import SessionLocal
from packages.domain.models import (
    MessageRecord,
    MessageUnderstandingRecord,
    SessionMemoryEventRecord,
    SessionMemoryLinkRecord,
    SessionMemorySummaryRecord,
    SessionRecord,
    SessionStateSnapshotRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskSpecRevisionRecord,
)
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.understanding import MessageUnderstanding, persist_message_understanding
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec


def _cleanup_session_memory(session_id: str) -> None:
    with SessionLocal() as db:
        db.query(SessionMemoryEventRecord).filter(
            SessionMemoryEventRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionMemoryLinkRecord).filter(
            SessionMemoryLinkRecord.session_id == session_id
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
            MessageUnderstandingRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionMemorySummaryRecord).filter(
            SessionMemorySummaryRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(SessionStateSnapshotRecord).filter(
            SessionStateSnapshotRecord.session_id == session_id
        ).delete(synchronize_session=False)
        db.query(TaskSpecRecord).filter(TaskSpecRecord.task_id.in_(task_ids)).delete(
            synchronize_session=False
        )
        db.query(TaskRunRecord).filter(TaskRunRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(MessageRecord).filter(MessageRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(
            synchronize_session=False
        )
        db.commit()


def _seed_session_memory() -> str:
    with SessionLocal() as db:
        session = SessionRecord(id=make_id("ses"), title="memory-api", status="active")
        message = MessageRecord(
            id=make_id("msg"),
            session_id=session.id,
            role="user",
            content="把 AOI 改成北京。",
            created_at=datetime.now(timezone.utc),
        )
        task = TaskRunRecord(
            id=make_id("task"),
            session_id=session.id,
            user_message_id=message.id,
            status="waiting_clarification",
            current_step="understanding",
            analysis_type="NDVI",
        )
        revision = TaskSpecRevisionRecord(
            id=make_id("rev"),
            task_id=task.id,
            revision_number=2,
            base_revision_id=None,
            source_message_id=message.id,
            change_type="task_correction",
            is_active=True,
            understanding_intent="task_correction",
            understanding_summary="把 AOI 改成北京。",
            raw_spec_json={
                "aoi_input": "北京",
                "aoi_source_type": "admin_name",
                "analysis_type": "NDVI",
                "missing_fields": ["time_range"],
                "preferred_output": ["png_map"],
                "user_priority": "balanced",
            },
            field_confidences_json={"aoi_input": {"score": 0.94, "level": "high"}},
            ranked_candidates_json={},
            response_mode="show_revision",
            understanding_trace_json={},
        )
        db.add_all([session, message, task, revision])
        db.flush()

        understanding = MessageUnderstanding(
            intent="task_correction",
            intent_confidence=0.95,
            understanding_summary="把 AOI 改成北京。",
            parsed_spec=ParsedTaskSpec(
                aoi_input="北京",
                aoi_source_type="admin_name",
                analysis_type="NDVI",
            ),
            parsed_candidates={"aoi_input": "北京"},
            field_confidences={},
            ranked_candidates={},
            trace={"source": "test"},
        )
        persist_message_understanding(
            db,
            message_id=message.id,
            session_id=session.id,
            task_id=task.id,
            derived_revision_id=revision.id,
            understanding=understanding,
            response_mode="show_revision",
        )
        SessionMemoryService(db).record_event(
            session_id=session.id,
            event_type="revision_activated",
            message_id=message.id,
            task_id=task.id,
            revision_id=revision.id,
            event_payload={"change_type": "task_correction"},
        )
        db.commit()
        return session.id


def test_get_session_memory_returns_snapshot_summary_and_lineage() -> None:
    session_id = _seed_session_memory()
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/sessions/{session_id}/memory?limit=10")
        assert response.status_code == 200
        payload = response.json()

        assert payload["session_id"] == session_id
        assert payload["snapshot"]["active_revision_number"] == 2
        assert payload["snapshot"]["field_history_rollup"]["aoi_input"]["latest_value"] == "北京"
        assert payload["latest_summary"]["summary_kind"] == "rolling_context"
        assert payload["lineage"][0]["intent"] == "task_correction"
        assert payload["lineage"][0]["derived_revision_id"] is not None
        assert payload["lineage"][0]["link_targets"][0]["target_type"] == "revision"
        assert payload["events"][0]["event_type"] in {"revision_activated", "message_understanding_created"}
    finally:
        _cleanup_session_memory(session_id)


def test_get_session_memory_returns_404_for_missing_session() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions/ses_missing/memory")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


def test_get_session_memory_lazily_backfills_snapshot_for_legacy_session() -> None:
    with SessionLocal() as db:
        session = SessionRecord(id=make_id("ses"), title="legacy-memory-api", status="active")
        message = MessageRecord(
            id=make_id("msg"),
            session_id=session.id,
            role="user",
            content="分析江西 NDVI",
            created_at=datetime.now(timezone.utc),
        )
        task = TaskRunRecord(
            id=make_id("task"),
            session_id=session.id,
            user_message_id=message.id,
            status="queued",
            current_step="planning",
            analysis_type="NDVI",
        )
        revision = TaskSpecRevisionRecord(
            id=make_id("rev"),
            task_id=task.id,
            revision_number=1,
            base_revision_id=None,
            source_message_id=message.id,
            change_type="initial_parse",
            is_active=True,
            understanding_intent="new_task",
            understanding_summary="分析江西 NDVI",
            raw_spec_json={
                "aoi_input": "江西",
                "aoi_source_type": "admin_name",
                "analysis_type": "NDVI",
                "missing_fields": ["time_range"],
            },
            field_confidences_json={"aoi_input": {"score": 0.9, "level": "high"}},
            ranked_candidates_json={},
            response_mode="confirm_understanding",
            understanding_trace_json={},
        )
        db.add_all([session, message, task, revision])
        db.commit()
        session_id = session.id
        revision_id = revision.id

    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/sessions/{session_id}/memory")
        assert response.status_code == 200
        payload = response.json()
        assert payload["snapshot"]["active_revision_id"] == revision_id
        assert payload["latest_summary"]["summary_kind"] == "rolling_context"
    finally:
        _cleanup_session_memory(session_id)


def test_get_session_memory_materializes_legacy_task_spec_without_revision() -> None:
    with SessionLocal() as db:
        session = SessionRecord(id=make_id("ses"), title="legacy-task-spec-memory", status="active")
        message = MessageRecord(
            id=make_id("msg"),
            session_id=session.id,
            role="user",
            content="分析江西",
            created_at=datetime.now(timezone.utc),
        )
        task = TaskRunRecord(
            id=make_id("task"),
            session_id=session.id,
            user_message_id=message.id,
            status="queued",
            current_step="planning",
            analysis_type="WORKFLOW",
        )
        task_spec = TaskSpecRecord(
            task_id=task.id,
            aoi_input="江西",
            aoi_source_type="admin_name",
            preferred_output=["png_map"],
            user_priority="balanced",
            need_confirmation=True,
            raw_spec_json={
                "aoi_input": "江西",
                "aoi_source_type": "admin_name",
                "analysis_type": "WORKFLOW",
                "missing_fields": ["time_range"],
                "preferred_output": ["png_map"],
                "user_priority": "balanced",
                "need_confirmation": True,
            },
        )
        task.task_spec = task_spec
        db.add_all([session, message, task, task_spec])
        db.commit()
        session_id = session.id
        task_id = task.id

    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/sessions/{session_id}/memory")
        assert response.status_code == 200
        payload = response.json()
        assert payload["snapshot"]["active_revision_id"] is not None
        assert payload["latest_summary"]["summary_kind"] == "rolling_context"

        with SessionLocal() as db:
            revisions = (
                db.query(TaskSpecRevisionRecord)
                .filter(TaskSpecRevisionRecord.task_id == task_id)
                .all()
            )
            assert len(revisions) == 1
            assert revisions[0].change_type == "legacy_backfill"
    finally:
        _cleanup_session_memory(session_id)
