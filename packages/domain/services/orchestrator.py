from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re

from fastapi import UploadFile
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload

from apps.worker.tasks import run_task_async
from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import (
    AOIRecord,
    MessageRecord,
    MessageUnderstandingRecord,
    SessionMemoryEventRecord,
    SessionMemoryLinkRecord,
    SessionMemorySummaryRecord,
    SessionRecord,
    SessionStateSnapshotRecord,
    TaskRunRecord,
    TaskSpecRevisionRecord,
    TaskSpecRecord,
    UploadedFileRecord,
)
from packages.domain.services.aoi import (
    build_named_aoi_clarification_message,
    clone_task_aoi,
    normalize_active_revision_aoi,
    normalize_task_aoi,
    upsert_task_aoi,
)
from packages.domain.services.agent_runtime import run_task_runtime
from packages.domain.services.chat import generate_chat_reply, generate_chat_reply_stream
from packages.domain.services.conversation_context import build_conversation_context
from packages.domain.services.input_slots import classify_uploaded_inputs
from packages.domain.services.intent import classify_message_intent, is_task_confirmation_message
from packages.domain.services.operation_plan_builder import build_operation_plan_from_registry
from packages.domain.services.processing_pipeline import preflight_processing_pipeline_inputs
from packages.domain.services.planner import PLAN_STATUS_NEEDS_CLARIFICATION, build_task_plan
from packages.domain.services.parser import (
    extract_parser_failure_error,
    is_followup_request,
    parse_followup_override_message,
    parse_task_message,
)
from packages.domain.services.plan_guard import validate_operation_plan
from packages.domain.services.response_policy import ResponseDecision, decide_response
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.storage import (
    build_artifact_path,
    detect_file_type,
    write_upload_file,
)
from packages.domain.services.task_events import append_task_event, list_task_events
from packages.domain.services.task_revisions import (
    activate_revision,
    create_correction_revision,
    create_initial_revision,
    ensure_initial_revision_for_task,
    get_active_revision,
)
from packages.domain.services.task_state import (
    TASK_STATUS_APPROVED,
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_task_status,
    write_task_error,
)
from packages.domain.services.understanding import (
    MessageUnderstanding,
    persist_message_understanding,
    understand_message,
)
from packages.domain.utils import make_id, merge_dicts
from packages.schemas.file import UploadedFileResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse
from packages.schemas.operation_plan import OperationNode, OperationPlan
from packages.schemas.session import (
    SessionMemoryLineageItemResponse,
    SessionMemoryResponse,
    SessionMessageItemResponse,
    SessionMessagesResponse,
    SessionResponse,
    SessionTaskItemResponse,
    SessionTasksResponse,
)
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
from packages.schemas.understanding import UnderstandingResponse

logger = get_logger(__name__)

TASK_CONFIRMATION_PROMPT = (
    "我理解你想发起一个 GIS 分析任务。请回复“好的，继续”或“开始执行”，我会为你生成计划并进入审批。"
)
TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT = "我收到了确认，但还没找到上一条可执行的任务请求。请先描述分析区域、时间范围和目标，再回复“好的，继续”。"
TASK_CONFIRMATION_REQUIRES_REVISION_PROMPT = (
    "我收到了继续，但当前理解还停留在待修正状态。请直接补充或修改我标出来的字段后，我再继续。"
)
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


@dataclass(slots=True)
class _ParsedTaskUnderstanding:
    intent: str
    understanding_summary: str | None
    parsed_spec: ParsedTaskSpec | None
    ranked_candidates: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    trace: dict[str, object] = field(default_factory=dict)
    _field_confidences: dict[str, object] = field(default_factory=dict)

    def field_confidences_dump(self) -> dict[str, object]:
        return dict(self._field_confidences)


@dataclass(slots=True)
class _ConfirmationSourceContext:
    source_request_message: MessageRecord | None = None
    source_understanding: MessageUnderstandingRecord | None = None
    latest_assistant_message: MessageRecord | None = None
    source_kind: str = "missing"


def _build_initial_revision_understanding(
    *,
    task_spec_raw: dict[str, object],
    parent_task_id: str | None,
) -> _ParsedTaskUnderstanding:
    return _ParsedTaskUnderstanding(
        intent="new_task" if parent_task_id is None else "task_followup",
        understanding_summary=None,
        parsed_spec=ParsedTaskSpec(**task_spec_raw),
    )


def _require_named_aoi_clarification(parsed: ParsedTaskSpec) -> ParsedTaskSpec:
    missing_fields = list(parsed.missing_fields)
    if "aoi_boundary" not in missing_fields:
        missing_fields.append("aoi_boundary")
    return parsed.model_copy(
        update={
            "need_confirmation": True,
            "missing_fields": missing_fields,
            "clarification_message": build_named_aoi_clarification_message(
                parsed.aoi_input or "当前研究区"
            ),
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


def _build_revision_summary(revision: object) -> dict[str, object]:
    return {
        "revision_id": getattr(revision, "id"),
        "revision_number": getattr(revision, "revision_number"),
        "change_type": getattr(revision, "change_type"),
        "created_at": getattr(revision, "created_at", None),
        "understanding_summary": getattr(revision, "understanding_summary", None),
        "execution_blocked": bool(getattr(revision, "execution_blocked", False)),
        "execution_blocked_reason": getattr(revision, "execution_blocked_reason", None),
        "field_confidences": dict(getattr(revision, "field_confidences_json", {}) or {}),
        "ranked_candidates": dict(getattr(revision, "ranked_candidates_json", {}) or {}),
    }


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
    understanding_summary: str | None = f"用户确认按上一条理解继续：{source_request_message.content.strip()}"
    if active_revision is not None:
        raw_spec_json = getattr(active_revision, "raw_spec_json", None)
        if isinstance(raw_spec_json, dict) and raw_spec_json:
            try:
                parsed_spec = ParsedTaskSpec(**raw_spec_json)
            except Exception:  # pragma: no cover - defensive fallback
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
            not task_response.need_approval and not task_response.need_clarification and not execution_blocked
        ),
    }
    if active_revision is not None:
        response_payload["revision_number"] = getattr(active_revision, "revision_number", None)
        response_payload["revision_change_type"] = getattr(active_revision, "change_type", None)
        response_payload["active_revision_summary"] = getattr(active_revision, "understanding_summary", None)
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


def _build_revision_override_payload(understanding: MessageUnderstanding) -> dict[str, object]:
    if understanding.parsed_spec is None:
        return {}
    override = understanding.parsed_spec.model_dump(exclude_none=True)
    if "created_from" not in override:
        override["created_from"] = "revision"
    return override


def _apply_correction_revision(
    db: Session,
    *,
    task: TaskRunRecord,
    source_message: MessageRecord,
    understanding: MessageUnderstanding,
    response_decision: ResponseDecision,
    active_revision: object | None,
    uploaded_files: list[UploadedFileRecord],
) -> tuple[TaskRunRecord, object, ResponseDecision]:
    if active_revision is None:
        return task, active_revision, response_decision

    base_raw_spec = dict(getattr(active_revision, "raw_spec_json", {}) or {})
    override = _build_revision_override_payload(understanding)
    merged_raw_spec = merge_dicts(base_raw_spec, override)
    correction_revision = create_correction_revision(
        db,
        task=task,
        base_revision=active_revision,
        understanding=understanding,
        source_message_id=source_message.id,
        raw_spec_json=merged_raw_spec,
    )
    correction_revision.response_mode = response_decision.mode
    correction_revision.response_payload_json = deepcopy(response_decision.response_payload)
    activate_revision(db, task=task, revision=correction_revision)

    refresh_result = normalize_active_revision_aoi(
        db,
        task=task,
        revision=correction_revision,
        uploaded_files=uploaded_files,
    )
    if refresh_result.execution_blocked:
        blocked_reason = correction_revision.execution_blocked_reason or "AOI normalization failed."
        missing_fields = list(response_decision.response_payload.get("missing_fields", []))
        if "aoi_boundary" not in missing_fields:
            missing_fields.append("aoi_boundary")
        if task.task_spec is not None:
            task.task_spec.need_confirmation = True
            raw_spec = dict(task.task_spec.raw_spec_json or {})
            raw_spec["need_confirmation"] = True
            raw_spec["missing_fields"] = missing_fields
            raw_spec["clarification_message"] = blocked_reason
            task.task_spec.raw_spec_json = raw_spec
        correction_revision.response_mode = "ask_missing_fields"
        correction_revision.response_payload_json = {
            **(correction_revision.response_payload_json or {}),
            "response_mode": "ask_missing_fields",
            "execution_blocked": True,
            "blocked_reason": blocked_reason,
            "missing_fields": missing_fields,
        }
        task.last_response_mode = "ask_missing_fields"
        response_decision = ResponseDecision(
            mode="ask_missing_fields",
            assistant_message=blocked_reason,
            editable_fields=missing_fields,
            response_payload=dict(correction_revision.response_payload_json or {}),
            execution_blocked=True,
            requires_task_creation=False,
            requires_revision_creation=False,
            requires_execution=False,
        )
    else:
        if task.task_spec is not None:
            task.task_spec.need_confirmation = False
            raw_spec = dict(task.task_spec.raw_spec_json or {})
            raw_spec["need_confirmation"] = False
            raw_spec["missing_fields"] = []
            raw_spec["clarification_message"] = None
            task.task_spec.raw_spec_json = raw_spec
        task.interaction_state = "understanding"
        set_task_status(
            db,
            task,
            TASK_STATUS_AWAITING_APPROVAL,
            current_step="awaiting_approval",
            event_type="task_revision_activated",
            detail={
                "revision_id": correction_revision.id,
                "revision_number": correction_revision.revision_number,
            },
            force=True,
        )
        correction_revision.response_mode = "show_revision"
        correction_revision.response_payload_json = {
            **(correction_revision.response_payload_json or {}),
            "response_mode": "show_revision",
            "execution_blocked": False,
            "blocked_reason": None,
        }
        task.last_response_mode = "show_revision"
        response_decision = ResponseDecision(
            mode="show_revision",
            assistant_message=response_decision.assistant_message,
            editable_fields=response_decision.editable_fields,
            response_payload={
                **response_decision.response_payload,
                "response_mode": "show_revision",
                "requires_revision_creation": False,
                "execution_blocked": False,
                "blocked_reason": None,
            },
            execution_blocked=False,
            requires_task_creation=False,
            requires_revision_creation=False,
            requires_execution=False,
        )

    db.flush()
    return task, correction_revision, response_decision


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
    if snapshot is None or (snapshot.active_task_id is None and snapshot.active_revision_id is None):
        legacy_task, legacy_revision = _load_latest_session_memory_anchor(db, session_id=session_id)
        if legacy_task is not None or legacy_revision is not None:
            snapshot = memory.refresh_snapshot(
                session_id,
                task_id=legacy_task.id if legacy_task is not None else None,
                revision_id=legacy_revision.id if legacy_revision is not None else None,
            )
    latest_summary = (
        db.get(SessionMemorySummaryRecord, snapshot.latest_summary_id)
        if snapshot is not None and isinstance(snapshot.latest_summary_id, str) and snapshot.latest_summary_id
        else None
    )

    understandings = (
        db.query(MessageUnderstandingRecord)
        .options(selectinload(MessageUnderstandingRecord.message))
        .filter(MessageUnderstandingRecord.session_id == session_id)
        .order_by(MessageUnderstandingRecord.created_at.desc(), MessageUnderstandingRecord.id.desc())
        .limit(limit)
        .all()
    )
    revision_ids = {
        row.derived_revision_id
        for row in understandings
        if isinstance(row.derived_revision_id, str) and row.derived_revision_id
    }
    revisions = (
        db.query(TaskSpecRevisionRecord)
        .filter(TaskSpecRevisionRecord.id.in_(revision_ids))
        .all()
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
            message_excerpt=_excerpt(row.message.content, limit=120) if row.message is not None else None,
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
        query.order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc()).limit(limit).all()
    )
    return [{"role": record.role, "content": record.content} for record in reversed(records)]


def _parse_candidate_request_content(content: str, *, has_upload: bool) -> ParsedTaskSpec:
    return parse_task_message(content, has_upload=has_upload)


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
        # Fallback: once the user has explicitly confirmed execution, we still treat
        # the nearest pre-confirmation user request as executable context, even if
        # parser extraction is incomplete for that message.
        return message
    return None


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
            file_id
            for file_id in file_ids
            if files_by_id[file_id].session_id != session_id
        ]
        if invalid_file_ids:
            raise AppError.bad_request(
                error_code=ErrorCode.BAD_REQUEST,
                message="One or more uploaded files do not belong to the current session.",
                detail={"session_id": session_id, "file_ids": invalid_file_ids},
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


def _build_initial_operation_plan(
    task_plan: TaskPlanResponse,
    parsed: ParsedTaskSpec,
    *,
    uploaded_files: list[UploadedFileRecord] | None = None,
    upload_slots: dict[str, str] | None = None,
) -> OperationPlan:
    if task_plan.operation_plan_nodes:
        try:
            candidate = OperationPlan(
                version=1,
                status="draft",
                missing_fields=list(task_plan.missing_fields),
                nodes=[
                    OperationNode.model_validate(node) for node in task_plan.operation_plan_nodes
                ],
            )
            injected = _inject_upload_context_into_operation_plan(
                candidate,
                parsed=parsed,
                uploaded_files=uploaded_files,
                upload_slots=upload_slots,
            )
            return validate_operation_plan(injected).model_copy(update={"status": "draft"})
        except Exception as exc:  # pragma: no cover - fallback guard
            logger.warning("task.operation_plan_from_planner_invalid detail=%s", repr(exc))

    plan = build_operation_plan_from_registry(
        parsed,
        version=1,
        status="draft",
        missing_fields=list(task_plan.missing_fields),
    )
    injected = _inject_upload_context_into_operation_plan(
        plan,
        parsed=parsed,
        uploaded_files=uploaded_files,
        upload_slots=upload_slots,
    )
    return validate_operation_plan(injected).model_copy(update={"status": "draft"})


def _load_latest_session_task(db: Session, session_id: str) -> TaskRunRecord | None:
    task = (
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
    if task is not None and task.task_spec is not None:
        ensure_initial_revision_for_task(db, task)
    return task


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
    defer_revision_aoi_normalization = safe_parsed.aoi_source_type == "file_upload"
    upload_slots = classify_uploaded_inputs(uploaded_files or [])
    parser_error_code, parser_error_message = extract_parser_failure_error(safe_parsed)
    if existing_task is None:
        task = TaskRunRecord(
            id=make_id("task"),
            session_id=session_id,
            parent_task_id=parent_task_id,
            user_message_id=user_message_id,
            status=TASK_STATUS_WAITING_CLARIFICATION
            if safe_parsed.need_confirmation
            else TASK_STATUS_QUEUED,
            current_step="parse_task",
            analysis_type=safe_parsed.analysis_type,
            requested_time_range=safe_parsed.time_range,
        )
        db.add(task)
        db.flush()
    else:
        task = existing_task
        task.parent_task_id = parent_task_id
        task.status = (
            TASK_STATUS_WAITING_CLARIFICATION
            if safe_parsed.need_confirmation
            else TASK_STATUS_QUEUED
        )
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

    if not defer_revision_aoi_normalization:
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
        merged_missing_fields = list(
            dict.fromkeys([*safe_parsed.missing_fields, *task_plan.missing_fields])
        )
        safe_parsed = safe_parsed.model_copy(
            update={
                "need_confirmation": True,
                "missing_fields": merged_missing_fields,
                "clarification_message": safe_parsed.clarification_message
                or "任务规划需要补充信息后重试。",
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
            uploaded_files=uploaded_files,
            upload_slots=upload_slots,
        )
        task.plan_json = {
            **(task.plan_json or {}),
            "operation_plan": initial_operation_plan.model_dump(),
            "operation_plan_version": initial_operation_plan.version,
        }
        task.status = TASK_STATUS_AWAITING_APPROVAL
        task.current_step = "awaiting_approval"

    task_spec_raw = dict(task_spec.raw_spec_json or {})
    understanding = _build_initial_revision_understanding(
        task_spec_raw=task_spec_raw,
        parent_task_id=parent_task_id,
    )
    revision = create_initial_revision(
        db,
        task=task,
        understanding=understanding,
        source_message_id=user_message_id,
        raw_spec_json=task_spec_raw,
    )
    revision.response_mode = (
        "ask_missing_fields"
        if task_spec.need_confirmation
        else "confirm_understanding"
    )
    activate_revision(db, task=task, revision=revision)

    if defer_revision_aoi_normalization:
        refresh_result = normalize_active_revision_aoi(
            db,
            task=task,
            revision=revision,
            uploaded_files=uploaded_files,
        )
        if refresh_result.execution_blocked:
            blocked_reason = revision.execution_blocked_reason or "AOI normalization failed."
            blocked_missing_fields = list(safe_parsed.missing_fields)
            if "aoi_boundary" not in blocked_missing_fields:
                blocked_missing_fields.append("aoi_boundary")
            safe_parsed = safe_parsed.model_copy(
                update={
                    "need_confirmation": True,
                    "missing_fields": blocked_missing_fields,
                    "clarification_message": blocked_reason,
                }
            )
            task_spec.need_confirmation = True
            task_spec.raw_spec_json = _build_task_spec_raw_payload(safe_parsed, upload_slots)
            revision.response_mode = "ask_missing_fields"
            revision.response_payload_json = {
                "response_mode": "ask_missing_fields",
                "execution_blocked": True,
                "blocked_reason": blocked_reason,
            }
            task.last_response_mode = "ask_missing_fields"
            db.flush()

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
    require_approval: bool = True,
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
        files = _load_uploaded_files_by_payload_order(
            db,
            file_ids=payload.file_ids,
            session_id=payload.session_id,
        )

    if existing_user_message is None:
        message = _persist_message(
            db,
            session_id=payload.session_id,
            role="user",
            content=payload.content,
        )
        SessionMemoryService(db).record_event(
            session_id=payload.session_id,
            event_type="message_received",
            message_id=message.id,
            event_payload={"role": "user"},
        )
    else:
        message = existing_user_message

    provisional_task = TaskRunRecord(
        id=make_id("task"),
        session_id=payload.session_id,
        user_message_id=message.id,
        status=TASK_STATUS_QUEUED,
        current_step="parse_task",
        analysis_type="WORKFLOW",
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
            if parent_task is not None
            and should_inherit_original_aoi(parent_task, parsed, override)
            else None
        ),
        require_approval=require_approval,
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
    need_approval = task.status == TASK_STATUS_AWAITING_APPROVAL

    if need_clarification:
        assistant_message = str(
            response_spec.get("clarification_message")
            or "我还需要补充信息，才能继续执行该 GIS 任务。"
        )
    elif need_approval:
        assistant_message = "已生成执行计划，请确认后开始执行。"
    else:
        assistant_message = "收到，已开始执行 GIS 处理。我会持续同步进度和结果。"

    _persist_message(
        db,
        session_id=payload.session_id,
        role="assistant",
        content=assistant_message,
        linked_task_id=task.id,
    )

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
        assistant_message=assistant_message,
        need_clarification=need_clarification,
        need_approval=need_approval,
        missing_fields=list(response_spec.get("missing_fields", [])),
        clarification_message=response_spec.get("clarification_message"),
    )


def create_message(
    db: Session,
    payload: MessageCreateRequest,
    *,
    chat_stream_handler: Callable[[str], None] | None = None,
) -> MessageCreateResponse:
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
    SessionMemoryService(db).record_event(
        session_id=payload.session_id,
        event_type="message_received",
        message_id=message.id,
        event_payload={"role": "user"},
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
    message_understanding: MessageUnderstanding | None = None
    response_decision: ResponseDecision | None = None
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
        and message_understanding.intent in {"task_correction", "task_followup", "task_confirmation"}
    ):
        fallback_task = _load_latest_session_task(db, payload.session_id)
        if fallback_task is not None:
            active_revision = get_active_revision(db, fallback_task.id)
            if conversation_context is not None and conversation_context.latest_active_task_id is None:
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


def _load_task(db: Session, task_id: str) -> TaskRunRecord:
    task = (
        db.query(TaskRunRecord)
        .options(
            selectinload(TaskRunRecord.task_spec),
            selectinload(TaskRunRecord.aoi),
            selectinload(TaskRunRecord.candidates),
            selectinload(TaskRunRecord.steps),
            selectinload(TaskRunRecord.artifacts),
            selectinload(TaskRunRecord.revisions),
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
    if task.task_spec is not None:
        ensure_initial_revision_for_task(db, task)
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
    revisions = sorted(task.revisions, key=lambda item: item.revision_number)
    active_revision = next((revision for revision in revisions if revision.is_active), None)
    if active_revision is None:
        active_revision = get_active_revision(db, task.id)
    if active_revision is not None and all(
        revision.id != active_revision.id for revision in revisions
    ):
        revisions.append(active_revision)
        revisions.sort(key=lambda item: item.revision_number)

    return TaskDetailResponse(
        task_id=task.id,
        parent_task_id=task.parent_task_id,
        status=task.status,
        interaction_state=task.interaction_state,
        last_response_mode=task.last_response_mode,
        approval_required=task.status == TASK_STATUS_AWAITING_APPROVAL,
        execution_mode=(task.plan_json or {}).get("execution_mode"),
        execution_submitted=bool(
            (task.plan_json or {}).get("execution_submitted")
            or task.status in {"queued", "running", "success", "failed"}
        ),
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
        active_revision=_build_revision_summary(active_revision) if active_revision is not None else None,
        revisions=[_build_revision_summary(revision) for revision in revisions],
        summary_text=task.result_summary_text,
        methods_text=task.methods_text,
        png_preview_url=png_preview_url,
        aoi_name=task.aoi.name if task.aoi else None,
        aoi_bbox_bounds=list(task.aoi.bbox_bounds_json)
        if task.aoi and task.aoi.bbox_bounds_json
        else None,
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
            detail={
                "task_id": task_id,
                "latest_version": latest_version,
                "patched_version": plan.version,
            },
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
    try:
        preflight_processing_pipeline_inputs(
            task_id=task.id,
            plan_nodes=[node.model_dump() for node in approved_plan.nodes],
            working_dir=Path(build_artifact_path(task.id, "pipeline_preflight.tmp")).parent,
        )
    except Exception as exc:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_EDIT_INVALID,
            message="Task plan failed preflight input validation.",
            detail={
                "task_id": task.id,
                "cause_error_code": ErrorCode.PLAN_SCHEMA_INVALID,
                "cause_message": str(exc),
            },
        ) from exc

    execution_mode = get_settings().execution_mode
    task.plan_json = {
        **task.plan_json,
        "operation_plan": approved_plan.model_dump(),
        "operation_plan_version": approved_plan.version,
        "execution_mode": execution_mode,
        "execution_submitted": True,
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
        detail={"execution_mode": execution_mode},
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
        inherited_aoi=original_task.aoi
        if should_inherit_original_aoi(original_task, parsed, override)
        else None,
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
        execution_mode = get_settings().execution_mode
        task.plan_json = {
            **(task.plan_json or {}),
            "execution_mode": execution_mode,
            "execution_submitted": True,
        }
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_queued",
            status=task.status,
            detail={"execution_mode": execution_mode, "rerun": True},
        )
    db.commit()
    if task.task_spec is not None and not task.task_spec.need_confirmation:
        _queue_or_run(task.id)
    return get_task_detail(db=db, task_id=task.id)
