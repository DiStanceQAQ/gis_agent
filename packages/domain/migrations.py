from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from packages.domain.config import get_settings
from packages.domain.database import engine


BASELINE_REVISION = "20260331_0001"
BASELINE_TABLES = frozenset(
    {
        "sessions",
        "messages",
        "uploaded_files",
        "task_runs",
        "task_specs",
        "aois",
        "dataset_candidates",
        "task_steps",
        "artifacts",
    }
)
MIGRATION_LOCK_ID = 20260331


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_alembic_config() -> Config:
    config = Config(str(_repo_root() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    return config


def detect_bootstrap_mode(existing_tables: set[str]) -> str:
    tables_without_version = existing_tables - {"alembic_version"}
    present_baseline_tables = tables_without_version & BASELINE_TABLES

    if not present_baseline_tables:
        return "fresh"
    if BASELINE_TABLES.issubset(tables_without_version):
        return "legacy_baseline"
    return "partial"


def run_migrations(revision: str = "head") -> None:
    settings = get_settings()
    if not settings.is_postgres:
        raise RuntimeError("GIS Agent currently requires PostgreSQL / PostGIS.")

    config = build_alembic_config()

    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID})
        try:
            config.attributes["connection"] = connection
            inspector = inspect(connection)
            existing_tables = set(inspector.get_table_names())
            bootstrap_mode = detect_bootstrap_mode(existing_tables)

            if bootstrap_mode == "partial":
                raise RuntimeError(
                    "Detected a partial legacy schema without alembic_version. "
                    "Please reconcile the database before continuing."
                )

            if "alembic_version" not in existing_tables and bootstrap_mode == "legacy_baseline":
                command.stamp(config, BASELINE_REVISION)

            command.upgrade(config, revision)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID})
