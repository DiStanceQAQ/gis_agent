from __future__ import annotations

import pytest

from packages.domain.database import SessionLocal
from packages.domain.models import MessageRecord, SessionRecord, TaskEventRecord, TaskRunRecord
from packages.domain.services.orchestrator import list_task_events
from packages.domain.utils import make_id


@pytest.fixture
def db_session():
    with SessionLocal() as db:
        yield db


@pytest.fixture
def seeded_task_id() -> str:
    session_id = make_id("ses")
    message_id = make_id("msg")
    task_id = make_id("task")

    with SessionLocal() as db:
        db.add(SessionRecord(id=session_id, title="events", status="active"))
        db.add(MessageRecord(id=message_id, session_id=session_id, role="user", content="hello"))
        db.add(TaskRunRecord(id=task_id, session_id=session_id, user_message_id=message_id, status="queued"))
        db.flush()

        db.add(TaskEventRecord(task_id=task_id, event_type="task_created", status="queued"))
        db.add(TaskEventRecord(task_id=task_id, event_type="task_queued", status="queued"))
        db.add(TaskEventRecord(task_id=task_id, event_type="task_started", status="running"))
        db.commit()

    yield task_id

    with SessionLocal() as db:
        db.query(TaskEventRecord).filter(TaskEventRecord.task_id == task_id).delete(synchronize_session=False)
        db.query(TaskRunRecord).filter(TaskRunRecord.id == task_id).delete(synchronize_session=False)
        db.query(MessageRecord).filter(MessageRecord.id == message_id).delete(synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
        db.commit()


def test_task_events_are_monotonic_by_id(db_session, seeded_task_id: str) -> None:  # noqa: ANN001
    _, events = list_task_events(db_session, task_id=seeded_task_id, since_id=0)
    ids = [event.id for event in events]
    assert ids == sorted(ids)
