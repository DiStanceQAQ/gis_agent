from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload

from packages.domain.errors import AppError, ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import (
    MessageRecord,
    MessageUnderstandingRecord,
    SessionMemoryEventRecord,
    SessionMemoryLinkRecord,
    SessionMemorySummaryRecord,
    SessionRecord,
    SessionStateSnapshotRecord,
    TaskRunRecord,
    TaskSpecRevisionRecord,
)
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.task_revisions import ensure_initial_revision_for_task
from packages.domain.utils import make_id
from packages.schemas.session import (
    SessionMemoryLineageItemResponse,
    SessionMemoryResponse,
    SessionMessageItemResponse,
    SessionMessagesResponse,
    SessionResponse,
    SessionTaskItemResponse,
    SessionTasksResponse,
)

logger = get_logger(__name__)


def create_session(db: Session) -> SessionResponse:
    record = SessionRecord(id=make_id("ses"), status="active")
    db.add(record)
    db.flush()
    logger.info("session.created session_id=%s", record.id)
    return SessionResponse(session_id=record.id, status=record.status)


def list_session_tasks(db: Session, session_id: str, limit: int = 8) -> SessionTasksResponse:
    session = db.get(SessionRecord, session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": session_id},
        )

    tasks = (
        db.query(TaskRunRecord)
        .options(selectinload(TaskRunRecord.task_spec))
        .filter(TaskRunRecord.session_id == session_id)
        .order_by(TaskRunRecord.created_at.desc())
        .limit(limit)
        .all()
    )

    return SessionTasksResponse(
        session_id=session_id,
        tasks=[
            SessionTaskItemResponse(
                task_id=task.id,
                parent_task_id=task.parent_task_id,
                status=task.status,
                current_step=task.current_step,
                analysis_type=task.analysis_type,
                created_at=task.created_at,
                task_spec=task.task_spec.raw_spec_json if task.task_spec else None,
            )
            for task in tasks
        ],
    )


def list_session_messages(
    db: Session,
    session_id: str,
    *,
    limit: int = 20,
    cursor: str | None = None,
) -> SessionMessagesResponse:
    session = db.get(SessionRecord, session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": session_id},
        )

    query = db.query(MessageRecord).filter(MessageRecord.session_id == session_id)
    if cursor:
        cursor_record = (
            db.query(MessageRecord)
            .filter(
                MessageRecord.session_id == session_id,
                MessageRecord.id == cursor,
            )
            .first()
        )
        if cursor_record is None:
            raise AppError.bad_request(
                error_code=ErrorCode.BAD_REQUEST,
                message="Cursor message not found in session.",
                detail={"session_id": session_id, "cursor": cursor},
            )
        query = query.filter(
            or_(
                MessageRecord.created_at < cursor_record.created_at,
                and_(
                    MessageRecord.created_at == cursor_record.created_at,
                    MessageRecord.id < cursor_record.id,
                ),
            )
        )

    rows_desc = (
        query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows_desc) > limit
    rows = list(reversed(rows_desc[:limit]))
    next_cursor = rows[0].id if has_more and rows else None

    return SessionMessagesResponse(
        session_id=session_id,
        next_cursor=next_cursor,
        messages=[
            SessionMessageItemResponse(
                message_id=item.id,
                role=item.role,
                content=item.content,
                linked_task_id=item.linked_task_id,
                created_at=item.created_at,
            )
            for item in rows
        ],
    )


def get_session_memory(
    db: Session,
    *,
    session_id: str,
    limit: int = 20,
) -> SessionMemoryResponse:
    session = db.get(SessionRecord, session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": session_id},
        )

    memory = SessionMemoryService(db)
    snapshot = memory.get_latest_snapshot(session_id)
    if snapshot is None or (
        snapshot.active_task_id is None and snapshot.active_revision_id is None
    ):
        legacy_task, legacy_revision = _load_latest_session_memory_anchor(db, session_id=session_id)
        if legacy_task is not None or legacy_revision is not None:
            snapshot = memory.refresh_snapshot(
                session_id,
                task_id=legacy_task.id if legacy_task is not None else None,
                revision_id=legacy_revision.id if legacy_revision is not None else None,
            )
    latest_summary = (
        db.get(SessionMemorySummaryRecord, snapshot.latest_summary_id)
        if snapshot is not None
        and isinstance(snapshot.latest_summary_id, str)
        and snapshot.latest_summary_id
        else None
    )

    understandings = (
        db.query(MessageUnderstandingRecord)
        .options(selectinload(MessageUnderstandingRecord.message))
        .filter(MessageUnderstandingRecord.session_id == session_id)
        .order_by(
            MessageUnderstandingRecord.created_at.desc(), MessageUnderstandingRecord.id.desc()
        )
        .limit(limit)
        .all()
    )
    revision_ids = {
        row.derived_revision_id
        for row in understandings
        if isinstance(row.derived_revision_id, str) and row.derived_revision_id
    }
    revisions = (
        db.query(TaskSpecRevisionRecord).filter(TaskSpecRevisionRecord.id.in_(revision_ids)).all()
        if revision_ids
        else []
    )
    revisions_by_id = {revision.id: revision for revision in revisions}

    understanding_ids = [row.id for row in understandings]
    links = (
        db.query(SessionMemoryLinkRecord)
        .filter(
            SessionMemoryLinkRecord.session_id == session_id,
            SessionMemoryLinkRecord.source_type == "understanding",
            SessionMemoryLinkRecord.source_id.in_(understanding_ids),
        )
        .order_by(SessionMemoryLinkRecord.created_at.desc(), SessionMemoryLinkRecord.id.desc())
        .all()
        if understanding_ids
        else []
    )
    links_by_source: dict[str, list[SessionMemoryLinkRecord]] = {}
    for link in links:
        links_by_source.setdefault(link.source_id, []).append(link)

    lineage = [
        SessionMemoryLineageItemResponse(
            understanding_id=row.id,
            message_id=row.message_id,
            message_role=row.message.role if row.message is not None else None,
            message_excerpt=_excerpt(row.message.content, limit=120)
            if row.message is not None
            else None,
            intent=row.intent,
            response_mode=row.response_mode,
            derived_revision_id=row.derived_revision_id,
            revision_number=(
                revisions_by_id[row.derived_revision_id].revision_number
                if row.derived_revision_id in revisions_by_id
                else None
            ),
            snapshot_id=row.snapshot_id,
            summary_id=row.summary_id,
            lineage_root_id=row.lineage_root_id,
            created_at=row.created_at,
            link_targets=[
                {
                    "target_type": link.target_type,
                    "target_id": link.target_id,
                    "link_type": link.link_type,
                    "weight": link.weight,
                    "payload": dict(link.payload_json or {}),
                }
                for link in links_by_source.get(row.id, [])
            ],
        )
        for row in understandings
    ]

    events = (
        db.query(SessionMemoryEventRecord)
        .filter(SessionMemoryEventRecord.session_id == session_id)
        .order_by(SessionMemoryEventRecord.created_at.desc(), SessionMemoryEventRecord.id.desc())
        .limit(limit)
        .all()
    )

    return SessionMemoryResponse(
        session_id=session_id,
        snapshot=_serialize_session_memory_snapshot(snapshot),
        latest_summary=_serialize_session_memory_summary(latest_summary),
        lineage=lineage,
        events=[
            {
                "id": event.id,
                "event_type": event.event_type,
                "message_id": event.message_id,
                "task_id": event.task_id,
                "revision_id": event.revision_id,
                "payload": dict(event.event_payload_json or {}),
                "created_at": event.created_at,
            }
            for event in events
        ],
    )


def _serialize_session_memory_snapshot(
    snapshot: SessionStateSnapshotRecord | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "snapshot_id": snapshot.id,
        "active_task_id": snapshot.active_task_id,
        "active_revision_id": snapshot.active_revision_id,
        "active_revision_number": snapshot.active_revision_number,
        "active_understanding_id": snapshot.active_understanding_id,
        "open_missing_fields": list(snapshot.open_missing_fields_json or []),
        "blocked_reason": snapshot.blocked_reason,
        "latest_summary_id": snapshot.latest_summary_id,
        "field_history_rollup": dict(snapshot.field_history_rollup_json or {}),
        "user_preference_profile": dict(snapshot.user_preference_profile_json or {}),
        "risk_profile": dict(snapshot.risk_profile_json or {}),
        "snapshot_version": snapshot.snapshot_version,
        "created_at": snapshot.created_at,
    }


def _serialize_session_memory_summary(
    summary: SessionMemorySummaryRecord | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "summary_id": summary.id,
        "summary_kind": summary.summary_kind,
        "summary_text": summary.summary_text,
        "summary_json": dict(summary.summary_json or {}),
        "source_event_range": dict(summary.source_event_range_json or {}),
        "created_at": summary.created_at,
    }


def _excerpt(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 1, 0)] + "…"


def _load_latest_session_memory_anchor(
    db: Session,
    *,
    session_id: str,
) -> tuple[TaskRunRecord | None, TaskSpecRevisionRecord | None]:
    row = (
        db.query(TaskSpecRevisionRecord, TaskRunRecord)
        .join(TaskRunRecord, TaskRunRecord.id == TaskSpecRevisionRecord.task_id)
        .filter(
            TaskRunRecord.session_id == session_id,
            TaskSpecRevisionRecord.is_active.is_(True),
        )
        .order_by(
            TaskSpecRevisionRecord.created_at.desc(),
            TaskSpecRevisionRecord.revision_number.desc(),
            TaskRunRecord.created_at.desc(),
        )
        .first()
    )
    if row is not None:
        revision, task = row
        return task, revision

    legacy_task = (
        db.query(TaskRunRecord)
        .join(TaskRunRecord.task_spec)
        .filter(TaskRunRecord.session_id == session_id)
        .order_by(TaskRunRecord.created_at.desc(), TaskRunRecord.id.desc())
        .first()
    )
    if legacy_task is None:
        return None, None
    return legacy_task, ensure_initial_revision_for_task(db, legacy_task)
