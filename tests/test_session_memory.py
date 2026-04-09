from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.domain import models  # noqa: F401
from packages.domain.database import SessionLocal
from packages.domain.database import Base
from packages.domain.models import (
    MessageRecord,
    MessageUnderstandingRecord,
    SessionMemoryEventRecord,
    SessionRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskSpecRevisionRecord,
)
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.task_revisions import activate_revision
from packages.domain.utils import make_id
from packages.schemas.session_memory import (
    LineageLinkPayload,
    SessionMemoryEventPayload,
    SessionMemorySummaryPayload,
    SessionStateSnapshotPayload,
)
from sqlalchemy.orm import Session


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as db:
        yield db


def _fk_targets(table_name: str, column_name: str) -> set[str]:
    table = Base.metadata.tables[table_name]
    return {fk.target_fullname for fk in table.c[column_name].foreign_keys}


def test_session_memory_tables_are_registered() -> None:
    expected = {
        "session_memory_events",
        "session_state_snapshots",
        "session_memory_summaries",
        "session_memory_links",
        "session_memory_retrieval_cache",
    }
    assert expected.issubset(Base.metadata.tables.keys())

    events = Base.metadata.tables["session_memory_events"]
    assert "task_id" in events.c
    assert "event_payload_json" in events.c
    assert "payload_json" not in events.c
    event_index_names = {index.name for index in events.indexes}
    assert "ix_session_memory_events_session_created" in event_index_names
    assert "ix_session_memory_events_message_id" in event_index_names
    assert "ix_session_memory_events_revision_id" in event_index_names

    snapshots = Base.metadata.tables["session_state_snapshots"]
    for column in (
        "active_task_id",
        "active_revision_id",
        "active_revision_number",
        "active_understanding_id",
        "open_missing_fields_json",
        "blocked_reason",
        "latest_summary_id",
        "field_history_rollup_json",
        "user_preference_profile_json",
        "risk_profile_json",
        "snapshot_version",
        "created_at",
    ):
        assert column in snapshots.c
    assert "lineage_root_id" not in snapshots.c
    assert "state_json" not in snapshots.c
    assert "updated_at" not in snapshots.c
    snapshot_index_names = {index.name for index in snapshots.indexes}
    assert "ux_session_state_snapshots_session" in snapshot_index_names

    summaries = Base.metadata.tables["session_memory_summaries"]
    assert "summary_kind" in summaries.c
    assert "source_event_range_json" in summaries.c
    assert "summary_type" not in summaries.c
    assert "source_event_id" not in summaries.c

    retrieval_cache = Base.metadata.tables["session_memory_retrieval_cache"]
    for column in (
        "id",
        "session_id",
        "message_id",
        "query_fingerprint",
        "retrieval_result_json",
        "created_at",
    ):
        assert column in retrieval_cache.c


def test_session_memory_links_has_key_columns_and_lookup_indexes() -> None:
    links = Base.metadata.tables["session_memory_links"]

    for column in (
        "id",
        "session_id",
        "source_type",
        "source_id",
        "target_type",
        "target_id",
        "link_type",
        "payload_json",
        "created_at",
    ):
        assert column in links.c

    link_index_names = {index.name for index in links.indexes}
    assert "ix_session_memory_links_session_source" in link_index_names
    assert "ix_session_memory_links_session_target" in link_index_names


def test_lineage_columns_added_to_understandings_and_revisions() -> None:
    understandings = Base.metadata.tables["message_understandings"]
    for column in (
        "snapshot_id",
        "summary_id",
        "history_features_json",
        "lineage_root_id",
    ):
        assert column in understandings.c

    revisions = Base.metadata.tables["task_spec_revisions"]
    for column in (
        "lineage_root_id",
        "parent_message_understanding_id",
        "history_features_json",
    ):
        assert column in revisions.c


def test_session_memory_foreign_key_wiring() -> None:
    assert _fk_targets("session_memory_events", "session_id") == {"sessions.id"}
    assert _fk_targets("session_memory_events", "message_id") == {"messages.id"}
    assert _fk_targets("session_memory_events", "task_id") == {"task_runs.id"}
    assert _fk_targets("session_memory_events", "revision_id") == {"task_spec_revisions.id"}

    assert _fk_targets("message_understandings", "snapshot_id") == {"session_state_snapshots.id"}
    assert _fk_targets("message_understandings", "summary_id") == {"session_memory_summaries.id"}
    assert _fk_targets("task_spec_revisions", "parent_message_understanding_id") == {"message_understandings.id"}

    assert _fk_targets("session_memory_retrieval_cache", "session_id") == {"sessions.id"}
    assert _fk_targets("session_memory_retrieval_cache", "message_id") == {"messages.id"}


def test_session_memory_model_relationship_accessors_exist() -> None:
    session_relationships = set(models.SessionRecord.__mapper__.relationships.keys())
    assert "session_memory_events" in session_relationships
    assert "session_state_snapshots" in session_relationships
    assert "session_memory_summaries" in session_relationships
    assert "session_memory_links" in session_relationships
    assert "session_memory_retrieval_cache_entries" in session_relationships

    understanding_relationships = set(models.MessageUnderstandingRecord.__mapper__.relationships.keys())
    assert "snapshot" in understanding_relationships
    assert "summary" in understanding_relationships

    revision_relationships = set(models.TaskSpecRevisionRecord.__mapper__.relationships.keys())
    assert "parent_message_understanding" in revision_relationships

    event_relationships = set(models.SessionMemoryEventRecord.__mapper__.relationships.keys())
    assert "session" in event_relationships
    assert "message" in event_relationships
    assert "task" in event_relationships
    assert "revision" in event_relationships

    retrieval_relationships = set(models.SessionMemoryRetrievalCacheRecord.__mapper__.relationships.keys())
    assert "session" in retrieval_relationships
    assert "message" in retrieval_relationships


def test_session_memory_schema_payload_models_validate() -> None:
    event = SessionMemoryEventPayload.model_validate(
        {
            "event_type": "message_understanding_created",
            "message_id": "msg_001",
            "task_id": "task_001",
            "revision_id": "rev_001",
            "event_payload": {"intent": "task_correction"},
        }
    )
    assert event.task_id == "task_001"
    assert event.event_payload["intent"] == "task_correction"

    snapshot = SessionStateSnapshotPayload.model_validate(
        {
            "active_task_id": "task_001",
            "active_revision_id": "rev_001",
            "active_revision_number": 2,
            "active_understanding_id": "mu_001",
            "open_missing_fields": ["time_range"],
            "blocked_reason": "need_time_range",
            "latest_summary_id": "sum_001",
            "field_history_rollup": {"aoi_input": ["江西省"]},
            "user_preference_profile": {"output": "png_map"},
            "risk_profile": {"continuation_without_confirmation": 0.42},
            "snapshot_version": 1,
        }
    )
    assert snapshot.active_task_id == "task_001"
    assert snapshot.open_missing_fields == ["time_range"]
    assert snapshot.snapshot_version == 1

    summary = SessionMemorySummaryPayload.model_validate(
        {
            "summary_kind": "rolling_context",
            "summary_text": "用户更偏好输出 png 地图。",
            "summary": {"preferred_output": "png_map"},
            "source_event_range": {"start_event_id": "evt_001", "end_event_id": "evt_010"},
        }
    )
    assert summary.summary_kind == "rolling_context"
    assert summary.source_event_range["end_event_id"] == "evt_010"

    link = LineageLinkPayload.model_validate(
        {
            "source_type": "message_understanding",
            "source_id": "mu_001",
            "target_type": "task_spec_revision",
            "target_id": "rev_001",
            "link_type": "derived_revision",
            "weight": 0.8,
            "attributes": {"reason": "matched correction signal"},
        }
    )
    assert link.link_type == "derived_revision"
    assert link.weight == 0.8


def test_session_memory_schema_payload_model_defaults() -> None:
    event = SessionMemoryEventPayload(event_type="message_understanding_created")
    assert event.event_payload == {}

    snapshot = SessionStateSnapshotPayload()
    assert snapshot.open_missing_fields == []
    assert snapshot.field_history_rollup == {}
    assert snapshot.user_preference_profile == {}
    assert snapshot.risk_profile == {}
    assert snapshot.snapshot_version == 1

    summary = SessionMemorySummaryPayload(summary_kind="rolling_context")
    assert summary.summary == {}
    assert summary.source_event_range == {}

    link = LineageLinkPayload(
        source_type="message_understanding",
        source_id="mu_001",
        target_type="task_spec_revision",
        target_id="rev_001",
        link_type="derived_revision",
    )
    assert link.attributes == {}


def test_session_memory_service_record_event_persists_event_and_snapshot(
    db_session: Session,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="memory-service", status="active")
    message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="请分析江西")
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session.id,
        user_message_id=message.id,
        status="waiting_clarification",
    )
    task.task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="江西",
        aoi_source_type="admin_name",
        preferred_output=["png_map"],
        user_priority="speed",
        need_confirmation=True,
        raw_spec_json={
            "aoi_input": "江西",
            "aoi_source_type": "admin_name",
            "missing_fields": ["time_range"],
            "preferred_output": ["png_map"],
            "user_priority": "speed",
        },
    )
    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=2,
        base_revision_id=None,
        source_message_id=message.id,
        change_type="correction",
        is_active=True,
        understanding_intent="task_correction",
        understanding_summary="缺少时间范围",
        raw_spec_json=dict(task.task_spec.raw_spec_json),
        field_confidences_json={"aoi_input": {"score": 0.92}},
        ranked_candidates_json={},
        response_mode="ask_missing_fields",
        understanding_trace_json={},
        execution_blocked=True,
        execution_blocked_reason="need_time_range",
    )
    understanding = MessageUnderstandingRecord(
        id=make_id("mu"),
        message_id=message.id,
        session_id=session.id,
        task_id=task.id,
        intent="task_correction",
        intent_confidence=0.99,
        understanding_summary="用户补充任务",
        response_mode="ask_missing_fields",
        field_confidences_json={},
        field_evidence_json={},
        context_trace_json={},
        history_features_json={},
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([session, message, task, revision, understanding])
    db_session.flush()

    service = SessionMemoryService(db_session)
    service.record_event(
        session_id=session.id,
        event_type="message_received",
        message_id=message.id,
        task_id=task.id,
        revision_id=revision.id,
        event_payload={"source": "user_ingestion"},
    )

    event = db_session.query(SessionMemoryEventRecord).filter_by(session_id=session.id).one()
    snapshot = service.get_latest_snapshot(session.id)

    assert event.event_type == "message_received"
    assert event.event_payload_json == {"source": "user_ingestion"}
    assert snapshot is not None
    assert snapshot.active_task_id == task.id
    assert snapshot.active_revision_id == revision.id
    assert snapshot.active_understanding_id == understanding.id
    assert snapshot.open_missing_fields_json == ["time_range"]
    assert snapshot.blocked_reason == "need_time_range"
    assert snapshot.field_history_rollup_json == {"aoi_input": {"score": 0.92}}
    assert snapshot.user_preference_profile_json == {
        "preferred_output": ["png_map"],
        "user_priority": "speed",
    }
    assert snapshot.risk_profile_json == {"execution_blocked": True}
    assert snapshot.snapshot_version == 1


def test_session_memory_service_refresh_snapshot_increments_snapshot_version(
    db_session: Session,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="memory-version", status="active")
    message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="任务")
    task = TaskRunRecord(id=make_id("task"), session_id=session.id, user_message_id=message.id)
    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=message.id,
        change_type="initial_parse",
        is_active=True,
        understanding_intent="new_task",
        understanding_summary=None,
        raw_spec_json={},
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="confirm_understanding",
        understanding_trace_json={},
    )
    db_session.add_all([session, message, task, revision])
    db_session.flush()

    service = SessionMemoryService(db_session)
    first = service.refresh_snapshot(session.id, task_id=task.id, revision_id=revision.id)
    first_version = first.snapshot_version
    second = service.refresh_snapshot(session.id, task_id=task.id, revision_id=revision.id)

    assert first_version == 1
    assert second.snapshot_version == 2


def test_activate_revision_records_revision_activated_session_memory_event(
    db_session: Session,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="activate-revision", status="active")
    first_message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="江西")
    second_message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="改成北京")
    task = TaskRunRecord(id=make_id("task"), session_id=session.id, user_message_id=first_message.id)
    task.task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="江西",
        aoi_source_type="admin_name",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
    )
    rev1 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=first_message.id,
        change_type="initial_parse",
        is_active=True,
        understanding_intent="new_task",
        understanding_summary="江西",
        raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="confirm_understanding",
        understanding_trace_json={},
    )
    rev2 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=2,
        base_revision_id=rev1.id,
        source_message_id=second_message.id,
        change_type="correction",
        is_active=False,
        understanding_intent="task_correction",
        understanding_summary="北京",
        raw_spec_json={"aoi_input": "北京", "aoi_source_type": "admin_name"},
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="show_revision",
        understanding_trace_json={},
    )
    db_session.add_all([session, first_message, second_message, task, rev1, rev2])
    db_session.flush()

    activate_revision(db_session, task=task, revision=rev2)

    event = (
        db_session.query(SessionMemoryEventRecord)
        .filter(
            SessionMemoryEventRecord.session_id == session.id,
            SessionMemoryEventRecord.event_type == "revision_activated",
            SessionMemoryEventRecord.revision_id == rev2.id,
        )
        .one_or_none()
    )
    assert event is not None
