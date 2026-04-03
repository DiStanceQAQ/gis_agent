from __future__ import annotations

from copy import deepcopy

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
from packages.domain.services.planner import PLAN_STATUS_NEEDS_CLARIFICATION, build_task_plan
from packages.domain.services.parser import (
    extract_parser_failure_error,
    is_followup_request,
    parse_followup_override_message,
    parse_task_message,
)
from packages.domain.services.storage import detect_file_type, write_upload_file
from packages.domain.services.task_events import append_task_event, list_task_events
from packages.domain.services.task_state import (
    TASK_STATUS_APPROVED,
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_QUEUED,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_task_status,
    write_task_error,
)
from packages.domain.utils import make_id, merge_dicts
from packages.schemas.file import UploadedFileResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse
from packages.schemas.operation_plan import OperationPlan
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


def _build_initial_operation_plan(task_plan: TaskPlanResponse) -> OperationPlan:
    return OperationPlan(
        version=1,
        status="draft",
        missing_fields=list(task_plan.missing_fields),
        nodes=[],
    )


def _load_latest_session_task(db: Session, session_id: str) -> TaskRunRecord | None:
    return (
        db.query(TaskRunRecord)
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
) -> TaskRunRecord:
    safe_parsed = parsed
    parser_error_code, parser_error_message = extract_parser_failure_error(safe_parsed)
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
    if parser_error_code and parser_error_message:
        write_task_error(
            task,
            error_code=parser_error_code,
            error_message=parser_error_message,
        )

    task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input=safe_parsed.aoi_input,
        aoi_source_type=safe_parsed.aoi_source_type,
        preferred_output=safe_parsed.preferred_output,
        user_priority=safe_parsed.user_priority,
        need_confirmation=safe_parsed.need_confirmation,
        raw_spec_json=safe_parsed.model_dump(),
    )
    db.add(task_spec)
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
        task_spec.raw_spec_json = safe_parsed.model_dump()

    # The task row is not committed yet at this point, so LLM call logs from
    # planner should not reference task_id to avoid FK races in a separate session.
    task_plan = build_task_plan(safe_parsed, task_id=None)
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
        task_spec.raw_spec_json = safe_parsed.model_dump()
    task.plan_json = task_plan.model_dump()

    initial_operation_plan = _build_initial_operation_plan(TaskPlanResponse(**task.plan_json))
    if not safe_parsed.need_confirmation:
        task.plan_json = {
            **(task.plan_json or {}),
            "operation_plan": initial_operation_plan.model_dump(),
        }
        set_task_status(
            db,
            task,
            TASK_STATUS_AWAITING_APPROVAL,
            current_step="awaiting_approval",
            event_type="task_plan_drafted",
            detail={"operation_plan_version": initial_operation_plan.version},
            force=True,
        )

    db.commit()
    db.refresh(task)
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
    db.commit()
    return task


def create_message_and_task(db: Session, payload: MessageCreateRequest) -> MessageCreateResponse:
    session = db.get(SessionRecord, payload.session_id)
    if session is None:
        raise AppError.not_found(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message="Session not found.",
            detail={"session_id": payload.session_id},
        )

    if session.title is None:
        session.title = payload.content[:60]

    files = []
    if payload.file_ids:
        files = (
            db.query(UploadedFileRecord)
            .filter(UploadedFileRecord.id.in_(payload.file_ids))
            .all()
        )
        found_file_ids = {record.id for record in files}
        missing_file_ids = [file_id for file_id in payload.file_ids if file_id not in found_file_ids]
        if missing_file_ids:
            raise AppError.not_found(
                error_code=ErrorCode.FILE_NOT_FOUND,
                message="One or more uploaded files were not found.",
                detail={"file_ids": missing_file_ids},
            )

    message = MessageRecord(
        id=make_id("msg"),
        session_id=payload.session_id,
        role="user",
        content=payload.content,
    )
    db.add(message)
    db.flush()

    parsed = parse_task_message(payload.content, has_upload=bool(files))
    parsed, parent_task, override = _resolve_followup_context(
        db,
        session_id=payload.session_id,
        content=payload.content,
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
        task_id=task.id,
        task_status=task.status,
        need_clarification=need_clarification,
        need_approval=need_approval,
        missing_fields=list(response_spec.get("missing_fields", [])),
        clarification_message=response_spec.get("clarification_message"),
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
    )


def update_task_plan_draft(db: Session, task_id: str, plan: OperationPlan) -> TaskDetailResponse:
    task = _load_task(db, task_id)
    if task.status != TASK_STATUS_AWAITING_APPROVAL or not task.plan_json:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_APPROVAL_REQUIRED,
            message="Task plan is not awaiting approval.",
            detail={"task_id": task_id},
        )

    draft_plan = plan.model_copy(update={"status": "validated"})
    task.plan_json = {
        **task.plan_json,
        "operation_plan": draft_plan.model_dump(),
    }
    append_task_event(
        db,
        task_id=task.id,
        event_type="task_plan_updated",
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
    if int(operation_plan_data.get("version", 0)) != approved_version:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_EDIT_INVALID,
            message="Approved version does not match latest draft plan.",
            detail={"task_id": task_id, "approved_version": approved_version},
        )

    approved_plan = OperationPlan(**operation_plan_data).model_copy(update={"status": "approved"})
    task.plan_json = {
        **task.plan_json,
        "operation_plan": approved_plan.model_dump(),
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
        _queue_or_run(task.id)
    return get_task_detail(db=db, task_id=task.id)
