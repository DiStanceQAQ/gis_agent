from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re

from sqlalchemy.orm import Session

from packages.domain.models import MessageRecord, TaskSpecRevisionRecord, UploadedFileRecord


UPLOAD_HINT_PATTERN = re.compile(
    r"(上传|文件|边界|boundary|shp|geojson|gpkg|矢量)",
    re.IGNORECASE,
)
CORRECTION_HINT_PATTERN = re.compile(
    r"(不是|改成|改为|换成|换到|改一下|调整|修正|纠正|更正|重新|改回|换回)",
    re.IGNORECASE,
)
TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fff\.]+", re.UNICODE)
TRACKED_HISTORY_FIELDS = ("aoi_input", "time_range", "analysis_type")


@dataclass(slots=True)
class SessionMemoryRetrievalResult:
    messages: list[dict[str, object]] = field(default_factory=list)
    revisions: list[dict[str, object]] = field(default_factory=list)
    uploads: list[dict[str, object]] = field(default_factory=list)
    field_value_history: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    trace: dict[str, object] = field(default_factory=dict)


class SessionMemoryRetrievalService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def retrieve(
        self,
        *,
        session_id: str,
        message_id: str,
        message_content: str,
        snapshot: object | None,
        preferred_file_ids: list[str] | None = None,
    ) -> SessionMemoryRetrievalResult:
        current_message = self._load_message(session_id=session_id, message_id=message_id)
        current_message_created_at = current_message.created_at if current_message is not None else None
        active_task_id = getattr(snapshot, "active_task_id", None) if snapshot is not None else None
        active_revision_id = getattr(snapshot, "active_revision_id", None) if snapshot is not None else None

        revisions = self._load_revision_candidates(
            session_id=session_id,
            active_task_id=active_task_id,
            active_revision_id=active_revision_id,
        )
        uploads = self._load_upload_candidates(
            session_id=session_id,
            before_created_at=current_message_created_at,
            preferred_file_ids=preferred_file_ids or [],
        )
        messages = self._load_message_candidates(
            session_id=session_id,
            message_id=message_id,
            before_created_at=current_message_created_at,
            active_task_id=active_task_id,
            revisions=revisions,
        )

        ranked_revisions, revision_scores = self._rank_revisions(
            revisions=revisions,
            active_task_id=active_task_id,
            active_revision_id=active_revision_id,
            message_content=message_content,
        )
        ranked_uploads, upload_scores = self._rank_uploads(
            uploads=uploads,
            preferred_file_ids=preferred_file_ids or [],
            message_content=message_content,
            active_revision=ranked_revisions[0] if ranked_revisions else None,
            current_message_created_at=current_message_created_at,
        )
        ranked_messages, message_scores = self._rank_messages(
            messages=messages,
            active_task_id=active_task_id,
            active_revision=ranked_revisions[0] if ranked_revisions else None,
            message_content=message_content,
            current_message_created_at=current_message_created_at,
        )

        field_value_history = self._build_field_value_history(ranked_revisions)
        return SessionMemoryRetrievalResult(
            messages=[self._serialize_message(record) for record in ranked_messages[:5]],
            revisions=[self._serialize_revision(record) for record in ranked_revisions[:5]],
            uploads=[self._serialize_upload(record) for record in ranked_uploads[:5]],
            field_value_history=field_value_history,
            trace={
                "selected_message_ids": [record.id for record in ranked_messages[:5]],
                "selected_upload_ids": [record.id for record in ranked_uploads[:5]],
                "selected_revision_ids": [record.id for record in ranked_revisions[:5]],
                "scoring": {
                    "messages": message_scores[:5],
                    "uploads": upload_scores[:5],
                    "revisions": revision_scores[:5],
                },
            },
        )

    def _load_message(self, *, session_id: str, message_id: str) -> MessageRecord | None:
        message = self._db.get(MessageRecord, message_id)
        if message is None or message.session_id != session_id:
            return None
        return message

    def _load_revision_candidates(
        self,
        *,
        session_id: str,
        active_task_id: str | None,
        active_revision_id: str | None,
    ) -> list[TaskSpecRevisionRecord]:
        selected: list[TaskSpecRevisionRecord] = []
        selected_ids: set[str] = set()

        def add(record: TaskSpecRevisionRecord | None) -> None:
            if record is None or record.id in selected_ids:
                return
            selected.append(record)
            selected_ids.add(record.id)

        if active_revision_id:
            add(self._db.get(TaskSpecRevisionRecord, active_revision_id))

        if active_task_id:
            for record in (
                self._db.query(TaskSpecRevisionRecord)
                .filter(TaskSpecRevisionRecord.task_id == active_task_id)
                .order_by(
                    TaskSpecRevisionRecord.revision_number.desc(),
                    TaskSpecRevisionRecord.created_at.desc(),
                )
                .limit(8)
                .all()
            ):
                add(record)

        for record in (
            self._db.query(TaskSpecRevisionRecord)
            .join(TaskSpecRevisionRecord.task)
            .filter(TaskSpecRevisionRecord.task.has(session_id=session_id))
            .order_by(TaskSpecRevisionRecord.created_at.desc(), TaskSpecRevisionRecord.id.desc())
            .limit(8)
            .all()
        ):
            add(record)

        return selected

    def _load_upload_candidates(
        self,
        *,
        session_id: str,
        before_created_at: datetime | None,
        preferred_file_ids: list[str],
    ) -> list[UploadedFileRecord]:
        query = self._db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id)
        if before_created_at is not None:
            query = query.filter(UploadedFileRecord.created_at <= before_created_at)
        found = (
            query.order_by(UploadedFileRecord.created_at.desc(), UploadedFileRecord.id.desc())
            .limit(12)
            .all()
        )
        if not preferred_file_ids:
            return found

        preferred_lookup = {record.id: record for record in found}
        for record in (
            self._db.query(UploadedFileRecord)
            .filter(
                UploadedFileRecord.session_id == session_id,
                UploadedFileRecord.id.in_(preferred_file_ids),
            )
            .all()
        ):
            preferred_lookup[record.id] = record

        ordered: list[UploadedFileRecord] = []
        seen_ids: set[str] = set()
        for file_id in preferred_file_ids:
            record = preferred_lookup.get(file_id)
            if record is not None and record.id not in seen_ids:
                ordered.append(record)
                seen_ids.add(record.id)
        for record in found:
            if record.id in seen_ids:
                continue
            ordered.append(record)
            seen_ids.add(record.id)
        return ordered

    def _load_message_candidates(
        self,
        *,
        session_id: str,
        message_id: str,
        before_created_at: datetime | None,
        active_task_id: str | None,
        revisions: list[TaskSpecRevisionRecord],
    ) -> list[MessageRecord]:
        selected: list[MessageRecord] = []
        selected_ids: set[str] = set()

        def add(record: MessageRecord | None) -> None:
            if record is None or record.id in selected_ids:
                return
            selected.append(record)
            selected_ids.add(record.id)

        for revision in revisions:
            add(self._load_message(session_id=session_id, message_id=revision.source_message_id))

        if active_task_id:
            query = self._db.query(MessageRecord).filter(
                MessageRecord.session_id == session_id,
                MessageRecord.id != message_id,
                MessageRecord.linked_task_id == active_task_id,
            )
            if before_created_at is not None:
                query = query.filter(MessageRecord.created_at <= before_created_at)
            for record in (
                query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
                .limit(6)
                .all()
            ):
                add(record)

        nearby_query = self._db.query(MessageRecord).filter(
            MessageRecord.session_id == session_id,
            MessageRecord.id != message_id,
        )
        if before_created_at is not None:
            nearby_query = nearby_query.filter(MessageRecord.created_at <= before_created_at)
        for record in (
            nearby_query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
            .limit(8)
            .all()
        ):
            add(record)

        return selected

    def _rank_revisions(
        self,
        *,
        revisions: list[TaskSpecRevisionRecord],
        active_task_id: str | None,
        active_revision_id: str | None,
        message_content: str,
    ) -> tuple[list[TaskSpecRevisionRecord], list[dict[str, object]]]:
        scored: list[tuple[float, TaskSpecRevisionRecord, dict[str, object]]] = []
        message_tokens = _tokenize(message_content)
        correction_hint = bool(CORRECTION_HINT_PATTERN.search(message_content))

        for revision in revisions:
            structural = 0.0
            evidence: list[str] = []
            if revision.id == active_revision_id:
                structural += 1.0
                evidence.append("matches active revision")
            if active_task_id and revision.task_id == active_task_id:
                structural += 0.7
                evidence.append("belongs to active task")
            semantic = _overlap_score(
                message_tokens,
                _tokenize(revision.understanding_summary or "")
                | _tokenize(_raw_spec_text(revision.raw_spec_json or {})),
            )
            if semantic:
                evidence.append("text overlaps with revision summary/spec")
            recency = _recency_decay(revision.created_at)
            if recency:
                evidence.append("recent revision")
            correction_bonus = 0.5 if correction_hint and revision.task_id == active_task_id else 0.0
            if correction_bonus:
                evidence.append("correction message anchored to active task")
            signal_scores = {
                "structural_relevance": structural,
                "semantic_text_hint": semantic,
                "recency_decay": recency,
                "correction_link_bonus": correction_bonus,
            }
            total = sum(signal_scores.values())
            scored.append((total, revision, _score_payload(revision.id, signal_scores, evidence)))

        scored.sort(key=lambda item: (item[0], item[1].revision_number, _to_ts(item[1].created_at)), reverse=True)
        return [item[1] for item in scored], [item[2] for item in scored]

    def _rank_uploads(
        self,
        *,
        uploads: list[UploadedFileRecord],
        preferred_file_ids: list[str],
        message_content: str,
        active_revision: TaskSpecRevisionRecord | None,
        current_message_created_at: datetime | None,
    ) -> tuple[list[UploadedFileRecord], list[dict[str, object]]]:
        scored: list[tuple[float, UploadedFileRecord, dict[str, object]]] = []
        message_tokens = _tokenize(message_content)
        correction_hint = bool(CORRECTION_HINT_PATTERN.search(message_content))
        upload_hint = bool(UPLOAD_HINT_PATTERN.search(message_content))
        active_spec = dict(active_revision.raw_spec_json or {}) if active_revision is not None else {}

        for upload in uploads:
            structural = 0.0
            evidence: list[str] = []
            if upload.id in preferred_file_ids:
                structural += 1.0
                evidence.append("preferred file id requested")
            if active_spec.get("aoi_source_type") == "file_upload" and upload.file_type in {"shp", "geojson", "gpkg"}:
                structural += 0.8
                evidence.append("matches active revision file AOI type")
            semantic = _overlap_score(
                message_tokens,
                _tokenize(upload.original_name) | _tokenize(upload.file_type),
            )
            if upload_hint and upload.file_type in {"shp", "geojson", "gpkg"}:
                semantic += 0.35
                evidence.append("message explicitly references uploaded boundary file")
            if semantic and "message explicitly references uploaded boundary file" not in evidence:
                evidence.append("text overlaps with upload name/type")
            recency = _recency_decay(upload.created_at, current_message_created_at)
            if recency:
                evidence.append("recent upload")
            correction_bonus = 0.45 if correction_hint and active_spec.get("aoi_source_type") == "file_upload" else 0.0
            if correction_bonus:
                evidence.append("correction points back to uploaded AOI")
            signal_scores = {
                "structural_relevance": structural,
                "semantic_text_hint": semantic,
                "recency_decay": recency,
                "correction_link_bonus": correction_bonus,
            }
            total = sum(signal_scores.values())
            scored.append((total, upload, _score_payload(upload.id, signal_scores, evidence)))

        scored.sort(key=lambda item: (item[0], _to_ts(item[1].created_at)), reverse=True)
        return [item[1] for item in scored], [item[2] for item in scored]

    def _rank_messages(
        self,
        *,
        messages: list[MessageRecord],
        active_task_id: str | None,
        active_revision: TaskSpecRevisionRecord | None,
        message_content: str,
        current_message_created_at: datetime | None,
    ) -> tuple[list[MessageRecord], list[dict[str, object]]]:
        scored: list[tuple[float, MessageRecord, dict[str, object]]] = []
        message_tokens = _tokenize(message_content)
        correction_hint = bool(CORRECTION_HINT_PATTERN.search(message_content))

        for record in messages:
            structural = 0.0
            evidence: list[str] = []
            if active_revision is not None and record.id == active_revision.source_message_id:
                structural += 1.0
                evidence.append("source message for active revision")
            if active_task_id and record.linked_task_id == active_task_id:
                structural += 0.8
                evidence.append("linked to active task")
            semantic = _overlap_score(message_tokens, _tokenize(record.content))
            if semantic:
                evidence.append("text overlaps with current message")
            recency = _recency_decay(record.created_at, current_message_created_at)
            if recency:
                evidence.append("recent conversation context")
            correction_bonus = 0.4 if correction_hint and record.role == "user" else 0.0
            if correction_bonus:
                evidence.append("user correction likely refers back to prior user context")
            signal_scores = {
                "structural_relevance": structural,
                "semantic_text_hint": semantic,
                "recency_decay": recency,
                "correction_link_bonus": correction_bonus,
            }
            total = sum(signal_scores.values())
            scored.append((total, record, _score_payload(record.id, signal_scores, evidence)))

        scored.sort(key=lambda item: (item[0], _to_ts(item[1].created_at)), reverse=True)
        return [item[1] for item in scored], [item[2] for item in scored]

    def _build_field_value_history(
        self,
        revisions: list[TaskSpecRevisionRecord],
    ) -> dict[str, list[dict[str, object]]]:
        history: dict[str, list[dict[str, object]]] = {field: [] for field in TRACKED_HISTORY_FIELDS}
        for revision in revisions:
            raw_spec = dict(revision.raw_spec_json or {})
            for field_name in TRACKED_HISTORY_FIELDS:
                if field_name not in raw_spec:
                    continue
                history[field_name].append(
                    {
                        "revision_id": revision.id,
                        "revision_number": revision.revision_number,
                        "value": raw_spec[field_name],
                    }
                )
        return history

    @staticmethod
    def _serialize_message(record: MessageRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "role": record.role,
            "content": record.content,
            "linked_task_id": record.linked_task_id,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @staticmethod
    def _serialize_upload(record: UploadedFileRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "original_name": record.original_name,
            "file_type": record.file_type,
            "storage_key": record.storage_key,
            "size_bytes": record.size_bytes,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @staticmethod
    def _serialize_revision(record: TaskSpecRevisionRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "task_id": record.task_id,
            "revision_number": record.revision_number,
            "understanding_summary": record.understanding_summary,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }


def _tokenize(value: str | None) -> set[str]:
    if not value:
        return set()
    return {token.lower() for token in TOKEN_PATTERN.findall(value) if token.strip()}


def _raw_spec_text(raw_spec: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in raw_spec.items():
        if isinstance(value, dict):
            parts.append(f"{key} {' '.join(str(item) for item in value.values())}")
        elif isinstance(value, list):
            parts.append(f"{key} {' '.join(str(item) for item in value)}")
        else:
            parts.append(f"{key} {value}")
    return " ".join(parts)


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    if overlap == 0:
        return 0.0
    return min(1.0, overlap / max(1, min(len(left), len(right))))


def _recency_decay(
    created_at: datetime | None,
    reference_time: datetime | None = None,
) -> float:
    if created_at is None:
        return 0.0
    now = reference_time or datetime.now(timezone.utc)
    age_seconds = max(0.0, (now - created_at).total_seconds())
    return math.exp(-age_seconds / 3600.0)


def _to_ts(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return value.timestamp()


def _score_payload(
    item_id: str,
    signal_scores: dict[str, float],
    evidence: list[str],
) -> dict[str, object]:
    return {
        "id": item_id,
        "total_score": round(sum(signal_scores.values()), 6),
        "signal_scores": {key: round(value, 6) for key, value in signal_scores.items()},
        "evidence": evidence,
    }
