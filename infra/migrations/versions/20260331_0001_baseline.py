"""Baseline schema for GIS Agent MVP."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry


revision = "20260331_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("linked_task_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"], unique=False)

    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_uploaded_files_session_id", "uploaded_files", ["session_id"], unique=False)

    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("parent_task_id", sa.String(length=32), nullable=True),
        sa.Column("user_message_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_step", sa.String(length=64), nullable=True),
        sa.Column("analysis_type", sa.String(length=32), nullable=False),
        sa.Column("requested_time_range", sa.JSON(), nullable=True),
        sa.Column("actual_time_range", sa.JSON(), nullable=True),
        sa.Column("selected_dataset", sa.String(length=32), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("recommendation_json", sa.JSON(), nullable=True),
        sa.Column("result_summary_text", sa.Text(), nullable=True),
        sa.Column("methods_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_runs_parent_task_id", "task_runs", ["parent_task_id"], unique=False)
    op.create_index("ix_task_runs_session_id", "task_runs", ["session_id"], unique=False)
    op.create_index("ix_task_runs_status", "task_runs", ["status"], unique=False)

    op.create_table(
        "task_specs",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("aoi_input", sa.Text(), nullable=True),
        sa.Column("aoi_source_type", sa.String(length=32), nullable=True),
        sa.Column("preferred_output", sa.JSON(), nullable=True),
        sa.Column("user_priority", sa.String(length=32), nullable=False),
        sa.Column("need_confirmation", sa.Boolean(), nullable=False),
        sa.Column("raw_spec_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )

    op.create_table(
        "aois",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("source_file_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "geom",
            Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column(
            "bbox",
            Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("bbox_bounds_json", sa.JSON(), nullable=True),
        sa.Column("area_km2", sa.Float(), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("validation_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_aois_bbox", "aois", ["bbox"], unique=False, postgresql_using="gist")
    op.create_index("ix_aois_geom", "aois", ["geom"], unique=False, postgresql_using="gist")

    op.create_table(
        "dataset_candidates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("dataset_name", sa.String(length=32), nullable=False),
        sa.Column("collection_id", sa.String(length=128), nullable=False),
        sa.Column("scene_count", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Float(), nullable=False),
        sa.Column("effective_pixel_ratio_estimate", sa.Float(), nullable=False),
        sa.Column("cloud_metric_summary", sa.JSON(), nullable=True),
        sa.Column("spatial_resolution", sa.Integer(), nullable=False),
        sa.Column("temporal_density_note", sa.String(length=32), nullable=False),
        sa.Column("suitability_score", sa.Float(), nullable=False),
        sa.Column("recommendation_rank", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dataset_candidates_task_id", "dataset_candidates", ["task_id"], unique=False)

    op.create_table(
        "task_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("step_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_steps_task_id", "task_steps", ["task_id"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_artifacts_task_id", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("ix_task_steps_task_id", table_name="task_steps")
    op.drop_table("task_steps")

    op.drop_index("ix_dataset_candidates_task_id", table_name="dataset_candidates")
    op.drop_table("dataset_candidates")

    op.drop_index("ix_aois_geom", table_name="aois")
    op.drop_index("ix_aois_bbox", table_name="aois")
    op.drop_table("aois")

    op.drop_table("task_specs")

    op.drop_index("ix_task_runs_status", table_name="task_runs")
    op.drop_index("ix_task_runs_session_id", table_name="task_runs")
    op.drop_index("ix_task_runs_parent_task_id", table_name="task_runs")
    op.drop_table("task_runs")

    op.drop_index("ix_uploaded_files_session_id", table_name="uploaded_files")
    op.drop_table("uploaded_files")

    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_table("messages")

    op.drop_table("sessions")
