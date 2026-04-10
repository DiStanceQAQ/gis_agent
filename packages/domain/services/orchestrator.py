from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import (
    SessionRecord,
    TaskRunRecord,
)
from packages.domain.services.chat import generate_chat_reply, generate_chat_reply_stream
from packages.domain.services.conversation_context import build_conversation_context
from packages.domain.services.intent import classify_message_intent, is_task_confirmation_message
from packages.domain.services.aoi import (  # noqa: F401 - compat export
    normalize_active_revision_aoi,
    normalize_task_aoi,
)
from packages.domain.services.response_policy import decide_response
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.session_queries import (  # noqa: F401 - re-exported for backward compat
    create_session,
    get_session_memory,
    list_session_messages,
    list_session_tasks,
)
from packages.domain.services.upload_handler import (  # noqa: F401 - re-exported for backward compat
    UPLOAD_ACCESS_QUESTION_PATTERN,
    UPLOAD_REQUEST_HINT_PATTERN,
    UPLOAD_STATUS_FOLLOWUP_PATTERN,
    _build_upload_access_reply,
    _inject_upload_context_into_operation_plan,
    _is_upload_access_question,
    _is_upload_status_followup,
    _load_recent_session_file_ids,
    _load_recent_session_uploads,
    _load_uploaded_files_by_payload_order,
    _resolve_chat_upload_files,
    _resolve_uploaded_storage_path,
    _select_uploaded_file,
    _serialize_uploaded_files_for_chat,
    create_uploaded_file,
)
from packages.domain.services.revision_handler import (  # noqa: F401 - re-exported for backward compat
    _ParsedTaskUnderstanding,
    _apply_correction_revision,
    _build_initial_revision_understanding,
    _build_revision_override_payload,
)
from packages.domain.services.task_events import append_task_event, list_task_events  # noqa: F401
from packages.domain.services.task_lifecycle import (  # noqa: F401 - re-exported for backward compat
    _build_initial_operation_plan,
    _build_revision_summary,
    _build_task,
    _build_task_spec_raw_payload,
    _load_latest_session_task,
    _load_task,
    _merge_task_spec,
    _parse_iso_datetime,
    _persist_message,
    _queue_or_run,
    _require_named_aoi_clarification,
    _resolve_followup_context,
    _validate_operation_plan_for_edit,
    approve_task_plan,
    create_message_and_task,
    get_task_detail,
    get_task_events,
    reject_task_plan,
    rerun_task,
    should_inherit_original_aoi,
    update_task_plan_draft,
)
from packages.domain.services.task_revisions import get_active_revision
from packages.domain.services.understanding import understand_message
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse
from packages.domain.services.message_handler import (  # noqa: F401
    TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT,
    TASK_CONFIRMATION_PROMPT,
    TASK_CONFIRMATION_REQUIRES_REVISION_PROMPT,
    _ConfirmationSourceContext,
    _attach_understanding_metadata,
    _build_confirmation_response_decision,
    _build_confirmation_understanding,
    _create_chat_mode_response,
    _finalize_response_with_understanding,
    _find_latest_executable_request_message,
    _is_executable_followup_request,
    _load_active_confirmation_prompt_message,
    _load_latest_confirmation_candidate_message,
    _load_message_history,
    _parse_candidate_request_content,
    _persist_understanding_metadata,
    _resolve_confirmation_source_context,
    _serialize_understanding_response,
    _understanding_pipeline_enabled,
)
from packages.domain.services import message_handler as _message_handler_module
from packages.domain.services import revision_handler as _revision_handler_module
from packages.domain.services import task_lifecycle as _task_lifecycle_module
from packages.domain.services.parser import parse_task_message  # noqa: F401 - compat export
from packages.domain.services.planner import build_task_plan  # noqa: F401 - compat export
from packages.domain.services.processing_pipeline import (  # noqa: F401 - compat export
    preflight_processing_pipeline_inputs,
)

logger = get_logger(__name__)


def _sync_facade_compat() -> None:
    _task_lifecycle_module.parse_task_message = parse_task_message
    _task_lifecycle_module.build_task_plan = build_task_plan
    _task_lifecycle_module._queue_or_run = _queue_or_run
    _task_lifecycle_module.preflight_processing_pipeline_inputs = (
        preflight_processing_pipeline_inputs
    )
    _message_handler_module.parse_task_message = parse_task_message
    _revision_handler_module.normalize_active_revision_aoi = normalize_active_revision_aoi


def create_message_and_task(*args, **kwargs):
    _sync_facade_compat()
    return _task_lifecycle_module.create_message_and_task(*args, **kwargs)


def approve_task_plan(*args, **kwargs):
    _sync_facade_compat()
    return _task_lifecycle_module.approve_task_plan(*args, **kwargs)


def update_task_plan_draft(*args, **kwargs):
    _sync_facade_compat()
    return _task_lifecycle_module.update_task_plan_draft(*args, **kwargs)


def reject_task_plan(*args, **kwargs):
    _sync_facade_compat()
    return _task_lifecycle_module.reject_task_plan(*args, **kwargs)


def rerun_task(*args, **kwargs):
    _sync_facade_compat()
    return _task_lifecycle_module.rerun_task(*args, **kwargs)


def create_message(
    db: Session,
    payload: MessageCreateRequest,
    *,
    chat_stream_handler: Callable[[str], None] | None = None,
) -> MessageCreateResponse:
    _sync_facade_compat()
    settings = get_settings()
    if not settings.intent_router_enabled:
        return create_message_and_task(db=db, payload=payload)
    understanding_enabled = _understanding_pipeline_enabled(settings)
    include_understanding_payload = bool(settings.message_understanding_payload_enabled)

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
    message_understanding = None
    response_decision = None
    active_revision = None
    conversation_context = None

    if is_task_confirmation_message(payload.content):
        confirmation_source = _resolve_confirmation_source_context(
            db,
            session_id=payload.session_id,
            current_message_id=message.id,
            limit=settings.intent_history_limit,
        )
        source_request_message = confirmation_source.source_request_message
        if source_request_message is None:
            response = _create_chat_mode_response(
                db,
                user_message_id=message.id,
                session_id=payload.session_id,
                assistant_message=TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
            )
            return _finalize_response_with_understanding(
                db,
                response,
                message_id=message.id,
                session_id=payload.session_id,
                understanding=message_understanding,
                response_decision=response_decision,
                active_revision=active_revision,
                include_payload=include_understanding_payload,
            )

        if confirmation_source.source_kind in {"ask_missing_fields", "show_revision"}:
            response = _create_chat_mode_response(
                db,
                user_message_id=message.id,
                session_id=payload.session_id,
                assistant_message=TASK_CONFIRMATION_REQUIRES_REVISION_PROMPT,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
            )
            return _finalize_response_with_understanding(
                db,
                response,
                message_id=message.id,
                session_id=payload.session_id,
                understanding=message_understanding,
                response_decision=response_decision,
                active_revision=active_revision,
                include_payload=include_understanding_payload,
            )

        if confirmation_source.source_kind not in {"legacy_prompt", "confirm_understanding"}:
            response = _create_chat_mode_response(
                db,
                user_message_id=message.id,
                session_id=payload.session_id,
                assistant_message=TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
            )
            return _finalize_response_with_understanding(
                db,
                response,
                message_id=message.id,
                session_id=payload.session_id,
                understanding=message_understanding,
                response_decision=response_decision,
                active_revision=active_revision,
                include_payload=include_understanding_payload,
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
        task_response.intent = intent_result.intent
        task_response.intent_confidence = intent_result.confidence
        task_response.awaiting_task_confirmation = False
        if task_response.task_id is not None:
            active_revision = get_active_revision(db, task_response.task_id)
        message_understanding = _build_confirmation_understanding(
            confirmation_message=payload.content,
            intent_confidence=intent_result.confidence,
            source_request_message=source_request_message,
            source_kind=confirmation_source.source_kind,
            active_revision=active_revision,
        )
        response_decision = _build_confirmation_response_decision(
            task_response=task_response,
            understanding=message_understanding,
            active_revision=active_revision,
        )
        return _finalize_response_with_understanding(
            db,
            task_response,
            message_id=message.id,
            session_id=payload.session_id,
            understanding=message_understanding,
            response_decision=response_decision,
            active_revision=active_revision,
            include_payload=include_understanding_payload,
            task_id=task_response.task_id,
        )

    if understanding_enabled:
        conversation_context = build_conversation_context(
            db,
            session_id=payload.session_id,
            message_id=message.id,
            message_content=payload.content,
            preferred_file_ids=payload.file_ids,
        )
        if conversation_context.latest_active_task_id is not None:
            active_revision = get_active_revision(db, conversation_context.latest_active_task_id)
        message_understanding = understand_message(
            payload.content,
            context=conversation_context,
            task_id=conversation_context.latest_active_task_id,
            db_session=db,
        )
        response_decision = decide_response(
            message_understanding,
            active_revision=active_revision,
            require_approval=True,
            user_preference_profile=conversation_context.user_preference_profile,
            risk_profile=conversation_context.risk_profile,
        )

    chat_upload_files = _resolve_chat_upload_files(
        db,
        session_id=payload.session_id,
        preferred_file_ids=payload.file_ids,
        limit=min(settings.intent_history_limit, 8),
    )

    if _is_upload_access_question(payload.content) or _is_upload_status_followup(
        payload.content,
        history=history,
    ):
        response = _create_chat_mode_response(
            db,
            user_message_id=message.id,
            session_id=payload.session_id,
            assistant_message=_build_upload_access_reply(chat_upload_files),
            intent=intent_result.intent,
            intent_confidence=intent_result.confidence,
        )
        return _finalize_response_with_understanding(
            db,
            response,
            message_id=message.id,
            session_id=payload.session_id,
            understanding=message_understanding,
            response_decision=response_decision,
            active_revision=active_revision,
            include_payload=include_understanding_payload,
        )

    if (
        message_understanding is not None
        and active_revision is None
        and message_understanding.intent
        in {"task_correction", "task_followup", "task_confirmation"}
    ):
        fallback_task = _load_latest_session_task(db, payload.session_id)
        if fallback_task is not None:
            active_revision = get_active_revision(db, fallback_task.id)
            if (
                conversation_context is not None
                and conversation_context.latest_active_task_id is None
            ):
                conversation_context.latest_active_task_id = fallback_task.id

    if (
        message_understanding is not None
        and response_decision is not None
        and response_decision.requires_revision_creation
        and conversation_context is not None
        and conversation_context.latest_active_task_id is not None
        and active_revision is not None
    ):
        active_task = db.get(TaskRunRecord, conversation_context.latest_active_task_id)
        if active_task is not None:
            active_task, active_revision, response_decision = _apply_correction_revision(
                db,
                task=active_task,
                source_message=message,
                understanding=message_understanding,
                response_decision=response_decision,
                active_revision=active_revision,
                uploaded_files=chat_upload_files,
            )

    if response_decision is not None and response_decision.mode in {
        "confirm_understanding",
        "ask_missing_fields",
        "show_revision",
    }:
        response = _create_chat_mode_response(
            db,
            user_message_id=message.id,
            session_id=payload.session_id,
            assistant_message=response_decision.assistant_message,
            intent=intent_result.intent,
            intent_confidence=intent_result.confidence,
        )
        if active_revision is not None:
            response.task_id = getattr(active_revision, "task_id", None)
            task_status = getattr(getattr(active_revision, "task", None), "status", None)
            if isinstance(task_status, str):
                response.task_status = task_status
        return _finalize_response_with_understanding(
            db,
            response,
            message_id=message.id,
            session_id=payload.session_id,
            understanding=message_understanding,
            response_decision=response_decision,
            active_revision=active_revision,
            include_payload=include_understanding_payload,
            task_id=getattr(active_revision, "task_id", None),
        )

    if (
        intent_result.intent == "task"
        and intent_result.confidence >= settings.intent_task_confidence_threshold
    ):
        if settings.intent_task_confirmation_required:
            response = _create_chat_mode_response(
                db,
                user_message_id=message.id,
                session_id=payload.session_id,
                assistant_message=TASK_CONFIRMATION_PROMPT,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
                awaiting_task_confirmation=True,
            )
            return _finalize_response_with_understanding(
                db,
                response,
                message_id=message.id,
                session_id=payload.session_id,
                understanding=message_understanding,
                response_decision=response_decision,
                active_revision=active_revision,
                include_payload=include_understanding_payload,
            )

        task_response = create_message_and_task(
            db=db,
            payload=payload,
            existing_user_message=message,
            task_request_content=payload.content,
        )
        task_response.intent = intent_result.intent
        task_response.intent_confidence = intent_result.confidence
        task_response.awaiting_task_confirmation = False
        return _finalize_response_with_understanding(
            db,
            task_response,
            message_id=message.id,
            session_id=payload.session_id,
            understanding=message_understanding,
            response_decision=response_decision,
            active_revision=active_revision,
            include_payload=include_understanding_payload,
            task_id=task_response.task_id,
        )

    if chat_stream_handler is None:
        assistant_message = generate_chat_reply(
            user_message=payload.content,
            history=history,
            uploaded_files=_serialize_uploaded_files_for_chat(chat_upload_files),
            db_session=db,
        )
    else:
        assistant_message = generate_chat_reply_stream(
            user_message=payload.content,
            history=history,
            uploaded_files=_serialize_uploaded_files_for_chat(chat_upload_files),
            db_session=db,
            on_delta=chat_stream_handler,
        )
    response = _create_chat_mode_response(
        db,
        user_message_id=message.id,
        session_id=payload.session_id,
        assistant_message=assistant_message,
        intent=intent_result.intent,
        intent_confidence=intent_result.confidence,
    )
    return _finalize_response_with_understanding(
        db,
        response,
        message_id=message.id,
        session_id=payload.session_id,
        understanding=message_understanding,
        response_decision=response_decision,
        active_revision=active_revision,
        include_payload=include_understanding_payload,
    )
