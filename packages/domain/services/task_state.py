from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from packages.domain.models import TaskRunRecord, TaskStepRecord
from packages.domain.services.task_events import append_task_event

TASK_STATUS_DRAFT = "draft"
TASK_STATUS_AWAITING_APPROVAL = "awaiting_approval"
TASK_STATUS_APPROVED = "approved"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_WAITING_CLARIFICATION = "waiting_clarification"

STEP_STATUS_PENDING = "pending"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_SUCCESS = "success"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"

STEP_SEQUENCE = [
    "plan_task",
    "normalize_aoi",
    "search_candidates",
    "recommend_dataset",
    "run_ndvi_pipeline",
    "generate_outputs",
]

TASK_STATUS_TRANSITIONS: dict[str, set[str]] = {
    TASK_STATUS_DRAFT: {
        TASK_STATUS_QUEUED,
        TASK_STATUS_WAITING_CLARIFICATION,
        TASK_STATUS_AWAITING_APPROVAL,
    },
    TASK_STATUS_WAITING_CLARIFICATION: {
        TASK_STATUS_QUEUED,
        TASK_STATUS_FAILED,
        TASK_STATUS_CANCELLED,
        TASK_STATUS_AWAITING_APPROVAL,
    },
    TASK_STATUS_AWAITING_APPROVAL: {
        TASK_STATUS_APPROVED,
        TASK_STATUS_CANCELLED,
        TASK_STATUS_FAILED,
    },
    TASK_STATUS_APPROVED: {
        TASK_STATUS_QUEUED,
        TASK_STATUS_CANCELLED,
        TASK_STATUS_FAILED,
    },
    TASK_STATUS_QUEUED: {
        TASK_STATUS_RUNNING,
        TASK_STATUS_FAILED,
        TASK_STATUS_CANCELLED,
        TASK_STATUS_WAITING_CLARIFICATION,
    },
    TASK_STATUS_RUNNING: {
        TASK_STATUS_SUCCESS,
        TASK_STATUS_FAILED,
        TASK_STATUS_CANCELLED,
    },
    TASK_STATUS_SUCCESS: set(),
    TASK_STATUS_FAILED: set(),
    TASK_STATUS_CANCELLED: set(),
}

STEP_STATUS_TRANSITIONS: dict[str, set[str]] = {
    STEP_STATUS_PENDING: {STEP_STATUS_RUNNING, STEP_STATUS_SKIPPED},
    STEP_STATUS_RUNNING: {STEP_STATUS_SUCCESS, STEP_STATUS_FAILED, STEP_STATUS_SKIPPED},
    STEP_STATUS_SUCCESS: set(),
    STEP_STATUS_FAILED: set(),
    STEP_STATUS_SKIPPED: set(),
}


def can_transition(mapping: dict[str, set[str]], current: str, target: str) -> bool:
    if current == target:
        return True
    return target in mapping.get(current, set())


def ensure_task_status_transition(current: str, target: str) -> None:
    if not can_transition(TASK_STATUS_TRANSITIONS, current, target):
        raise ValueError(f"Invalid task status transition: {current} -> {target}")


def ensure_step_status_transition(current: str, target: str) -> None:
    if not can_transition(STEP_STATUS_TRANSITIONS, current, target):
        raise ValueError(f"Invalid step status transition: {current} -> {target}")


def ensure_task_steps(db: Session, task: TaskRunRecord, step_names: list[str]) -> None:
    existing = {step.step_name for step in task.steps}
    for step_name in step_names:
        if step_name not in existing:
            db.add(TaskStepRecord(task_id=task.id, step_name=step_name, status=STEP_STATUS_PENDING))
    db.flush()


def set_task_status(
    db: Session,
    task: TaskRunRecord,
    status: str,
    *,
    current_step: str | None = None,
    event_type: str | None = None,
    detail: dict | None = None,
    force: bool = False,
) -> None:
    current = task.status or TASK_STATUS_DRAFT
    if not force:
        ensure_task_status_transition(current, status)
    task.status = status
    if current_step is not None:
        task.current_step = current_step
    db.flush()
    if event_type is not None:
        append_task_event(
            db,
            task_id=task.id,
            event_type=event_type,
            step_name=current_step,
            status=status,
            detail=detail,
        )


def set_step_status(
    db: Session,
    task: TaskRunRecord,
    step_name: str,
    status: str,
    *,
    detail: dict | None = None,
) -> None:
    step = next((item for item in task.steps if item.step_name == step_name), None)
    if step is None:
        step = TaskStepRecord(task_id=task.id, step_name=step_name, status=STEP_STATUS_PENDING)
        db.add(step)
        db.flush()
        task.steps.append(step)

    previous_status = step.status or STEP_STATUS_PENDING
    ensure_step_status_transition(previous_status, status)

    now = datetime.now(timezone.utc)
    if status == STEP_STATUS_RUNNING and step.started_at is None:
        step.started_at = now
    if status in {STEP_STATUS_SUCCESS, STEP_STATUS_FAILED, STEP_STATUS_SKIPPED}:
        step.ended_at = now

    step.status = status
    step.detail_json = detail
    task.current_step = step_name
    db.flush()

    if previous_status != status:
        append_task_event(
            db,
            task_id=task.id,
            event_type="step_status_changed",
            step_name=step_name,
            status=status,
            detail=detail,
        )


def write_task_error(
    task: TaskRunRecord,
    *,
    error_code: str,
    error_message: str,
) -> None:
    task.error_code = error_code
    task.error_message = error_message
