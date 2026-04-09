from __future__ import annotations

from packages.domain import models  # noqa: F401
from packages.domain.database import Base
from packages.schemas.session_memory import (
    LineageLinkPayload,
    SessionMemoryEventPayload,
    SessionMemorySummaryPayload,
    SessionStateSnapshotPayload,
)


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
