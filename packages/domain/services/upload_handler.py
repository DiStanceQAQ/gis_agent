from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from fastapi import UploadFile
from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import SessionRecord, UploadedFileRecord
from packages.domain.services.storage import detect_file_type, write_upload_file
from packages.domain.utils import make_id
from packages.schemas.file import UploadedFileResponse
from packages.schemas.operation_plan import OperationNode, OperationPlan
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)

UPLOAD_REQUEST_HINT_PATTERN = re.compile(
    r"(上传|文件|边界|geojson|shp|gpkg)",
    re.IGNORECASE,
)
UPLOAD_STATUS_FOLLOWUP_PATTERN = re.compile(
    r"(现在呢|现在怎么样|更新了吗|你没更新|刷新了吗|移除了|删除了|还剩|剩下|还有几个|变了吗|还在吗|now|updated|refresh)",
    re.IGNORECASE,
)
UPLOAD_ACCESS_QUESTION_PATTERN = re.compile(
    r"(?:能|可以|是否|可不可以|可否).*(?:读到|读取|访问|识别|看到).*(?:上传|文件)|(?:读到|读取|访问|识别|看到).*(?:上传|文件)|can\s+you\s+(?:read|access).*(?:upload|file)",
    re.IGNORECASE,
)


def create_uploaded_file(db: Session, session_id: str, upload: UploadFile) -> UploadedFileResponse:
    session = db.get(SessionRecord, session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": session_id},
        )
    path, size_bytes, checksum = write_upload_file(session_id=session_id, upload=upload)
    file_type = detect_file_type(upload.filename or "")
    record = UploadedFileRecord(
        id=make_id("file"),
        session_id=session_id,
        original_name=upload.filename or "uploaded_file",
        file_type=file_type,
        storage_key=path,
        size_bytes=size_bytes,
        checksum=checksum,
    )
    db.add(record)
    db.flush()
    logger.info(
        "file.uploaded session_id=%s file_id=%s file_type=%s size_bytes=%s",
        session_id,
        record.id,
        record.file_type,
        record.size_bytes,
    )
    return UploadedFileResponse(
        file_id=record.id,
        file_type=record.file_type,
        storage_key=record.storage_key,
        original_name=record.original_name,
        size_bytes=record.size_bytes,
    )


def _load_recent_session_uploads(
    db: Session,
    *,
    session_id: str,
    limit: int,
    before_created_at: datetime | None = None,
) -> list[UploadedFileRecord]:
    files_query = db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id)
    if before_created_at is not None:
        files_query = files_query.filter(UploadedFileRecord.created_at <= before_created_at)
    return (
        files_query.order_by(UploadedFileRecord.created_at.desc(), UploadedFileRecord.id.desc())
        .limit(limit)
        .all()
    )


def _load_recent_session_file_ids(
    db: Session,
    *,
    session_id: str,
    limit: int,
    before_created_at: datetime | None = None,
) -> list[str]:
    files = _load_recent_session_uploads(
        db,
        session_id=session_id,
        limit=limit,
        before_created_at=before_created_at,
    )
    return [record.id for record in files]


def _resolve_chat_upload_files(
    db: Session,
    *,
    session_id: str,
    preferred_file_ids: list[str],
    limit: int,
) -> list[UploadedFileRecord]:
    if preferred_file_ids:
        try:
            return _load_uploaded_files_by_payload_order(
                db,
                file_ids=preferred_file_ids,
                session_id=session_id,
            )
        except AppError as exc:
            if exc.error_code != ErrorCode.FILE_NOT_FOUND:
                raise
            logger.warning(
                "chat.upload_files_from_payload_failed",
                extra={"session_id": session_id, "file_ids": preferred_file_ids},
                exc_info=True,
            )

    return _load_recent_session_uploads(
        db,
        session_id=session_id,
        limit=limit,
    )


def _is_upload_access_question(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if UPLOAD_ACCESS_QUESTION_PATTERN.search(text) is not None:
        return True

    normalized = text.lower().replace(" ", "")
    mentions_upload = any(token in normalized for token in ("上传", "文件", "upload", "file"))
    asks_access = any(
        token in normalized for token in ("读到", "读取", "访问", "识别", "看到", "read", "access")
    )
    return mentions_upload and asks_access


def _history_mentions_upload_context(history: list[dict[str, str]]) -> bool:
    for turn in history[-6:]:
        content = str(turn.get("content") or "")
        if not content:
            continue
        if _is_upload_access_question(content):
            return True
        if UPLOAD_REQUEST_HINT_PATTERN.search(content):
            return True
    return False


def _is_upload_status_followup(message: str, *, history: list[dict[str, str]]) -> bool:
    text = message.strip()
    if not text:
        return False
    if not UPLOAD_STATUS_FOLLOWUP_PATTERN.search(text):
        return False
    return _history_mentions_upload_context(history)


def _serialize_uploaded_files_for_chat(
    uploaded_files: list[UploadedFileRecord],
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for record in uploaded_files:
        serialized.append(
            {
                "file_id": record.id,
                "original_name": record.original_name,
                "file_type": record.file_type,
                "size_bytes": record.size_bytes,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            }
        )
    return serialized


def _build_upload_access_reply(uploaded_files: list[UploadedFileRecord]) -> str:
    if not uploaded_files:
        return "当前会话还没有可用上传文件。你可以先在左侧上传数据，再告诉我你要执行的 GIS 操作。"

    preview_items = [f"{item.original_name}（{item.file_type}）" for item in uploaded_files[:3]]
    suffix = "等" if len(uploaded_files) > 3 else ""
    return (
        "可以，我能识别当前会话里已上传的文件，并在进入执行阶段后将它们用于 GIS 处理。"
        f"当前共有 {len(uploaded_files)} 个文件：{'，'.join(preview_items)}{suffix}。"
        "如果你要开始处理，请直接描述操作并回复“好的，继续”。"
    )


def _load_uploaded_files_by_payload_order(
    db: Session,
    *,
    file_ids: list[str],
    session_id: str | None = None,
) -> list[UploadedFileRecord]:
    files = db.query(UploadedFileRecord).filter(UploadedFileRecord.id.in_(file_ids)).all()
    files_by_id = {record.id: record for record in files}
    missing_file_ids = [file_id for file_id in file_ids if file_id not in files_by_id]
    if missing_file_ids:
        raise AppError.not_found(
            error_code=ErrorCode.FILE_NOT_FOUND,
            message="One or more uploaded files were not found.",
            detail={"file_ids": missing_file_ids},
        )
    if session_id is not None:
        invalid_file_ids = [
            file_id for file_id in file_ids if files_by_id[file_id].session_id != session_id
        ]
        if invalid_file_ids:
            raise AppError.bad_request(
                error_code=ErrorCode.BAD_REQUEST,
                message="One or more uploaded files do not belong to the current session.",
                detail={"session_id": session_id, "file_ids": invalid_file_ids},
            )
    return [files_by_id[file_id] for file_id in file_ids]


def _resolve_uploaded_storage_path(storage_key: str) -> str:
    candidate = Path(storage_key)
    if candidate.is_absolute():
        return str(candidate)
    if candidate.exists():
        return str(candidate.resolve())
    settings = get_settings()
    rooted = Path(settings.storage_root) / candidate
    if rooted.exists():
        return str(rooted.resolve())
    return str(candidate)


def _select_uploaded_file(
    *,
    uploaded_files: list[UploadedFileRecord],
    preferred_file_id: str | None,
    allowed_types: set[str],
) -> UploadedFileRecord | None:
    if preferred_file_id:
        preferred = next((item for item in uploaded_files if item.id == preferred_file_id), None)
        if preferred is not None and preferred.file_type in allowed_types:
            return preferred
    return next((item for item in uploaded_files if item.file_type in allowed_types), None)


def _inject_upload_context_into_operation_plan(
    plan: OperationPlan,
    *,
    parsed: ParsedTaskSpec,
    uploaded_files: list[UploadedFileRecord] | None,
    upload_slots: dict[str, str] | None,
) -> OperationPlan:
    uploaded_files = uploaded_files or []
    upload_slots = upload_slots or {}
    files_by_storage_key = {item.storage_key: item for item in uploaded_files}
    raster_file = _select_uploaded_file(
        uploaded_files=uploaded_files,
        preferred_file_id=upload_slots.get("input_raster_file_id"),
        allowed_types={"raster_tiff"},
    )
    vector_file = _select_uploaded_file(
        uploaded_files=uploaded_files,
        preferred_file_id=upload_slots.get("input_vector_file_id"),
        allowed_types={"geojson", "shp", "shp_zip", "vector_gpkg"},
    )

    source_path = str((parsed.operation_params or {}).get("source_path") or "").strip()
    clip_path = str((parsed.operation_params or {}).get("clip_path") or "").strip()
    if source_path in files_by_storage_key:
        source_path = _resolve_uploaded_storage_path(source_path)
    if clip_path in files_by_storage_key:
        clip_path = _resolve_uploaded_storage_path(clip_path)

    normalized_nodes: list[OperationNode] = []
    for node in plan.nodes:
        payload = node.model_dump()
        inputs = dict(payload.get("inputs") or {})
        params = dict(payload.get("params") or {})
        op_name = str(payload.get("op_name") or "")

        if op_name == "input.upload_raster":
            if not str(inputs.get("upload_id") or "").strip() and raster_file is not None:
                inputs["upload_id"] = raster_file.id
            if not str(params.get("source_path") or "").strip():
                if source_path:
                    params["source_path"] = source_path
                elif raster_file is not None:
                    params["source_path"] = _resolve_uploaded_storage_path(raster_file.storage_key)

        if op_name == "input.upload_vector":
            if not str(inputs.get("upload_id") or "").strip() and vector_file is not None:
                inputs["upload_id"] = vector_file.id
            if not str(params.get("source_path") or "").strip():
                if clip_path:
                    params["source_path"] = clip_path
                elif vector_file is not None:
                    params["source_path"] = _resolve_uploaded_storage_path(vector_file.storage_key)

        if op_name == "raster.clip":
            has_clip_source = any(
                str(params.get(key) or "").strip()
                for key in ("aoi_path", "clip_path", "vector_path")
            )
            if not has_clip_source:
                if clip_path:
                    params["aoi_path"] = clip_path
                elif vector_file is not None:
                    params["aoi_path"] = _resolve_uploaded_storage_path(vector_file.storage_key)

        for key in ("source_path", "aoi_path", "clip_path", "vector_path"):
            value = str(params.get(key) or "").strip()
            if not value:
                continue
            uploaded = files_by_storage_key.get(value)
            if uploaded is not None:
                params[key] = _resolve_uploaded_storage_path(uploaded.storage_key)

        payload["inputs"] = inputs
        payload["params"] = params
        normalized_nodes.append(OperationNode.model_validate(payload))

    return plan.model_copy(update={"nodes": normalized_nodes})
