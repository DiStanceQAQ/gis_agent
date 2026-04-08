from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.domain.models import TaskRunRecord, TaskSpecRecord, TaskSpecRevisionRecord
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec


class UnderstandingLike(Protocol):
    intent: str
    understanding_summary: str | None
    parsed_spec: ParsedTaskSpec | None
    ranked_candidates: dict[str, list[dict[str, object]]]
    trace: dict[str, object]

    def field_confidences_dump(self) -> dict[str, object]: ...


@dataclass(slots=True)
class RevisionActivationResult:
    revision_id: str
    revision_number: int
    task_id: str
    is_new_task: bool
    active_task_status: str


def get_active_revision(db: Session, task_id: str) -> TaskSpecRevisionRecord | None:
    return (
        db.query(TaskSpecRevisionRecord)
        .filter(
            TaskSpecRevisionRecord.task_id == task_id,
            TaskSpecRevisionRecord.is_active.is_(True),
        )
        .one_or_none()
    )


def create_initial_revision(
    db: Session,
    *,
    task: TaskRunRecord,
    understanding: UnderstandingLike,
    source_message_id: str,
    raw_spec_json: dict[str, object] | None = None,
) -> TaskSpecRevisionRecord:
    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=source_message_id,
        change_type="initial_parse",
        is_active=False,
        understanding_intent=understanding.intent,
        understanding_summary=understanding.understanding_summary,
        raw_spec_json=_resolve_revision_raw_spec_json(
            understanding=understanding,
            raw_spec_json=raw_spec_json,
        ),
        field_confidences_json=understanding.field_confidences_dump(),
        ranked_candidates_json=understanding.ranked_candidates,
        understanding_trace_json=understanding.trace,
    )
    db.add(revision)
    db.flush()
    return revision


def create_correction_revision(
    db: Session,
    *,
    task: TaskRunRecord,
    base_revision: TaskSpecRevisionRecord,
    understanding: UnderstandingLike,
    source_message_id: str,
    raw_spec_json: dict[str, object] | None = None,
) -> TaskSpecRevisionRecord:
    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=base_revision.revision_number + 1,
        base_revision_id=base_revision.id,
        source_message_id=source_message_id,
        change_type="correction",
        is_active=False,
        understanding_intent=understanding.intent,
        understanding_summary=understanding.understanding_summary,
        raw_spec_json=_resolve_revision_raw_spec_json(
            understanding=understanding,
            raw_spec_json=raw_spec_json,
        ),
        field_confidences_json=understanding.field_confidences_dump(),
        ranked_candidates_json=understanding.ranked_candidates,
        understanding_trace_json=understanding.trace,
    )
    db.add(revision)
    db.flush()
    return revision


def activate_revision(
    db: Session,
    *,
    task: TaskRunRecord,
    revision: TaskSpecRevisionRecord,
    mirror_legacy_task_spec: bool = True,
) -> RevisionActivationResult:
    locked_task = (
        db.query(TaskRunRecord).filter(TaskRunRecord.id == task.id).with_for_update().one()
    )
    previous_active = (
        db.query(TaskSpecRevisionRecord)
        .filter(
            TaskSpecRevisionRecord.task_id == locked_task.id,
            TaskSpecRevisionRecord.is_active.is_(True),
        )
        .one_or_none()
    )
    db.query(TaskSpecRevisionRecord).filter(
        TaskSpecRevisionRecord.task_id == locked_task.id,
        TaskSpecRevisionRecord.is_active.is_(True),
    ).update({TaskSpecRevisionRecord.is_active: False}, synchronize_session=False)
    revision.is_active = True
    if _is_user_correction_revision(revision):
        revision.user_revision_count = (previous_active.user_revision_count if previous_active else 0) + 1
        revision.user_last_revision_at = datetime.now(timezone.utc)
    locked_task.last_understanding_message_id = revision.source_message_id
    locked_task.last_response_mode = revision.response_mode
    if mirror_legacy_task_spec:
        _mirror_revision_to_task_spec(db, task=locked_task, revision=revision)
    db.flush()
    return RevisionActivationResult(
        revision_id=revision.id,
        revision_number=revision.revision_number,
        task_id=locked_task.id,
        is_new_task=revision.revision_number == 1,
        active_task_status=locked_task.status,
    )


def ensure_initial_revision_for_task(db: Session, task: TaskRunRecord) -> TaskSpecRevisionRecord:
    existing = get_active_revision(db, task.id)
    if existing is not None:
        return existing

    try:
        with db.begin_nested():
            revision = _materialize_initial_revision_from_legacy_task_spec(db, task)
            activate_revision(db, task=task, revision=revision)
        return revision
    except IntegrityError:
        reloaded = get_active_revision(db, task.id)
        if reloaded is None:
            raise
        return reloaded


def _materialize_initial_revision_from_legacy_task_spec(
    db: Session,
    task: TaskRunRecord,
) -> TaskSpecRevisionRecord:
    if task.task_spec is None:
        raise IntegrityError("task_spec_missing", {}, Exception("task spec missing"))

    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=task.last_understanding_message_id or task.user_message_id,
        change_type="legacy_backfill",
        is_active=False,
        understanding_intent="legacy_materialized",
        understanding_summary=None,
        raw_spec_json=dict(task.task_spec.raw_spec_json or {}),
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode=task.last_response_mode,
        understanding_trace_json={},
    )
    inserted = _insert_initial_revision(db, revision)
    return inserted or revision


def _insert_initial_revision(
    db: Session,
    revision: TaskSpecRevisionRecord,
) -> TaskSpecRevisionRecord:
    db.add(revision)
    db.flush()
    return revision


def _mirror_revision_to_task_spec(
    db: Session,
    *,
    task: TaskRunRecord,
    revision: TaskSpecRevisionRecord,
) -> TaskSpecRecord:
    raw_spec = dict(revision.raw_spec_json or {})
    parsed = ParsedTaskSpec(**raw_spec)

    task_spec = db.get(TaskSpecRecord, task.id)
    if task_spec is None:
        task_spec = TaskSpecRecord(task_id=task.id, raw_spec_json=raw_spec)
        db.add(task_spec)
        task.task_spec = task_spec

    task_spec.aoi_input = parsed.aoi_input
    task_spec.aoi_source_type = parsed.aoi_source_type
    task_spec.preferred_output = parsed.preferred_output
    task_spec.user_priority = parsed.user_priority
    task_spec.need_confirmation = parsed.need_confirmation
    task_spec.raw_spec_json = raw_spec
    return task_spec


def _resolve_revision_raw_spec_json(
    *,
    understanding: UnderstandingLike,
    raw_spec_json: dict[str, object] | None,
) -> dict[str, object]:
    if raw_spec_json is not None:
        return dict(raw_spec_json)
    if understanding.parsed_spec is None:
        return {}
    return understanding.parsed_spec.model_dump()


def _is_user_correction_revision(revision: TaskSpecRevisionRecord) -> bool:
    return revision.change_type == "correction"
