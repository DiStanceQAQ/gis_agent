from packages.domain.database import Base
from packages.domain import models  # noqa: F401


def test_revision_tables_are_registered() -> None:
    assert "task_spec_revisions" in Base.metadata.tables
    assert "message_understandings" in Base.metadata.tables

    task_runs = Base.metadata.tables["task_runs"]
    assert "interaction_state" in task_runs.c
    assert "last_understanding_message_id" in task_runs.c
    assert "last_response_mode" in task_runs.c

    revisions = Base.metadata.tables["task_spec_revisions"]
    assert "execution_blocked" in revisions.c
    assert "execution_blocked_reason" in revisions.c
    assert "field_confidences_json" in revisions.c

    revision_index_names = {index.name for index in revisions.indexes}
    assert "ux_task_spec_revisions_active" in revision_index_names
