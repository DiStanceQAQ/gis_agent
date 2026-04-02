from __future__ import annotations

from sqlalchemy.orm import Session

from packages.domain.models import TaskEventRecord, TaskRunRecord


def append_task_event(
    db: Session,
    *,
    task_id: str,
    event_type: str,
    step_name: str | None = None,
    status: str | None = None,
    detail: dict | None = None,
) -> TaskEventRecord:
    event = TaskEventRecord(
        task_id=task_id,
        event_type=event_type,
        step_name=step_name,
        status=status,
        detail_json=detail,
    )
    db.add(event)
    db.flush()
    return event


def list_task_events(db: Session, *, task_id: str, since_id: int = 0) -> tuple[TaskRunRecord, list[TaskEventRecord]]:
    task = db.get(TaskRunRecord, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    events = (
        db.query(TaskEventRecord)
        .filter(TaskEventRecord.task_id == task_id, TaskEventRecord.id > since_id)
        .order_by(TaskEventRecord.id.asc())
        .all()
    )
    return task, events
