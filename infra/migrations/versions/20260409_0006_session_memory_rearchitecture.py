"""Add session memory tables and lineage columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260409_0006"
down_revision = "20260408_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_memory_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.String(length=32), nullable=True),
        sa.Column("revision_id", sa.String(length=32), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["task_spec_revisions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_session_memory_events_session_created",
        "session_memory_events",
        ["session_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_session_memory_events_message_id", "session_memory_events", ["message_id"], unique=False)
    op.create_index("ix_session_memory_events_revision_id", "session_memory_events", ["revision_id"], unique=False)

    op.create_table(
        "session_memory_summaries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("summary_type", sa.String(length=32), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("source_event_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.ForeignKeyConstraint(["source_event_id"], ["session_memory_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_session_memory_summaries_session_created",
        "session_memory_summaries",
        ["session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "session_state_snapshots",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("lineage_root_id", sa.String(length=32), nullable=True),
        sa.Column("active_revision_id", sa.String(length=32), nullable=True),
        sa.Column("active_summary_id", sa.String(length=32), nullable=True),
        sa.Column("state_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("history_features_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_session_state_snapshots_session",
        "session_state_snapshots",
        ["session_id"],
        unique=True,
    )

    op.create_table(
        "session_memory_links",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=32), nullable=False),
        sa.Column("link_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_session_memory_links_session_source",
        "session_memory_links",
        ["session_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_memory_links_session_target",
        "session_memory_links",
        ["session_id", "target_type", "target_id"],
        unique=False,
    )

    op.add_column("message_understandings", sa.Column("snapshot_id", sa.String(length=32), nullable=True))
    op.add_column("message_understandings", sa.Column("summary_id", sa.String(length=32), nullable=True))
    op.add_column(
        "message_understandings",
        sa.Column("history_features_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("message_understandings", sa.Column("lineage_root_id", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        "fk_message_understandings_snapshot_id",
        "message_understandings",
        "session_state_snapshots",
        ["snapshot_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_message_understandings_summary_id",
        "message_understandings",
        "session_memory_summaries",
        ["summary_id"],
        ["id"],
    )
    op.create_index(
        "ix_message_understandings_snapshot_id",
        "message_understandings",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index("ix_message_understandings_summary_id", "message_understandings", ["summary_id"], unique=False)
    op.create_index(
        "ix_message_understandings_lineage_root_id",
        "message_understandings",
        ["lineage_root_id"],
        unique=False,
    )

    op.add_column("task_spec_revisions", sa.Column("lineage_root_id", sa.String(length=32), nullable=True))
    op.add_column(
        "task_spec_revisions",
        sa.Column("parent_message_understanding_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "task_spec_revisions",
        sa.Column("history_features_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.create_foreign_key(
        "fk_task_spec_revisions_parent_message_understanding_id",
        "task_spec_revisions",
        "message_understandings",
        ["parent_message_understanding_id"],
        ["id"],
    )
    op.create_index(
        "ix_task_spec_revisions_lineage_root_id",
        "task_spec_revisions",
        ["lineage_root_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_spec_revisions_parent_message_understanding_id",
        "task_spec_revisions",
        ["parent_message_understanding_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_spec_revisions_parent_message_understanding_id", table_name="task_spec_revisions")
    op.drop_index("ix_task_spec_revisions_lineage_root_id", table_name="task_spec_revisions")
    op.drop_constraint(
        "fk_task_spec_revisions_parent_message_understanding_id",
        "task_spec_revisions",
        type_="foreignkey",
    )
    op.drop_column("task_spec_revisions", "history_features_json")
    op.drop_column("task_spec_revisions", "parent_message_understanding_id")
    op.drop_column("task_spec_revisions", "lineage_root_id")

    op.drop_index("ix_message_understandings_lineage_root_id", table_name="message_understandings")
    op.drop_index("ix_message_understandings_summary_id", table_name="message_understandings")
    op.drop_index("ix_message_understandings_snapshot_id", table_name="message_understandings")
    op.drop_constraint("fk_message_understandings_summary_id", "message_understandings", type_="foreignkey")
    op.drop_constraint("fk_message_understandings_snapshot_id", "message_understandings", type_="foreignkey")
    op.drop_column("message_understandings", "lineage_root_id")
    op.drop_column("message_understandings", "history_features_json")
    op.drop_column("message_understandings", "summary_id")
    op.drop_column("message_understandings", "snapshot_id")

    op.drop_index("ix_session_memory_links_session_target", table_name="session_memory_links")
    op.drop_index("ix_session_memory_links_session_source", table_name="session_memory_links")
    op.drop_table("session_memory_links")

    op.drop_index("ux_session_state_snapshots_session", table_name="session_state_snapshots")
    op.drop_table("session_state_snapshots")

    op.drop_index("ix_session_memory_summaries_session_created", table_name="session_memory_summaries")
    op.drop_table("session_memory_summaries")

    op.drop_index("ix_session_memory_events_revision_id", table_name="session_memory_events")
    op.drop_index("ix_session_memory_events_message_id", table_name="session_memory_events")
    op.drop_index("ix_session_memory_events_session_created", table_name="session_memory_events")
    op.drop_table("session_memory_events")
