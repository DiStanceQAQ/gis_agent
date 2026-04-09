from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from packages.domain.models import (
    MessageUnderstandingRecord,
    SessionMemoryEventRecord,
    SessionMemoryLinkRecord,
    SessionMemorySummaryRecord,
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

    def link_entities(
        self,
        *,
        session_id: str,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        link_type: str,
        weight: float | None = None,
        payload: dict[str, object] | None = None,
    ) -> SessionMemoryLinkRecord:
        existing = (
            self._db.query(SessionMemoryLinkRecord)
            .filter(
                SessionMemoryLinkRecord.session_id == session_id,
                SessionMemoryLinkRecord.source_type == source_type,
                SessionMemoryLinkRecord.source_id == source_id,
                SessionMemoryLinkRecord.target_type == target_type,
                SessionMemoryLinkRecord.target_id == target_id,
                SessionMemoryLinkRecord.link_type == link_type,
            )
            .one_or_none()
        )
        if existing is not None:
            existing.weight = weight
            existing.payload_json = dict(payload or {})
            self._db.flush()
            return existing

        record = SessionMemoryLinkRecord(
            id=make_id("lnk"),
            session_id=session_id,
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            link_type=link_type,
            weight=weight,
            payload_json=dict(payload or {}),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def list_links_for_target(
        self,
        *,
        target_type: str,
        target_id: str,
        session_id: str | None = None,
    ) -> list[SessionMemoryLinkRecord]:
        query = self._db.query(SessionMemoryLinkRecord).filter(
            SessionMemoryLinkRecord.target_type == target_type,
            SessionMemoryLinkRecord.target_id == target_id,
        )
        if session_id is not None:
            query = query.filter(SessionMemoryLinkRecord.session_id == session_id)
        return (
            query.order_by(
                SessionMemoryLinkRecord.created_at.desc(),
                SessionMemoryLinkRecord.id.desc(),
            )
            .all()
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
                snapshot_version=0,
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
            snapshot.field_history_rollup_json = self._build_field_history_rollup(task_id=revision.task_id)
            snapshot.user_preference_profile_json = {
                "preferred_output": list(raw_spec.get("preferred_output", []) or []),
                "user_priority": raw_spec.get("user_priority"),
                "confirmation_preference": self._derive_confirmation_preference(raw_spec),
            }
            task_risk = self._derive_task_risk(revision)
            snapshot.risk_profile_json = {
                "execution_blocked": bool(revision.execution_blocked),
                "task_risk": task_risk,
                "risk_level": task_risk,
            }
        else:
            snapshot.active_task_id = task_id
            snapshot.active_revision_id = revision_id

        snapshot.active_understanding_id = latest_understanding_id
        summary = self._upsert_rolling_summary(session_id=session_id, snapshot=snapshot, revision=revision)
        snapshot.latest_summary_id = summary.id if summary is not None else None
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

    def _build_field_history_rollup(self, *, task_id: str) -> dict[str, object]:
        revisions = (
            self._db.query(TaskSpecRevisionRecord)
            .filter(TaskSpecRevisionRecord.task_id == task_id)
            .order_by(TaskSpecRevisionRecord.revision_number.asc(), TaskSpecRevisionRecord.created_at.asc())
            .all()
        )
        rollup: dict[str, dict[str, object]] = {}
        if not revisions:
            return rollup

        for revision in revisions:
            raw_spec = dict(revision.raw_spec_json or {})
            confidence_payload = dict(revision.field_confidences_json or {})
            for field in ("aoi_input", "aoi_source_type", "time_range", "analysis_type"):
                value = raw_spec.get(field)
                if value is None:
                    continue
                bucket = rollup.setdefault(
                    field,
                    {
                        "accepted_count": 0,
                        "correction_count": 0,
                        "latest_value": None,
                        "previous_values": [],
                        "latest_revision_id": None,
                    },
                )
                previous_value = bucket.get("latest_value")
                bucket["accepted_count"] = int(bucket.get("accepted_count", 0)) + 1
                if previous_value is not None and previous_value != value:
                    bucket["correction_count"] = int(bucket.get("correction_count", 0)) + 1
                    previous_values = list(bucket.get("previous_values", []))
                    if previous_value not in previous_values:
                        previous_values.append(previous_value)
                    bucket["previous_values"] = previous_values[-5:]
                bucket["latest_value"] = value
                bucket["latest_revision_id"] = revision.id

                confidence = confidence_payload.get(field)
                if isinstance(confidence, dict):
                    score = confidence.get("score")
                    if isinstance(score, (int, float)):
                        bucket["latest_score"] = float(score)
                    level = confidence.get("level")
                    if isinstance(level, str) and level.strip():
                        bucket["latest_level"] = level

        return rollup

    def _upsert_rolling_summary(
        self,
        *,
        session_id: str,
        snapshot: SessionStateSnapshotRecord,
        revision: TaskSpecRevisionRecord | None,
    ) -> SessionMemorySummaryRecord | None:
        if revision is None:
            return None

        summary = None
        if isinstance(snapshot.latest_summary_id, str) and snapshot.latest_summary_id:
            summary = self._db.get(SessionMemorySummaryRecord, snapshot.latest_summary_id)
        if summary is None:
            summary = SessionMemorySummaryRecord(
                id=make_id("sum"),
                session_id=session_id,
                summary_kind="rolling_context",
            )
            self._db.add(summary)

        raw_spec = dict(revision.raw_spec_json or {})
        summary.summary_text = self._build_summary_text(
            revision=revision,
            raw_spec=raw_spec,
            snapshot=snapshot,
        )
        summary.summary_json = {
            "active_task_id": revision.task_id,
            "active_revision_id": revision.id,
            "active_revision_number": revision.revision_number,
            "analysis_type": raw_spec.get("analysis_type"),
            "aoi_input": raw_spec.get("aoi_input"),
            "aoi_source_type": raw_spec.get("aoi_source_type"),
            "missing_fields": list(snapshot.open_missing_fields_json or []),
            "blocked_reason": snapshot.blocked_reason,
            "preferred_output": list(raw_spec.get("preferred_output", []) or []),
            "user_priority": raw_spec.get("user_priority"),
            "field_history_rollup": dict(snapshot.field_history_rollup_json or {}),
        }
        summary.source_event_range_json = self._build_source_event_range(session_id=session_id)
        self._db.flush()
        return summary

    def _build_summary_text(
        self,
        *,
        revision: TaskSpecRevisionRecord,
        raw_spec: dict[str, object],
        snapshot: SessionStateSnapshotRecord,
    ) -> str:
        parts: list[str] = []
        if revision.understanding_summary:
            parts.append(str(revision.understanding_summary).strip())
        analysis_type = raw_spec.get("analysis_type")
        if isinstance(analysis_type, str) and analysis_type.strip():
            parts.append(f"当前任务类型为 {analysis_type}")
        preferred_output = list(raw_spec.get("preferred_output", []) or [])
        if preferred_output:
            parts.append(f"偏好输出为 {'、'.join(str(item) for item in preferred_output)}")
        if snapshot.blocked_reason:
            parts.append(f"当前仍被阻塞：{snapshot.blocked_reason}")
        missing_fields = list(snapshot.open_missing_fields_json or [])
        if missing_fields:
            parts.append(f"待补字段：{'、'.join(str(field) for field in missing_fields)}")
        return "；".join(part for part in parts if part) or "会话记忆已更新。"

    def _build_source_event_range(self, *, session_id: str) -> dict[str, object]:
        events = (
            self._db.query(SessionMemoryEventRecord)
            .filter(SessionMemoryEventRecord.session_id == session_id)
            .order_by(SessionMemoryEventRecord.created_at.asc(), SessionMemoryEventRecord.id.asc())
            .all()
        )
        if not events:
            return {}
        return {
            "start_event_id": events[0].id,
            "end_event_id": events[-1].id,
            "event_count": len(events),
        }

    def _derive_confirmation_preference(self, raw_spec: dict[str, object]) -> str:
        user_priority = str(raw_spec.get("user_priority") or "").strip().lower()
        if user_priority == "speed":
            return "prefer_execute"
        if user_priority in {"quality", "careful", "safe"}:
            return "always_confirm"
        return "balanced"

    def _derive_task_risk(self, revision: TaskSpecRevisionRecord) -> str:
        if revision.execution_blocked:
            return "high"
        raw_spec = dict(revision.raw_spec_json or {})
        analysis_type = str(raw_spec.get("analysis_type") or "WORKFLOW").strip().upper()
        if analysis_type in {"WORKFLOW", "BAND_MATH"}:
            return "medium"
        return "low"
