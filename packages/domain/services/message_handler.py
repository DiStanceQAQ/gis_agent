from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy.orm import Session

from packages.domain.logging import get_logger
from packages.domain.models import MessageRecord, MessageUnderstandingRecord, TaskRunRecord
from packages.domain.services.intent import is_task_confirmation_message
from packages.domain.services.parser import (
    is_followup_request,
    parse_followup_override_message,
    parse_task_message,
)
from packages.domain.services.response_policy import ResponseDecision
from packages.domain.services.task_lifecycle import _load_latest_session_task, _persist_message
from packages.domain.services.task_revisions import get_active_revision
from packages.domain.services.understanding import (
    MessageUnderstanding,
    persist_message_understanding,
)
from packages.domain.services.upload_handler import (
    UPLOAD_REQUEST_HINT_PATTERN,
    _load_recent_session_file_ids,
)
from packages.schemas.message import MessageCreateResponse
from packages.schemas.task import ParsedTaskSpec
from packages.schemas.understanding import UnderstandingResponse

logger = get_logger(__name__)

TASK_CONFIRMATION_PROMPT = (
    "我理解你想发起一个 GIS 分析任务。请回复“好的，继续”或“开始执行”，我会为你生成计划并进入审批。"
)
TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT = "我收到了确认，但还没找到上一条可执行的任务请求。请先描述分析区域、时间范围和目标，再回复“好的，继续”。"
TASK_CONFIRMATION_REQUIRES_REVISION_PROMPT = (
    "我收到了继续，但当前理解还停留在待修正状态。请直接补充或修改我标出来的字段后，我再继续。"
)


@dataclass(slots=True)
class _ConfirmationSourceContext:
    source_request_message: MessageRecord | None = None
    source_understanding: MessageUnderstandingRecord | None = None
    latest_assistant_message: MessageRecord | None = None
    source_kind: str = "missing"


def _build_confirmation_understanding(
    *,
    confirmation_message: str,
    intent_confidence: float,
    source_request_message: MessageRecord,
    source_kind: str,
    active_revision: object | None,
) -> MessageUnderstanding:
    parsed_spec: ParsedTaskSpec | None = None
    ranked_candidates = {}
    understanding_summary: str | None = (
        f"用户确认按上一条理解继续：{source_request_message.content.strip()}"
    )
    if active_revision is not None:
        raw_spec_json = getattr(active_revision, "raw_spec_json", None)
        if isinstance(raw_spec_json, dict) and raw_spec_json:
            try:
                parsed_spec = ParsedTaskSpec(**raw_spec_json)
            except Exception:
                parsed_spec = None
        revision_summary = getattr(active_revision, "understanding_summary", None)
        if isinstance(revision_summary, str) and revision_summary.strip():
            understanding_summary = revision_summary.strip()
        ranked_candidates = dict(getattr(active_revision, "ranked_candidates_json", {}) or {})

    return MessageUnderstanding(
        intent="task_confirmation",
        intent_confidence=intent_confidence,
        understanding_summary=understanding_summary,
        parsed_spec=parsed_spec,
        parsed_candidates=parsed_spec.model_dump() if parsed_spec is not None else {},
        field_confidences={},
        ranked_candidates=ranked_candidates,
        trace={
            "message_excerpt": confirmation_message[:120],
            "confirmation": {
                "source_message_id": source_request_message.id,
                "source_kind": source_kind,
            },
            "context": {
                "latest_active_task_id": getattr(active_revision, "task_id", None),
                "latest_active_revision_id": getattr(active_revision, "id", None),
                "latest_active_revision_summary": understanding_summary,
            },
        },
    )


def _build_confirmation_response_decision(
    *,
    task_response: MessageCreateResponse,
    understanding: MessageUnderstanding,
    active_revision: object | None,
) -> ResponseDecision:
    execution_blocked = bool(getattr(active_revision, "execution_blocked", False))
    blocked_reason = getattr(active_revision, "execution_blocked_reason", None)
    need_clarification = bool(task_response.need_clarification or execution_blocked)
    editable_fields = list(task_response.missing_fields) if need_clarification else []
    mode = "ask_missing_fields" if need_clarification else "execute_now"
    response_payload: dict[str, object] = {
        "response_mode": mode,
        "intent": understanding.intent,
        "intent_confidence": understanding.intent_confidence,
        "understanding_summary": understanding.understanding_summary,
        "editable_fields": editable_fields,
        "missing_fields": list(task_response.missing_fields),
        "require_approval": bool(task_response.need_approval),
        "field_confidences": understanding.field_confidences_dump(),
        "field_evidence": understanding.field_evidence_dump(),
        "ranked_candidates": understanding.ranked_candidates,
        "execution_blocked": execution_blocked,
        "blocked_reason": blocked_reason,
        "requires_task_creation": False,
        "requires_revision_creation": False,
        "requires_execution": bool(
            not task_response.need_approval
            and not task_response.need_clarification
            and not execution_blocked
        ),
    }
    if active_revision is not None:
        response_payload["revision_number"] = getattr(active_revision, "revision_number", None)
        response_payload["revision_change_type"] = getattr(active_revision, "change_type", None)
        response_payload["active_revision_summary"] = getattr(
            active_revision, "understanding_summary", None
        )
        response_payload["active_response_mode"] = getattr(active_revision, "response_mode", None)

    return ResponseDecision(
        mode=mode,
        assistant_message=task_response.assistant_message or "",
        editable_fields=editable_fields,
        response_payload=response_payload,
        execution_blocked=execution_blocked,
        requires_task_creation=False,
        requires_revision_creation=False,
        requires_execution=bool(response_payload["requires_execution"]),
    )


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
        query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc()).limit(limit).all()
    )
    return [{"role": record.role, "content": record.content} for record in reversed(records)]


def _parse_candidate_request_content(content: str, *, has_upload: bool) -> ParsedTaskSpec:
    return parse_task_message(content, has_upload=has_upload)


def _is_executable_followup_request(content: str, *, latest_task: TaskRunRecord | None) -> bool:
    if latest_task is None or not is_followup_request(content):
        return False
    return bool(parse_followup_override_message(content, has_upload=False))


def _load_latest_confirmation_candidate_message(
    prior_messages: list[MessageRecord],
) -> MessageRecord | None:
    if not prior_messages:
        return None
    for message in prior_messages[1:]:
        if message.role == "assistant":
            break
        if message.role != "user" or message.linked_task_id is not None:
            continue
        content = message.content.strip()
        if not content or is_task_confirmation_message(content):
            continue
        return message
    return None


def _resolve_confirmation_source_context(
    db: Session,
    *,
    session_id: str,
    current_message_id: str,
    limit: int,
) -> _ConfirmationSourceContext:
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
        return _ConfirmationSourceContext()

    latest_message = prior_messages[0]
    if latest_message.role != "assistant":
        return _ConfirmationSourceContext(latest_assistant_message=latest_message)

    if latest_message.content == TASK_CONFIRMATION_PROMPT:
        source_request_message = _find_latest_executable_request_message(
            db,
            session_id=session_id,
            current_message_id=current_message_id,
            confirmation_prompt_message=latest_message,
            has_recent_uploads=bool(
                _load_recent_session_file_ids(db, session_id=session_id, limit=limit)
            ),
            limit=limit,
        )
        return _ConfirmationSourceContext(
            source_request_message=source_request_message,
            latest_assistant_message=latest_message,
            source_kind="legacy_prompt" if source_request_message is not None else "missing",
        )

    source_request_message = _load_latest_confirmation_candidate_message(prior_messages)
    if source_request_message is None:
        return _ConfirmationSourceContext(latest_assistant_message=latest_message)

    source_understanding = (
        db.query(MessageUnderstandingRecord)
        .filter(MessageUnderstandingRecord.message_id == source_request_message.id)
        .one_or_none()
    )
    source_kind = source_understanding.response_mode if source_understanding else "missing"
    return _ConfirmationSourceContext(
        source_request_message=source_request_message,
        source_understanding=source_understanding,
        latest_assistant_message=latest_message,
        source_kind=source_kind or "missing",
    )


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
        return message
    return None


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


def _understanding_pipeline_enabled(settings: object) -> bool:
    return bool(
        getattr(settings, "conversation_context_enabled", False)
        and getattr(settings, "understanding_engine_enabled", False)
        and getattr(settings, "response_mode_enabled", False)
    )


def _serialize_understanding_response(
    *,
    understanding: MessageUnderstanding,
    response_decision: ResponseDecision,
    active_revision: object | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": understanding.intent,
        "intent_confidence": understanding.intent_confidence,
        "understanding_summary": understanding.understanding_summary,
        "editable_fields": list(response_decision.editable_fields),
        "field_confidences": understanding.field_confidences_dump(),
        "field_evidence": understanding.field_evidence_dump(),
        "ranked_candidates": deepcopy(understanding.ranked_candidates),
    }
    if active_revision is not None:
        revision_id = getattr(active_revision, "id", None)
        revision_number = getattr(active_revision, "revision_number", None)
        if isinstance(revision_id, str) and revision_id:
            payload["revision_id"] = revision_id
        if isinstance(revision_number, int):
            payload["revision_number"] = revision_number
    return payload


def _attach_understanding_metadata(
    response: MessageCreateResponse,
    *,
    understanding: MessageUnderstanding | None,
    response_decision: ResponseDecision | None,
    active_revision: object | None,
    include_payload: bool,
) -> MessageCreateResponse:
    if understanding is None or response_decision is None:
        return response

    response.response_mode = response_decision.mode
    if include_payload:
        response.understanding = UnderstandingResponse.model_validate(
            _serialize_understanding_response(
                understanding=understanding,
                response_decision=response_decision,
                active_revision=active_revision,
            )
        )
        response.response_payload = deepcopy(response_decision.response_payload)
    return response


def _persist_understanding_metadata(
    db: Session,
    *,
    message_id: str,
    session_id: str,
    understanding: MessageUnderstanding | None,
    response_decision: ResponseDecision | None,
    task_id: str | None = None,
    active_revision: object | None = None,
) -> None:
    if understanding is None or response_decision is None:
        return

    persist_message_understanding(
        db,
        message_id=message_id,
        session_id=session_id,
        understanding=understanding,
        task_id=task_id,
        derived_revision_id=getattr(active_revision, "id", None),
        response_mode=response_decision.mode,
    )


def _finalize_response_with_understanding(
    db: Session,
    response: MessageCreateResponse,
    *,
    message_id: str,
    session_id: str,
    understanding: MessageUnderstanding | None,
    response_decision: ResponseDecision | None,
    active_revision: object | None,
    include_payload: bool,
    task_id: str | None = None,
) -> MessageCreateResponse:
    resolved_active_revision = active_revision
    if resolved_active_revision is None and task_id is not None:
        resolved_active_revision = get_active_revision(db, task_id)

    response = _attach_understanding_metadata(
        response,
        understanding=understanding,
        response_decision=response_decision,
        active_revision=resolved_active_revision,
        include_payload=include_payload,
    )
    _persist_understanding_metadata(
        db,
        message_id=message_id,
        session_id=session_id,
        understanding=understanding,
        response_decision=response_decision,
        task_id=task_id,
        active_revision=resolved_active_revision,
    )
    if understanding is not None and response_decision is not None:
        db.commit()
    return response
