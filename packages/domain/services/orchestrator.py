from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

from fastapi import UploadFile
from sqlalchemy.orm import Session, selectinload

from apps.worker.tasks import run_task_async
from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import (
    AOIRecord,
    MessageRecord,
    SessionRecord,
    TaskRunRecord,
    TaskSpecRecord,
    UploadedFileRecord,
)
from packages.domain.services.aoi import (
    build_named_aoi_clarification_message,
    clone_task_aoi,
    normalize_task_aoi,
    upsert_task_aoi,
)
from packages.domain.services.agent_runtime import run_task_runtime
from packages.domain.services.chat import generate_chat_reply
from packages.domain.services.input_slots import classify_uploaded_inputs
from packages.domain.services.intent import classify_message_intent, is_task_confirmation_message
from packages.domain.services.operation_plan_builder import build_operation_plan_from_registry
from packages.domain.services.planner import PLAN_STATUS_NEEDS_CLARIFICATION, build_task_plan
from packages.domain.services.parser import (
    extract_parser_failure_error,
    is_followup_request,
    parse_followup_override_message,
    parse_task_message,
)
from packages.domain.services.plan_guard import validate_operation_plan
from packages.domain.services.storage import detect_file_type, write_upload_file
from packages.domain.services.task_events import append_task_event, list_task_events
from packages.domain.services.task_state import (
    TASK_STATUS_APPROVED,
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_task_status,
    write_task_error,
)
from packages.domain.utils import make_id, merge_dicts
from packages.schemas.file import UploadedFileResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse
from packages.schemas.operation_plan import OperationNode, OperationPlan
from packages.schemas.session import SessionResponse, SessionTaskItemResponse, SessionTasksResponse
from packages.schemas.task import (
    ArtifactResponse,
    CandidateResponse,
    ParsedTaskSpec,
    RecommendationResponse,
    TaskDetailResponse,
    TaskEventResponse,
    TaskEventsResponse,
    TaskPlanResponse,
    TaskStepResponse,
)

logger = get_logger(__name__)

TASK_CONFIRMATION_PROMPT = "我理解你想发起一个 GIS 分析任务。请回复“好的，继续”或“开始执行”，我会为你生成计划并进入审批。"
TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT = (
    "我收到了确认，但还没找到上一条可执行的任务请求。请先描述分析区域、时间范围和目标，再回复“好的，继续”。"
)
UPLOAD_REQUEST_HINT_PATTERN = re.compile(
    r"(上传|文件|边界|geojson|shp|gpkg)",
    re.IGNORECASE,
)


def _require_named_aoi_clarification(parsed: ParsedTaskSpec) -> ParsedTaskSpec:
    missing_fields = list(parsed.missing_fields)
    if "aoi_boundary" not in missing_fields:
        missing_fields.append("aoi_boundary")
    return parsed.model_copy(
        update={
            "need_confirmation": True,
            "missing_fields": missing_fields,
            "clarification_message": build_named_aoi_clarification_message(parsed.aoi_input or "当前研究区"),
        }
    )


def _build_task_spec_raw_payload(
    parsed: ParsedTaskSpec,
    upload_slots: dict[str, str],
) -> dict[str, object]:
    payload = parsed.model_dump()
    if upload_slots:
        payload["upload_slots"] = upload_slots
    return payload


def _validate_operation_plan_for_edit(plan: OperationPlan) -> OperationPlan:
    try:
        return validate_operation_plan(plan)
    except AppError as exc:
        if exc.error_code in {ErrorCode.PLAN_SCHEMA_INVALID, ErrorCode.PLAN_DEPENDENCY_CYCLE}:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_EDIT_INVALID,
                message="Edited plan failed validation.",
                detail={
                    "cause_error_code": exc.error_code,
                    "cause_message": exc.message,
                    "cause_detail": exc.detail,
                },
            ) from exc
        raise


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
    )


def _queue_or_run(task_id: str) -> None:
    settings = get_settings()
    if settings.is_async_execution:
        run_task_async.delay(task_id)
        return
    run_task_runtime(task_id)


def _persist_message(
    db: Session,
    *,
    session_id: str,
    role: str,
    content: str,
    linked_task_id: str | None = None,
) -> MessageRecord:
    message = MessageRecord(
        id=make_id("msg"),
        session_id=session_id,
        role=role,
        content=content,
        linked_task_id=linked_task_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(message)
    db.flush()
    return message


def _load_message_history(
    db: Session,
    *,
    session_id: str,
    exclude_message_id: str | None = None,
    limit: int,
) -> list[dict[str, str]]:
    query = db.query(MessageRecord).filter(MessageRecord.session_id == session_id)
    if exclude_message_id is not None:
        query = query.filter(MessageRecord.id != exclude_message_id)
    records = (
        query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
        .limit(limit)
        .all()
    )
    return [{"role": record.role, "content": record.content} for record in reversed(records)]


def _parse_candidate_request_content(content: str, *, has_upload: bool) -> ParsedTaskSpec:
    return parse_task_message(content, has_upload=has_upload)


def _load_recent_session_file_ids(
    db: Session,
    *,
    session_id: str,
    limit: int,
    before_created_at: datetime | None = None,
) -> list[str]:
    files_query = db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id)
    if before_created_at is not None:
        files_query = files_query.filter(UploadedFileRecord.created_at <= before_created_at)
    files = (
        files_query.order_by(UploadedFileRecord.created_at.desc(), UploadedFileRecord.id.desc())
        .limit(limit)
        .all()
    )
    return [record.id for record in files]


def _is_executable_followup_request(content: str, *, latest_task: TaskRunRecord | None) -> bool:
    if latest_task is None or not is_followup_request(content):
        return False
    return bool(parse_followup_override_message(content, has_upload=False))


def _load_active_confirmation_prompt_message(
    db: Session,
    *,
    session_id: str,
    current_message_id: str,
    limit: int,
) -> MessageRecord | None:
    prior_messages = (
        db.query(MessageRecord)
        .filter(
            MessageRecord.session_id == session_id,
            MessageRecord.id != current_message_id,
        )
        .order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
        .limit(limit)
        .all()
    )
    if not prior_messages:
        return None

    latest_message = prior_messages[0]
    if latest_message.role != "assistant" or latest_message.content != TASK_CONFIRMATION_PROMPT:
        return None
    return latest_message


def _find_latest_executable_request_message(
    db: Session,
    *,
    session_id: str,
    current_message_id: str,
    confirmation_prompt_message: MessageRecord,
    has_recent_uploads: bool,
    limit: int,
) -> MessageRecord | None:
    latest_task = _load_latest_session_task(db, session_id)
    prior_messages = (
        db.query(MessageRecord)
        .filter(
            MessageRecord.session_id == session_id,
            MessageRecord.id != current_message_id,
        )
        .order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
        .limit(limit)
        .all()
    )

    prompt_seen = False

    for message in prior_messages:
        if not prompt_seen:
            prompt_seen = message.id == confirmation_prompt_message.id
            continue
        if message.role == "assistant":
            break
        if message.role != "user" or message.linked_task_id is not None:
            continue

        content = message.content.strip()
        if not content or is_task_confirmation_message(content):
            continue
        if _is_executable_followup_request(content, latest_task=latest_task):
            return message

        has_upload = has_recent_uploads and UPLOAD_REQUEST_HINT_PATTERN.search(content) is not None
        parsed = _parse_candidate_request_content(content, has_upload=has_upload)
        if parsed.aoi_input or parsed.time_range or parsed.requested_dataset:
            return message
        # Fallback: once the user has explicitly confirmed execution, we still treat
        # the nearest pre-confirmation user request as executable context, even if
        # parser extraction is incomplete for that message.
        return message
    return None


def _load_uploaded_files_by_payload_order(
    db: Session,
    *,
    file_ids: list[str],
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
    return [files_by_id[file_id] for file_id in file_ids]


def _create_chat_mode_response(
    db: Session,
    *,
    user_message_id: str,
    session_id: str,
    assistant_message: str,
    intent: str,
    intent_confidence: float,
    awaiting_task_confirmation: bool = False,
) -> MessageCreateResponse:
    _persist_message(
        db,
        session_id=session_id,
        role="assistant",
        content=assistant_message,
    )
    db.commit()
    return MessageCreateResponse(
        message_id=user_message_id,
        mode="chat",
        task_id=None,
        task_status=None,
        assistant_message=assistant_message,
        intent=intent,
        intent_confidence=intent_confidence,
        awaiting_task_confirmation=awaiting_task_confirmation,
    )


def _build_initial_operation_plan(task_plan: TaskPlanResponse, parsed: ParsedTaskSpec) -> OperationPlan:
    if task_plan.operation_plan_nodes:
        try:
            candidate = OperationPlan(
                version=1,
                status="draft",
                missing_fields=list(task_plan.missing_fields),
                nodes=[OperationNode.model_validate(node) for node in task_plan.operation_plan_nodes],
            )
            validated = validate_operation_plan(candidate)
            return validated.model_copy(update={"status": "draft"})
        except Exception as exc:  # pragma: no cover - fallback guard
            logger.warning("task.operation_plan_from_planner_invalid detail=%s", repr(exc))

    return build_operation_plan_from_registry(
        parsed,
        version=1,
        status="draft",
        missing_fields=list(task_plan.missing_fields),
    )


def _load_latest_session_task(db: Session, session_id: str) -> TaskRunRecord | None:
    return (
        db.query(TaskRunRecord)
        .join(TaskSpecRecord, TaskSpecRecord.task_id == TaskRunRecord.id)
        .options(
            selectinload(TaskRunRecord.task_spec),
            selectinload(TaskRunRecord.aoi),
        )
        .filter(TaskRunRecord.session_id == session_id)
        .order_by(TaskRunRecord.created_at.desc())
        .first()
    )


def _merge_task_spec(base_spec: dict, override: dict[str, object]) -> ParsedTaskSpec:
    merged = merge_dicts(base_spec, override)
    if "preferred_output" in override:
        base_outputs = list(base_spec.get("preferred_output") or [])
        override_outputs = list(override.get("preferred_output") or [])
        merged["preferred_output"] = list(dict.fromkeys([*base_outputs, *override_outputs]))
    return ParsedTaskSpec(**merged)


def _resolve_followup_context(
    db: Session,
    *,
    session_id: str,
    content: str,
    parsed: ParsedTaskSpec,
    has_upload: bool,
) -> tuple[ParsedTaskSpec, TaskRunRecord | None, dict[str, object]]:
    if has_upload:
        return parsed, None, {}

    latest_task = _load_latest_session_task(db, session_id)
    if latest_task is None or latest_task.task_spec is None:
        return parsed, None, {}

    override = parse_followup_override_message(content, has_upload=False)
    if not override:
        return parsed, None, {}

    if not is_followup_request(content) and not parsed.need_confirmation:
        return parsed, None, {}

    merged = _merge_task_spec(latest_task.task_spec.raw_spec_json, override)
    logger.info(
        "task.followup_resolved session_id=%s parent_task_id=%s override_keys=%s",
        session_id,
        latest_task.id,
        sorted(override.keys()),
    )
    return merged, latest_task, override


def _build_task(
    db: Session,
    session_id: str,
    user_message_id: str,
    parsed: ParsedTaskSpec,
    uploaded_files: list[UploadedFileRecord] | None = None,
    parent_task_id: str | None = None,
    inherited_aoi: AOIRecord | None = None,
    require_approval: bool = False,
    existing_task: TaskRunRecord | None = None,
) -> TaskRunRecord:
    safe_parsed = parsed
    upload_slots = classify_uploaded_inputs(uploaded_files or [])
    parser_error_code, parser_error_message = extract_parser_failure_error(safe_parsed)
    if existing_task is None:
        task = TaskRunRecord(
            id=make_id("task"),
            session_id=session_id,
            parent_task_id=parent_task_id,
            user_message_id=user_message_id,
            status=TASK_STATUS_WAITING_CLARIFICATION if safe_parsed.need_confirmation else TASK_STATUS_QUEUED,
            current_step="parse_task",
            analysis_type=safe_parsed.analysis_type,
            requested_time_range=safe_parsed.time_range,
        )
        db.add(task)
        db.flush()
    else:
        task = existing_task
        task.parent_task_id = parent_task_id
        task.status = TASK_STATUS_WAITING_CLARIFICATION if safe_parsed.need_confirmation else TASK_STATUS_QUEUED
        task.current_step = "parse_task"
        task.analysis_type = safe_parsed.analysis_type
        task.requested_time_range = safe_parsed.time_range
    if parser_error_code and parser_error_message:
        write_task_error(
            task,
            error_code=parser_error_code,
            error_message=parser_error_message,
        )

    task_spec = db.get(TaskSpecRecord, task.id)
    if task_spec is None:
        task_spec = TaskSpecRecord(
            task_id=task.id,
            aoi_input=safe_parsed.aoi_input,
            aoi_source_type=safe_parsed.aoi_source_type,
            preferred_output=safe_parsed.preferred_output,
            user_priority=safe_parsed.user_priority,
            need_confirmation=safe_parsed.need_confirmation,
            raw_spec_json=_build_task_spec_raw_payload(safe_parsed, upload_slots),
        )
        db.add(task_spec)
    else:
        task_spec.aoi_input = safe_parsed.aoi_input
        task_spec.aoi_source_type = safe_parsed.aoi_source_type
        task_spec.preferred_output = safe_parsed.preferred_output
        task_spec.user_priority = safe_parsed.user_priority
        task_spec.need_confirmation = safe_parsed.need_confirmation
        task_spec.raw_spec_json = _build_task_spec_raw_payload(safe_parsed, upload_slots)
    db.flush()

    normalized_aoi = normalize_task_aoi(task=task, uploaded_files=uploaded_files)
    if normalized_aoi is not None:
        upsert_task_aoi(task=task, normalized_aoi=normalized_aoi)
    elif inherited_aoi is not None:
        clone_task_aoi(task=task, original_aoi=inherited_aoi)
    elif safe_parsed.aoi_input and safe_parsed.aoi_source_type in {"admin_name", "place_alias"}:
        safe_parsed = _require_named_aoi_clarification(safe_parsed)
        task.status = TASK_STATUS_WAITING_CLARIFICATION
        task_spec.need_confirmation = True
        task_spec.raw_spec_json = _build_task_spec_raw_payload(safe_parsed, upload_slots)

    try:
        task_plan = build_task_plan(safe_parsed, task_id=task.id, db_session=db)
    except TypeError:
        task_plan = build_task_plan(safe_parsed, task_id=task.id)
    if task_plan.error_code and task_plan.error_message:
        write_task_error(
            task,
            error_code=task_plan.error_code,
            error_message=task_plan.error_message,
        )
    if task_plan.status == PLAN_STATUS_NEEDS_CLARIFICATION and not safe_parsed.need_confirmation:
        merged_missing_fields = list(dict.fromkeys([*safe_parsed.missing_fields, *task_plan.missing_fields]))
        safe_parsed = safe_parsed.model_copy(
            update={
                "need_confirmation": True,
                "missing_fields": merged_missing_fields,
                "clarification_message": safe_parsed.clarification_message or "任务规划需要补充信息后重试。",
            }
        )
        task.status = TASK_STATUS_WAITING_CLARIFICATION
        task_spec.need_confirmation = True
        task_spec.raw_spec_json = _build_task_spec_raw_payload(safe_parsed, upload_slots)
    task.plan_json = task_plan.model_dump()

    if require_approval and not safe_parsed.need_confirmation:
        initial_operation_plan = _build_initial_operation_plan(
            TaskPlanResponse(**task.plan_json),
            safe_parsed,
        )
        task.plan_json = {
            **(task.plan_json or {}),
            "operation_plan": initial_operation_plan.model_dump(),
            "operation_plan_version": initial_operation_plan.version,
        }
        task.status = TASK_STATUS_AWAITING_APPROVAL
        task.current_step = "awaiting_approval"

    append_task_event(
        db,
        task_id=task.id,
        event_type="task_created",
        status=task.status,
        detail={
            "analysis_type": task.analysis_type,
            "current_step": task.current_step,
            "error_code": task.error_code,
        },
    )
    append_task_event(
        db,
        task_id=task.id,
        event_type="task_plan_built",
        status=task.status,
        detail={
            "plan_status": (task.plan_json or {}).get("status"),
            "objective": (task.plan_json or {}).get("objective"),
            "step_count": len((task.plan_json or {}).get("steps", [])),
            "error_code": (task.plan_json or {}).get("error_code"),
        },
    )
    if require_approval and not safe_parsed.need_confirmation:
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_plan_drafted",
            status=task.status,
            step_name="awaiting_approval",
            detail={"operation_plan_version": (task.plan_json or {}).get("operation_plan_version")},
        )
    return task


def create_message_and_task(
    db: Session,
    payload: MessageCreateRequest,
    *,
    existing_user_message: MessageRecord | None = None,
    task_request_content: str | None = None,
) -> MessageCreateResponse:
    session = db.get(SessionRecord, payload.session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": payload.session_id},
        )

    if session.title is None:
        session.title = payload.content[:60]

    request_content = (task_request_content or payload.content).strip() or payload.content

    files = []
    if payload.file_ids:
        files = _load_uploaded_files_by_payload_order(db, file_ids=payload.file_ids)

    if existing_user_message is None:
        message = _persist_message(
            db,
            session_id=payload.session_id,
            role="user",
            content=payload.content,
        )
    else:
        message = existing_user_message

    provisional_task = TaskRunRecord(
        id=make_id("task"),
        session_id=payload.session_id,
        user_message_id=message.id,
        status=TASK_STATUS_QUEUED,
        current_step="parse_task",
        analysis_type="NDVI",
    )
    db.add(provisional_task)
    db.flush()

    try:
        parsed = parse_task_message(
            request_content,
            has_upload=bool(files),
            task_id=provisional_task.id,
            db_session=db,
        )
    except TypeError:
        parsed = parse_task_message(request_content, has_upload=bool(files))
    parsed, parent_task, override = _resolve_followup_context(
        db,
        session_id=payload.session_id,
        content=request_content,
        parsed=parsed,
        has_upload=bool(files),
    )
    task = _build_task(
        db=db,
        session_id=payload.session_id,
        user_message_id=message.id,
        parsed=parsed,
        uploaded_files=files,
        parent_task_id=parent_task.id if parent_task is not None else None,
        inherited_aoi=(
            parent_task.aoi
            if parent_task is not None and should_inherit_original_aoi(parent_task, parsed, override)
            else None
        ),
        require_approval=True,
        existing_task=provisional_task,
    )

    message.linked_task_id = task.id
    db.flush()
    logger.info(
        "task.created session_id=%s task_id=%s message_id=%s need_clarification=%s",
        payload.session_id,
        task.id,
        message.id,
        task.task_spec.need_confirmation if task.task_spec else parsed.need_confirmation,
    )

    response_spec = task.task_spec.raw_spec_json if task.task_spec else parsed.model_dump()
    need_clarification = bool(response_spec.get("need_confirmation", False))
    need_approval = not need_clarification

    if need_clarification:
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_waiting_clarification",
            status=task.status,
            detail={"missing_fields": list(response_spec.get("missing_fields", []))},
        )
    db.commit()

    return MessageCreateResponse(
        message_id=message.id,
        mode="task",
        task_id=task.id,
        task_status=task.status,
        need_clarification=need_clarification,
        need_approval=need_approval,
        missing_fields=list(response_spec.get("missing_fields", [])),
        clarification_message=response_spec.get("clarification_message"),
    )


def create_message(db: Session, payload: MessageCreateRequest) -> MessageCreateResponse:
    settings = get_settings()
    if not settings.intent_router_enabled:
        return create_message_and_task(db=db, payload=payload)

    session = db.get(SessionRecord, payload.session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": payload.session_id},
        )

    if session.title is None:
        session.title = payload.content[:60]

    message = _persist_message(
        db,
        session_id=payload.session_id,
        role="user",
        content=payload.content,
    )
    history = _load_message_history(
        db,
        session_id=payload.session_id,
        exclude_message_id=message.id,
        limit=settings.intent_history_limit,
    )
    intent_result = classify_message_intent(
        payload.content,
        history=history,
        db_session=db,
    )

    if is_task_confirmation_message(payload.content):
        confirmation_prompt_message = _load_active_confirmation_prompt_message(
            db,
            session_id=payload.session_id,
            current_message_id=message.id,
            limit=settings.intent_history_limit,
        )
        if confirmation_prompt_message is None:
            return _create_chat_mode_response(
                db,
                user_message_id=message.id,
                session_id=payload.session_id,
                assistant_message=TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
            )

        session_file_ids = _load_recent_session_file_ids(
            db,
            session_id=payload.session_id,
            limit=settings.intent_history_limit,
        )
        source_request_message = _find_latest_executable_request_message(
            db,
            session_id=payload.session_id,
            current_message_id=message.id,
            confirmation_prompt_message=confirmation_prompt_message,
            has_recent_uploads=bool(session_file_ids),
            limit=settings.intent_history_limit,
        )
        if source_request_message is None:
            return _create_chat_mode_response(
                db,
                user_message_id=message.id,
                session_id=payload.session_id,
                assistant_message=TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
            )

        fallback_file_ids = _load_recent_session_file_ids(
            db,
            session_id=payload.session_id,
            limit=settings.intent_history_limit,
            before_created_at=source_request_message.created_at,
        )
        task_payload = payload
        if (
            not task_payload.file_ids
            and fallback_file_ids
            and UPLOAD_REQUEST_HINT_PATTERN.search(source_request_message.content)
        ):
            task_payload = payload.model_copy(update={"file_ids": fallback_file_ids})

        task_response = create_message_and_task(
            db=db,
            payload=task_payload,
            existing_user_message=message,
            task_request_content=source_request_message.content,
        )
        source_request_message.linked_task_id = task_response.task_id
        db.commit()
        task_response.intent = intent_result.intent
        task_response.intent_confidence = intent_result.confidence
        task_response.awaiting_task_confirmation = False
        return task_response

    if (
        intent_result.intent == "task"
        and intent_result.confidence >= settings.intent_task_confidence_threshold
    ):
        return _create_chat_mode_response(
            db,
            user_message_id=message.id,
            session_id=payload.session_id,
            assistant_message=TASK_CONFIRMATION_PROMPT,
            intent=intent_result.intent,
            intent_confidence=intent_result.confidence,
            awaiting_task_confirmation=True,
        )

    assistant_message = generate_chat_reply(
        user_message=payload.content,
        history=history,
        db_session=db,
    )
    return _create_chat_mode_response(
        db,
        user_message_id=message.id,
        session_id=payload.session_id,
        assistant_message=assistant_message,
        intent=intent_result.intent,
        intent_confidence=intent_result.confidence,
    )


def _load_task(db: Session, task_id: str) -> TaskRunRecord:
    task = (
        db.query(TaskRunRecord)
        .options(
            selectinload(TaskRunRecord.task_spec),
            selectinload(TaskRunRecord.aoi),
            selectinload(TaskRunRecord.candidates),
            selectinload(TaskRunRecord.steps),
            selectinload(TaskRunRecord.artifacts),
        )
        .filter(TaskRunRecord.id == task_id)
        .first()
    )
    if task is None:
        raise AppError.not_found(
            error_code=ErrorCode.TASK_NOT_FOUND,
            message="Task not found.",
            detail={"task_id": task_id},
        )
    return task


def get_task_detail(db: Session, task_id: str) -> TaskDetailResponse:
    settings = get_settings()
    task = _load_task(db, task_id)

    recommendation = None
    if task.recommendation_json:
        recommendation = RecommendationResponse(**task.recommendation_json)

    task_spec = task.task_spec.raw_spec_json if task.task_spec else None
    task_plan = TaskPlanResponse(**task.plan_json) if task.plan_json else None
    operation_plan = None
    if task.plan_json and task.plan_json.get("operation_plan") is not None:
        operation_plan = OperationPlan(**task.plan_json["operation_plan"])

    artifacts = [
        ArtifactResponse(
            artifact_id=artifact.id,
            artifact_type=artifact.artifact_type,
            mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes,
            checksum=artifact.checksum,
            created_at=artifact.created_at,
            metadata=artifact.metadata_json,
            download_url=f"{settings.api_prefix}/artifacts/{artifact.id}",
        )
        for artifact in sorted(task.artifacts, key=lambda item: item.created_at)
    ]

    png_preview_url = next(
        (artifact.download_url for artifact in artifacts if artifact.artifact_type == "png_map"),
        None,
    )

    return TaskDetailResponse(
        task_id=task.id,
        parent_task_id=task.parent_task_id,
        status=task.status,
        current_step=task.current_step,
        analysis_type=task.analysis_type,
        created_at=task.created_at,
        task_spec=task_spec,
        task_plan=task_plan,
        operation_plan=operation_plan,
        recommendation=recommendation,
        candidates=[
            CandidateResponse(
                dataset_name=c.dataset_name,
                collection_id=c.collection_id,
                scene_count=c.scene_count,
                coverage_ratio=c.coverage_ratio,
                effective_pixel_ratio_estimate=c.effective_pixel_ratio_estimate,
                cloud_metric_summary=c.cloud_metric_summary,
                spatial_resolution=c.spatial_resolution,
                temporal_density_note=c.temporal_density_note,
                suitability_score=c.suitability_score,
                recommendation_rank=c.recommendation_rank,
                summary_json=c.summary_json,
            )
            for c in sorted(task.candidates, key=lambda item: item.recommendation_rank)
        ],
        steps=[
            TaskStepResponse(
                step_name=step.step_name,
                status=step.status,
                started_at=step.started_at,
                ended_at=step.ended_at,
                observation=step.detail_json,
                detail=step.detail_json,
            )
            for step in task.steps
        ],
        artifacts=artifacts,
        summary_text=task.result_summary_text,
        methods_text=task.methods_text,
        png_preview_url=png_preview_url,
        aoi_name=task.aoi.name if task.aoi else None,
        aoi_bbox_bounds=list(task.aoi.bbox_bounds_json) if task.aoi and task.aoi.bbox_bounds_json else None,
        aoi_area_km2=task.aoi.area_km2 if task.aoi else None,
        requested_time_range=task.requested_time_range,
        actual_time_range=task.actual_time_range,
        error_code=task.error_code,
        error_message=task.error_message,
        clarification_message=(task_spec or {}).get("clarification_message"),
        rejected_reason=(task.plan_json or {}).get("rejected_reason"),
        rejected_at=_parse_iso_datetime((task.plan_json or {}).get("rejected_at")),
    )


def update_task_plan_draft(db: Session, task_id: str, plan: OperationPlan) -> TaskDetailResponse:
    task = _load_task(db, task_id)
    if task.status != TASK_STATUS_AWAITING_APPROVAL or not task.plan_json:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_APPROVAL_REQUIRED,
            message="Task plan is not awaiting approval.",
            detail={"task_id": task_id},
        )

    latest_version = int((task.plan_json or {}).get("operation_plan_version") or 0)
    if plan.version <= latest_version:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_EDIT_INVALID,
            message="Patched plan version must be greater than current draft version.",
            detail={"task_id": task_id, "latest_version": latest_version, "patched_version": plan.version},
        )

    draft_plan = _validate_operation_plan_for_edit(plan)
    task.plan_json = {
        **task.plan_json,
        "operation_plan": draft_plan.model_dump(),
        "operation_plan_version": draft_plan.version,
    }
    append_task_event(
        db,
        task_id=task.id,
        event_type="task_plan_validated",
        status=task.status,
        detail={"operation_plan_version": draft_plan.version},
    )
    db.commit()
    return get_task_detail(db=db, task_id=task.id)


def approve_task_plan(db: Session, task_id: str, approved_version: int) -> TaskDetailResponse:
    task = _load_task(db, task_id)
    if task.status != TASK_STATUS_AWAITING_APPROVAL or not task.plan_json:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_APPROVAL_REQUIRED,
            message="Task plan is not awaiting approval.",
            detail={"task_id": task_id},
        )

    operation_plan_data = task.plan_json.get("operation_plan") or {}
    if not operation_plan_data:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_APPROVAL_REQUIRED,
            message="Task plan is missing an editable operation plan.",
            detail={"task_id": task_id},
        )

    validated_plan = _validate_operation_plan_for_edit(OperationPlan(**operation_plan_data))
    if validated_plan.version != approved_version:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_EDIT_INVALID,
            message="Approved version does not match latest draft plan.",
            detail={"task_id": task_id, "approved_version": approved_version},
        )

    approved_plan = validated_plan.model_copy(update={"status": "approved"})
    task.plan_json = {
        **task.plan_json,
        "operation_plan": approved_plan.model_dump(),
        "operation_plan_version": approved_plan.version,
    }
    set_task_status(
        db,
        task,
        TASK_STATUS_APPROVED,
        event_type="task_plan_approved",
        detail={"approved_version": approved_version},
    )
    set_task_status(
        db,
        task,
        TASK_STATUS_QUEUED,
        event_type="task_queued",
        detail={"execution_mode": get_settings().execution_mode},
    )
    db.commit()
    _queue_or_run(task.id)
    return get_task_detail(db=db, task_id=task.id)


def reject_task_plan(db: Session, task_id: str, reason: str | None = None) -> TaskDetailResponse:
    task = _load_task(db, task_id)
    if task.status != TASK_STATUS_AWAITING_APPROVAL or not task.plan_json:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_APPROVAL_REQUIRED,
            message="Task plan is not awaiting approval.",
            detail={"task_id": task_id},
        )

    rejected_reason = (reason or "").strip() or "rejected_by_user"
    operation_plan_data = task.plan_json.get("operation_plan") or {}
    if operation_plan_data:
        current_plan = OperationPlan(**operation_plan_data)
        rejected_plan = current_plan.model_copy(update={"status": "rejected"})
        operation_plan_data = rejected_plan.model_dump()

    updated_plan_json: dict[str, object] = {
        **task.plan_json,
        "rejected_reason": rejected_reason,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }
    if operation_plan_data:
        updated_plan_json["operation_plan"] = operation_plan_data
    task.plan_json = updated_plan_json
    write_task_error(
        task,
        error_code=ErrorCode.TASK_CANCELLED_BY_USER,
        error_message=f"Task plan rejected by user: {rejected_reason}",
    )
    append_task_event(
        db,
        task_id=task.id,
        event_type="task_plan_rejected",
        status=task.status,
        detail={
            "reason": rejected_reason,
            "operation_plan_version": (task.plan_json or {}).get("operation_plan_version"),
        },
    )
    set_task_status(
        db,
        task,
        TASK_STATUS_CANCELLED,
        current_step="awaiting_approval",
        event_type="task_cancelled",
        detail={"reason": rejected_reason, "source": "user_reject"},
    )
    db.commit()
    return get_task_detail(db=db, task_id=task.id)


def get_task_events(db: Session, task_id: str, since_id: int = 0) -> TaskEventsResponse:
    try:
        task, events = list_task_events(db, task_id=task_id, since_id=since_id)
    except ValueError:
        raise AppError.not_found(
            error_code=ErrorCode.TASK_NOT_FOUND,
            message="Task not found.",
            detail={"task_id": task_id},
        ) from None

    next_cursor = since_id
    if events:
        next_cursor = events[-1].id

    return TaskEventsResponse(
        task_id=task.id,
        status=task.status,
        current_step=task.current_step,
        next_cursor=next_cursor,
        events=[
            TaskEventResponse(
                event_id=event.id,
                event_type=event.event_type,
                step_name=event.step_name,
                status=event.status,
                created_at=event.created_at,
                detail=event.detail_json,
            )
            for event in events
        ],
    )


def should_inherit_original_aoi(
    original_task: TaskRunRecord,
    parsed: ParsedTaskSpec,
    override: dict | None,
) -> bool:
    if original_task.aoi is None or original_task.task_spec is None:
        return False

    override = override or {}
    if "aoi_input" in override or "aoi_source_type" in override:
        return False

    return (
        parsed.aoi_input == original_task.task_spec.aoi_input
        and parsed.aoi_source_type == original_task.task_spec.aoi_source_type
    )


def rerun_task(db: Session, task_id: str, override: dict | None) -> TaskDetailResponse:
    original_task = _load_task(db, task_id)
    if original_task.task_spec is None:
        raise AppError.bad_request(
            error_code=ErrorCode.TASK_SPEC_MISSING,
            message="Original task has no task spec.",
            detail={"task_id": task_id},
        )

    original_spec = deepcopy(original_task.task_spec.raw_spec_json)
    merged = merge_dicts(original_spec, override or {})
    parsed = ParsedTaskSpec(**merged)

    message = MessageRecord(
        id=make_id("msg"),
        session_id=original_task.session_id,
        role="user",
        content="rerun task",
        linked_task_id=None,
    )
    db.add(message)
    db.flush()

    task = _build_task(
        db=db,
        session_id=original_task.session_id,
        user_message_id=message.id,
        parsed=parsed,
        uploaded_files=[],
        parent_task_id=original_task.id,
        inherited_aoi=original_task.aoi if should_inherit_original_aoi(original_task, parsed, override) else None,
    )
    message.linked_task_id = task.id
    db.flush()
    logger.info(
        "task.rerun original_task_id=%s new_task_id=%s session_id=%s",
        original_task.id,
        task.id,
        original_task.session_id,
    )

    if task.task_spec is not None and not task.task_spec.need_confirmation:
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_queued",
            status=task.status,
            detail={"execution_mode": get_settings().execution_mode, "rerun": True},
        )
    db.commit()
    if task.task_spec is not None and not task.task_spec.need_confirmation:
        _queue_or_run(task.id)
    return get_task_detail(db=db, task_id=task.id)
