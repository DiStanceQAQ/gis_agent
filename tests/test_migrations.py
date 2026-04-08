from pathlib import Path

from packages.domain.migrations import detect_bootstrap_mode


def test_detect_bootstrap_mode_for_fresh_schema() -> None:
    assert detect_bootstrap_mode(set()) == "fresh"


def test_detect_bootstrap_mode_for_legacy_baseline_schema() -> None:
    existing_tables = {
        "alembic_version",
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

    assert detect_bootstrap_mode(existing_tables) == "legacy_baseline"


def test_detect_bootstrap_mode_still_treats_legacy_schema_as_baseline() -> None:
    existing_tables = {
        "alembic_version",
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

    assert detect_bootstrap_mode(existing_tables) == "legacy_baseline"


def test_detect_bootstrap_mode_for_partial_legacy_schema() -> None:
    assert detect_bootstrap_mode({"sessions", "messages"}) == "partial"


def test_revision_migration_creates_expected_indexes() -> None:
    migration = Path("infra/migrations/versions/20260408_0005_conversation_understanding_revisions.py").read_text()

    assert "ix_task_spec_revisions_task_active" in migration
    assert "ix_task_spec_revisions_task_created" in migration
