from __future__ import annotations

from packages.domain import models  # noqa: F401
from packages.domain.database import Base
from packages.schemas.session_memory import (
    LineageLinkPayload,
    SessionStateSnapshotPayload,
)


def test_session_memory_tables_are_registered() -> None:
    expected = {
        "session_memory_events",
        "session_state_snapshots",
        "session_memory_summaries",
        "session_memory_links",
    }
    assert expected.issubset(Base.metadata.tables.keys())

    events = Base.metadata.tables["session_memory_events"]
    event_index_names = {index.name for index in events.indexes}
    assert "ix_session_memory_events_session_created" in event_index_names
    assert "ix_session_memory_events_message_id" in event_index_names
    assert "ix_session_memory_events_revision_id" in event_index_names

    snapshots = Base.metadata.tables["session_state_snapshots"]
    snapshot_index_names = {index.name for index in snapshots.indexes}
    assert "ux_session_state_snapshots_session" in snapshot_index_names


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
    snapshot = SessionStateSnapshotPayload.model_validate(
        {
            "lineage_root_id": "lin_001",
            "active_revision_id": "rev_001",
            "active_summary_id": "sum_001",
            "state": {"intent": "task_correction", "confidence": 0.91},
            "history_features": {"recent_intents": ["new_task", "task_correction"]},
        }
    )
    assert snapshot.lineage_root_id == "lin_001"
    assert snapshot.state["intent"] == "task_correction"

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
