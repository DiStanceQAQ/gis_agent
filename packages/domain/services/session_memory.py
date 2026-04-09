from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from packages.domain.models import (
    MessageUnderstandingRecord,
    SessionMemoryEventRecord,
    SessionStateSnapshotRecord,
    TaskSpecRevisionRecord,
)
from packages.domain.utils import make_id


class SessionMemoryService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def record_event(
        self,
        *,
        session_id: str,
        event_type: str,
        message_id: str | None = None,
        task_id: str | None = None,
        revision_id: str | None = None,
        event_payload: dict[str, object] | None = None,
    ) -> SessionMemoryEventRecord:
        event = SessionMemoryEventRecord(
            id=make_id("evt"),
            session_id=session_id,
            message_id=message_id,
            task_id=task_id,
            revision_id=revision_id,
            event_type=event_type,
            event_payload_json=dict(event_payload or {}),
        )
        self._db.add(event)
        self._db.flush()
        self.refresh_snapshot(
            session_id=session_id,
            task_id=task_id,
            revision_id=revision_id,
        )
        return event

    def get_latest_snapshot(self, session_id: str) -> SessionStateSnapshotRecord | None:
        return (
            self._db.query(SessionStateSnapshotRecord)
            .filter(SessionStateSnapshotRecord.session_id == session_id)
            .one_or_none()
        )

    def refresh_snapshot(
        self,
        session_id: str,
        *,
        task_id: str | None = None,
        revision_id: str | None = None,
        understanding_id: str | None = None,
    ) -> SessionStateSnapshotRecord:
        snapshot = self.get_latest_snapshot(session_id)
        if snapshot is None:
            snapshot = SessionStateSnapshotRecord(
                id=make_id("snap"),
                session_id=session_id,
                created_at=datetime.now(timezone.utc),
            )
            self._db.add(snapshot)

        revision = self._resolve_revision(task_id=task_id, revision_id=revision_id)
        latest_understanding_id = understanding_id or self._resolve_latest_understanding_id(
            session_id=session_id
        )

        if revision is not None:
            snapshot.active_task_id = revision.task_id
            snapshot.active_revision_id = revision.id
            snapshot.active_revision_number = revision.revision_number
            raw_spec = dict(revision.raw_spec_json or {})
            snapshot.open_missing_fields_json = list(raw_spec.get("missing_fields", []) or [])
            snapshot.blocked_reason = (
                revision.execution_blocked_reason
                or dict(revision.response_payload_json or {}).get("blocked_reason")
            )
            snapshot.field_history_rollup_json = dict(revision.field_confidences_json or {})
            snapshot.user_preference_profile_json = {
                "preferred_output": list(raw_spec.get("preferred_output", []) or []),
                "user_priority": raw_spec.get("user_priority"),
            }
            snapshot.risk_profile_json = {"execution_blocked": bool(revision.execution_blocked)}
        else:
            snapshot.active_task_id = task_id
            snapshot.active_revision_id = revision_id

        snapshot.active_understanding_id = latest_understanding_id
        snapshot.snapshot_version = (snapshot.snapshot_version or 0) + 1
        self._db.flush()
        return snapshot

    def _resolve_revision(
        self,
        *,
        task_id: str | None,
        revision_id: str | None,
    ) -> TaskSpecRevisionRecord | None:
        if revision_id is not None:
            return self._db.get(TaskSpecRevisionRecord, revision_id)
        if task_id is None:
            return None
        return (
            self._db.query(TaskSpecRevisionRecord)
            .filter(
                TaskSpecRevisionRecord.task_id == task_id,
                TaskSpecRevisionRecord.is_active.is_(True),
            )
            .order_by(TaskSpecRevisionRecord.revision_number.desc())
            .first()
        )

    def _resolve_latest_understanding_id(self, *, session_id: str) -> str | None:
        latest = (
            self._db.query(MessageUnderstandingRecord)
            .filter(MessageUnderstandingRecord.session_id == session_id)
            .order_by(
                MessageUnderstandingRecord.created_at.desc(),
                MessageUnderstandingRecord.id.desc(),
            )
            .first()
        )
        return latest.id if latest is not None else None
