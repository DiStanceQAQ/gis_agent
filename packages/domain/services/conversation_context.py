from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re

from sqlalchemy.orm import Session

from packages.domain.models import MessageRecord, TaskRunRecord, TaskSpecRevisionRecord, UploadedFileRecord
from packages.domain.services.aoi import parse_bbox_text
from packages.domain.services.intent import is_task_confirmation_message


UPLOAD_HINT_PATTERN = re.compile(
    r"(上传|文件|边界|boundary|shp|geojson|gpkg|矢量)",
    re.IGNORECASE,
)
BBOX_HINT_PATTERN = re.compile(r"\bbbox\s*\(|\[[^\]]+\]", re.IGNORECASE)
ADMIN_REGION_HINT_PATTERN = re.compile(
    r"(自治区|自治州|特别行政区|省|市|区|县|州|盟|旗|行政区|地区|乡|镇|村)",
    re.IGNORECASE,
)
CORRECTION_HINT_PATTERN = re.compile(
    r"(不是|改成|改为|换成|换到|改一下|调整|修正|纠正|更正|重新|改回|换回)",
    re.IGNORECASE,
)
CONFIRMATION_CONTEXT_PATTERN = re.compile(
    r"(确认|继续|开始执行|补充|clarif|clarification|等待|收到|好的)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ConversationContextBundle:
    session_id: str
    message_id: str
    latest_active_task_id: str | None
    latest_active_revision_id: str | None
    latest_active_revision_summary: str | None
    uploaded_files: list[dict[str, object]]
    relevant_messages: list[dict[str, object]]
    explicit_signals: dict[str, object]
    trace: dict[str, object]


def build_conversation_context(
    db: Session,
    *,
    session_id: str,
    message_id: str,
    message_content: str,
    preferred_file_ids: list[str] | None = None,
) -> ConversationContextBundle:
    current_message = _load_message(db, session_id=session_id, message_id=message_id)
    current_message_created_at = current_message.created_at if current_message else None

    latest_active_task, latest_active_revision = _load_latest_active_revision(db, session_id=session_id)
    latest_active_revision_summary = (
        latest_active_revision.understanding_summary if latest_active_revision else None
    )

    selected_upload_records = _select_recent_uploads(
        db,
        session_id=session_id,
        before_created_at=current_message_created_at,
        preferred_file_ids=preferred_file_ids or [],
    )
    selected_message_records = _select_relevant_messages(
        db,
        session_id=session_id,
        message_id=message_id,
        current_message_created_at=current_message_created_at,
        latest_active_task=latest_active_task,
        latest_active_revision=latest_active_revision,
        message_content=message_content,
    )

    explicit_signals = _build_explicit_signals(
        message_content=message_content,
        latest_active_revision=latest_active_revision,
    )

    trace = {
        "session_id": session_id,
        "message_id": message_id,
        "latest_active_task": (
            {
                "task_id": latest_active_task.id,
                "revision_id": latest_active_revision.id if latest_active_revision else None,
                "reason": "latest session task with an active revision",
            }
            if latest_active_task is not None
            else None
        ),
        "latest_active_revision": (
            {
                "revision_id": latest_active_revision.id,
                "task_id": latest_active_revision.task_id,
                "summary": latest_active_revision.understanding_summary,
                "reason": "most recent active revision in the session",
            }
            if latest_active_revision is not None
            else None
        ),
        "selected_files": [_serialize_upload_trace(record, preferred_file_ids or []) for record in selected_upload_records],
        "selected_messages": [_serialize_message_trace(record, latest_active_task, latest_active_revision, message_id) for record in selected_message_records],
        "signal_evidence": _build_signal_evidence(
            message_content=message_content,
            latest_active_revision=latest_active_revision,
            explicit_signals=explicit_signals,
        ),
    }

    return ConversationContextBundle(
        session_id=session_id,
        message_id=message_id,
        latest_active_task_id=latest_active_task.id if latest_active_task is not None else None,
        latest_active_revision_id=latest_active_revision.id if latest_active_revision is not None else None,
        latest_active_revision_summary=latest_active_revision_summary,
        uploaded_files=[_serialize_uploaded_file(record) for record in selected_upload_records],
        relevant_messages=[_serialize_message(record) for record in selected_message_records],
        explicit_signals=explicit_signals,
        trace=trace,
    )


def _load_message(db: Session, *, session_id: str, message_id: str) -> MessageRecord | None:
    message = db.get(MessageRecord, message_id)
    if message is None or message.session_id != session_id:
        return None
    return message


def _load_latest_active_revision(
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
    if row is None:
        return None, None
    revision, task = row
    return task, revision


def _select_recent_uploads(
    db: Session,
    *,
    session_id: str,
    before_created_at: datetime | None,
    preferred_file_ids: list[str],
    limit: int = 5,
) -> list[UploadedFileRecord]:
    query = db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id)
    if before_created_at is not None:
        query = query.filter(UploadedFileRecord.created_at <= before_created_at)
    recent_uploads = (
        query.order_by(UploadedFileRecord.created_at.desc(), UploadedFileRecord.id.desc())
        .limit(limit)
        .all()
    )
    uploads_by_id = {record.id: record for record in recent_uploads}
    ordered: list[UploadedFileRecord] = []
    seen_ids: set[str] = set()

    for file_id in preferred_file_ids:
        record = uploads_by_id.get(file_id)
        if record is None or record.id in seen_ids:
            continue
        ordered.append(record)
        seen_ids.add(record.id)

    for record in recent_uploads:
        if record.id in seen_ids:
            continue
        ordered.append(record)
        seen_ids.add(record.id)

    return ordered


def _select_relevant_messages(
    db: Session,
    *,
    session_id: str,
    message_id: str,
    current_message_created_at: datetime | None,
    latest_active_task: TaskRunRecord | None,
    latest_active_revision: TaskSpecRevisionRecord | None,
    message_content: str,
) -> list[MessageRecord]:
    selected: list[MessageRecord] = []
    selected_ids: set[str] = set()

    def add(record: MessageRecord | None) -> None:
        if record is None or record.id in selected_ids:
            return
        selected.append(record)
        selected_ids.add(record.id)

    if latest_active_revision is not None:
        add(_load_message(db, session_id=session_id, message_id=latest_active_revision.source_message_id))

    if latest_active_task is not None:
        linked_context_query = (
            db.query(MessageRecord)
            .filter(
                MessageRecord.session_id == session_id,
                MessageRecord.id != message_id,
                MessageRecord.linked_task_id == latest_active_task.id,
            )
        )
        if current_message_created_at is not None:
            linked_context_query = linked_context_query.filter(
                MessageRecord.created_at <= current_message_created_at
            )
        linked_context_messages = (
            linked_context_query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
            .limit(5)
            .all()
        )
        for record in linked_context_messages:
            if _is_task_context_message(record.content):
                add(record)

    nearby_query = db.query(MessageRecord).filter(
        MessageRecord.session_id == session_id,
        MessageRecord.id != message_id,
    )
    if current_message_created_at is not None:
        nearby_query = nearby_query.filter(MessageRecord.created_at <= current_message_created_at)
    nearby_messages = (
        nearby_query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
        .limit(6)
        .all()
    )
    for record in nearby_messages:
        if len(selected) >= 5:
            break
        if record.id in selected_ids:
            continue
        if _should_keep_nearby_message(record, message_content=message_content):
            add(record)

    return selected


def _should_keep_nearby_message(record: MessageRecord, *, message_content: str) -> bool:
    if record.role == "assistant" and _is_task_context_message(record.content):
        return True
    if record.linked_task_id is not None:
        return True
    if message_content and _is_task_context_message(message_content):
        return record.role == "user"
    return False


def _is_task_context_message(content: str) -> bool:
    text = content.strip()
    if not text:
        return False
    if is_task_confirmation_message(text):
        return True
    if CONFIRMATION_CONTEXT_PATTERN.search(text) is not None:
        return True
    if CORRECTION_HINT_PATTERN.search(text) is not None:
        return True
    if re.search(r"(补充|clarif|clarification|missing|缺少|还需要)", text, re.IGNORECASE):
        return True
    return False


def _build_explicit_signals(
    *,
    message_content: str,
    latest_active_revision: TaskSpecRevisionRecord | None,
) -> dict[str, object]:
    upload_file_hint = bool(UPLOAD_HINT_PATTERN.search(message_content))
    bbox_hint = bool(BBOX_HINT_PATTERN.search(message_content) or parse_bbox_text(message_content))
    admin_region_hint = bool(ADMIN_REGION_HINT_PATTERN.search(message_content))
    confirmation_hint = is_task_confirmation_message(message_content)
    correction_hint = bool(CORRECTION_HINT_PATTERN.search(message_content))

    revision_field_overlap = _build_revision_field_overlap(
        message_content=message_content,
        latest_active_revision=latest_active_revision,
        upload_file_hint=upload_file_hint,
        bbox_hint=bbox_hint,
        admin_region_hint=admin_region_hint,
        correction_hint=correction_hint,
    )

    return {
        "upload_file_hint": upload_file_hint,
        "bbox_hint": bbox_hint,
        "admin_region_hint": admin_region_hint,
        "confirmation_hint": confirmation_hint,
        "correction_hint": correction_hint,
        "revision_field_overlap": revision_field_overlap,
    }


def _build_revision_field_overlap(
    *,
    message_content: str,
    latest_active_revision: TaskSpecRevisionRecord | None,
    upload_file_hint: bool,
    bbox_hint: bool,
    admin_region_hint: bool,
    correction_hint: bool,
) -> dict[str, object]:
    if latest_active_revision is None:
        return {"revision_id": None, "fields": [], "matched_signals": []}

    raw_spec = dict(latest_active_revision.raw_spec_json or {})
    matched_fields: list[str] = []
    matched_signals: list[str] = []

    def add_fields(*fields: str) -> None:
        for field in fields:
            if field in raw_spec and field not in matched_fields:
                matched_fields.append(field)

    if upload_file_hint:
        add_fields("aoi_input", "aoi_source_type", "upload_slots")
        if any(raw_spec.get(field) for field in ("aoi_input", "aoi_source_type", "upload_slots")):
            matched_signals.append("upload_file_hint")

    if bbox_hint:
        aoi_input = str(raw_spec.get("aoi_input") or "")
        if raw_spec.get("aoi_source_type") == "bbox" or parse_bbox_text(aoi_input):
            add_fields("aoi_input", "aoi_source_type")
            matched_signals.append("bbox_hint")

    if admin_region_hint:
        aoi_input = str(raw_spec.get("aoi_input") or "")
        if raw_spec.get("aoi_source_type") in {"admin_name", "place_alias"} or _looks_like_admin_name(aoi_input):
            add_fields("aoi_input", "aoi_source_type")
            matched_signals.append("admin_region_hint")

    if correction_hint and raw_spec:
        add_fields("aoi_input", "aoi_source_type", "time_range", "upload_slots")
        matched_signals.append("correction_hint")

    if not matched_fields and message_content.strip():
        aoi_input = str(raw_spec.get("aoi_input") or "")
        if aoi_input and aoi_input in message_content:
            add_fields("aoi_input")
            matched_signals.append("literal_overlap")

    return {
        "revision_id": latest_active_revision.id,
        "fields": matched_fields,
        "matched_signals": matched_signals,
    }


def _looks_like_admin_name(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return False
    return compact.endswith(("自治区", "自治州", "特别行政区", "省", "市", "区", "县", "州", "盟", "旗"))


def _build_signal_evidence(
    *,
    message_content: str,
    latest_active_revision: TaskSpecRevisionRecord | None,
    explicit_signals: dict[str, object],
) -> dict[str, list[str]]:
    evidence: dict[str, list[str]] = {
        "upload_file_hint": [],
        "bbox_hint": [],
        "admin_region_hint": [],
        "confirmation_hint": [],
        "correction_hint": [],
        "revision_field_overlap": [],
    }

    if explicit_signals.get("upload_file_hint"):
        evidence["upload_file_hint"].append("current message mentions upload/file/shp/boundary terms")
    if explicit_signals.get("bbox_hint"):
        evidence["bbox_hint"].append("current message contains bbox-like text")
    if explicit_signals.get("admin_region_hint"):
        evidence["admin_region_hint"].append("current message contains an administrative-region cue")
    if explicit_signals.get("confirmation_hint"):
        evidence["confirmation_hint"].append("current message matches the confirmation heuristic")
    if explicit_signals.get("correction_hint"):
        evidence["correction_hint"].append("current message matches the correction heuristic")

    overlap = explicit_signals.get("revision_field_overlap")
    if isinstance(overlap, dict):
        revision_id = overlap.get("revision_id")
        fields = overlap.get("fields") or []
        matched_signals = overlap.get("matched_signals") or []
        if revision_id is not None and fields:
            evidence["revision_field_overlap"].append(
                f"revision={revision_id} fields={','.join(str(field) for field in fields)} signals={','.join(str(signal) for signal in matched_signals)}"
            )

    if latest_active_revision is not None:
        summary = latest_active_revision.understanding_summary
        if summary:
            evidence.setdefault("latest_active_revision", []).append(summary)

    return evidence


def _serialize_uploaded_file(record: UploadedFileRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "original_name": record.original_name,
        "file_type": record.file_type,
        "storage_key": record.storage_key,
        "size_bytes": record.size_bytes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _serialize_message(record: MessageRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "role": record.role,
        "content": record.content,
        "linked_task_id": record.linked_task_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _serialize_upload_trace(record: UploadedFileRecord, preferred_file_ids: list[str]) -> dict[str, object]:
    return {
        "file_id": record.id,
        "original_name": record.original_name,
        "file_type": record.file_type,
        "reason": "preferred_file_ids" if record.id in preferred_file_ids else "recent_uploads",
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _serialize_message_trace(
    record: MessageRecord,
    latest_active_task: TaskRunRecord | None,
    latest_active_revision: TaskSpecRevisionRecord | None,
    current_message_id: str,
) -> dict[str, object]:
    if latest_active_revision is not None and record.id == latest_active_revision.source_message_id:
        reason = "active revision source message"
    elif latest_active_task is not None and record.linked_task_id == latest_active_task.id:
        reason = "task-linked confirmation/clarification context"
    elif record.id != current_message_id:
        reason = "nearby recent session message"
    else:
        reason = "current message"

    return {
        "message_id": record.id,
        "role": record.role,
        "linked_task_id": record.linked_task_id,
        "reason": reason,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
