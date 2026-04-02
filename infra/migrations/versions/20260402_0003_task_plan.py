"""Add task plan payload to task runs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260402_0003"
down_revision = "20260401_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_runs", sa.Column("plan_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_runs", "plan_json")
