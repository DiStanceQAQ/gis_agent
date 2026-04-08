"""Add conversation understanding revision persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260408_0005"
down_revision = "20260403_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_runs", sa.Column("interaction_state", sa.String(length=32), nullable=True))
    op.add_column("task_runs", sa.Column("last_understanding_message_id", sa.String(length=32), nullable=True))
    op.add_column("task_runs", sa.Column("last_response_mode", sa.String(length=32), nullable=True))

    op.create_table(
        "task_spec_revisions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("base_revision_id", sa.String(length=32), nullable=True),
        sa.Column("source_message_id", sa.String(length=32), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("understanding_intent", sa.String(length=32), nullable=False),
        sa.Column("understanding_summary", sa.Text(), nullable=True),
        sa.Column("raw_spec_json", sa.JSON(), nullable=False),
        sa.Column("field_confidences_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("ranked_candidates_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("response_mode", sa.String(length=32), nullable=True),
        sa.Column("response_payload_json", sa.JSON(), nullable=True),
        sa.Column("execution_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("execution_blocked_reason", sa.Text(), nullable=True),
        sa.Column("understanding_trace_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("user_revision_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("user_last_revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_spec_revisions_source_message_id", "task_spec_revisions", ["source_message_id"])
    op.create_index("ix_task_spec_revisions_task_active", "task_spec_revisions", ["task_id", "is_active"])
    op.create_index("ix_task_spec_revisions_task_created", "task_spec_revisions", ["task_id", "created_at"])
    op.create_index("ux_task_spec_revisions_task_revision", "task_spec_revisions", ["task_id", "revision_number"], unique=True)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_spec_revisions_active ON task_spec_revisions (task_id) WHERE is_active = true"
    )
    op.create_table(
        "message_understandings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(length=32),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("session_id", sa.String(length=32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("task_id", sa.String(length=32), sa.ForeignKey("task_runs.id"), nullable=True),
        sa.Column("derived_revision_id", sa.String(length=32), nullable=True),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("intent_confidence", sa.Float(), nullable=False),
        sa.Column("understanding_summary", sa.Text(), nullable=True),
        sa.Column("response_mode", sa.String(length=32), nullable=True),
        sa.Column("field_confidences_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("field_evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("context_trace_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_message_understandings_derived_revision_id",
        "message_understandings",
        ["derived_revision_id"],
    )
    op.create_index("ix_message_understandings_session_created", "message_understandings", ["session_id", "created_at"])
    op.create_index("ix_message_understandings_task_created", "message_understandings", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_message_understandings_task_created", table_name="message_understandings")
    op.drop_index("ix_message_understandings_session_created", table_name="message_understandings")
    op.drop_index("ix_message_understandings_derived_revision_id", table_name="message_understandings")
    op.drop_table("message_understandings")

    op.execute("DROP INDEX IF EXISTS ux_task_spec_revisions_active")
    op.drop_index("ix_task_spec_revisions_task_created", table_name="task_spec_revisions")
    op.drop_index("ix_task_spec_revisions_task_active", table_name="task_spec_revisions")
    op.drop_index("ix_task_spec_revisions_source_message_id", table_name="task_spec_revisions")
    op.drop_index("ux_task_spec_revisions_task_revision", table_name="task_spec_revisions")
    op.drop_table("task_spec_revisions")

    op.drop_column("task_runs", "last_response_mode")
    op.drop_column("task_runs", "last_understanding_message_id")
    op.drop_column("task_runs", "interaction_state")
