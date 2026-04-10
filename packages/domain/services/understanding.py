from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.models import MessageUnderstandingRecord, TaskSpecRevisionRecord
from packages.domain.services.conversation_context import ConversationContextBundle
from packages.domain.services.session_memory_confidence import (
    build_history_features,
    compute_field_confidence,
)
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.intent import IntentResult, classify_message_intent
from packages.domain.services.parser import parse_task_message
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec


IntentKind = Literal[
    "new_task",
    "task_correction",
    "task_confirmation",
    "task_followup",
    "chat",
    "ambiguous",
]

ResponseMode = Literal[
    "execute_now",
    "confirm_understanding",
    "ask_missing_fields",
    "show_revision",
    "chat_reply",
]

TASK_LIKE_INTENTS: tuple[IntentKind, ...] = (
    "new_task",
    "task_correction",
    "task_confirmation",
    "task_followup",
)


@dataclass(slots=True)
class EvidenceItem:
    field: str
    source: str
    weight: float
    detail: str
    message_ref: str | None = None
    upload_file_id: str | None = None

    def dump(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "field": self.field,
            "source": self.source,
            "weight": self.weight,
            "detail": self.detail,
        }
        if self.message_ref is not None:
            payload["message_ref"] = self.message_ref
        if self.upload_file_id is not None:
            payload["upload_file_id"] = self.upload_file_id
        return payload


@dataclass(slots=True)
class FieldConfidence:
    score: float
    level: Literal["low", "medium", "high"]
    evidence: list[EvidenceItem] = field(default_factory=list)

    def dump(self) -> dict[str, object]:
        return {
            "score": self.score,
            "level": self.level,
            "evidence": [item.dump() for item in self.evidence],
        }


@dataclass(slots=True)
class MessageUnderstanding:
    intent: IntentKind
    intent_confidence: float
    understanding_summary: str
    parsed_spec: ParsedTaskSpec | None
    parsed_candidates: dict[str, object]
    field_confidences: dict[str, FieldConfidence]
    ranked_candidates: dict[str, list[dict[str, object]]]
    trace: dict[str, object]

    def field_confidences_dump(self) -> dict[str, object]:
        return {field: confidence.dump() for field, confidence in self.field_confidences.items()}

    def field_evidence_dump(self) -> dict[str, list[str]]:
        return {
            field: [_format_field_evidence(item) for item in confidence.evidence]
            for field, confidence in self.field_confidences.items()
        }


def understand_message(
    message: str,
    *,
    context: ConversationContextBundle,
    task_id: str | None = None,
    db_session: Session | None = None,
) -> MessageUnderstanding:
    settings = get_settings()
    resolved_task_id = task_id or context.latest_active_task_id

    history = _build_history(context)
    base_intent = classify_message_intent(
        message,
        history=history,
        task_id=resolved_task_id,
        db_session=db_session,
    )
    intent, promotion_reason, intent_confidence = _expand_intent(
        base_intent,
        context=context,
    )

    parsed_spec: ParsedTaskSpec | None = None
    parser_payload: dict[str, object] = {}
    if intent in TASK_LIKE_INTENTS:
        has_upload = _has_upload_context(context)
        parsed_spec = parse_task_message(
            message,
            has_upload=has_upload,
            task_id=resolved_task_id,
            db_session=db_session,
            context_summary=_parser_context_summary(context),
            field_value_history=context.field_value_history,
        )
        parser_payload = parsed_spec.model_dump()

    field_confidences = _build_field_confidences(
        intent=intent,
        parsed_spec=parsed_spec,
        context=context,
        message=message,
        settings=settings,
    )
    ranked_candidates = _build_ranked_candidates(field_confidences, parsed_spec)
    parsed_candidates = parser_payload

    understanding_summary = _build_understanding_summary(
        intent=intent,
        parsed_spec=parsed_spec,
        context=context,
        base_intent=base_intent,
    )

    trace = {
        "message_excerpt": _excerpt(message),
        "context": {
            "session_id": context.session_id,
            "message_id": context.message_id,
            "latest_active_task_id": context.latest_active_task_id,
            "latest_active_revision_id": context.latest_active_revision_id,
            "latest_active_revision_summary": context.latest_active_revision_summary,
            "explicit_signals": context.explicit_signals,
            "history_features": context.history_features,
        },
        "context_builder": deepcopy(context.trace),
        "classifier": {
            "intent": base_intent.intent,
            "confidence": base_intent.confidence,
            "reason": base_intent.reason,
        },
        "promotion": promotion_reason,
        "parser": {
            "invoked": parsed_spec is not None,
            "task_id": resolved_task_id,
            "has_upload": _has_upload_context(context) if parsed_spec is not None else None,
        },
        "field_confidences": {
            field: confidence.dump() for field, confidence in field_confidences.items()
        },
        "ranked_candidates": ranked_candidates,
    }

    return MessageUnderstanding(
        intent=intent,
        intent_confidence=intent_confidence,
        understanding_summary=understanding_summary,
        parsed_spec=parsed_spec,
        parsed_candidates=parsed_candidates,
        field_confidences=field_confidences,
        ranked_candidates=ranked_candidates,
        trace=trace,
    )


def persist_message_understanding(
    db_session: Session,
    *,
    message_id: str,
    session_id: str,
    understanding: MessageUnderstanding,
    task_id: str | None = None,
    derived_revision_id: str | None = None,
    response_mode: ResponseMode | None = None,
) -> MessageUnderstandingRecord:
    existing = (
        db_session.query(MessageUnderstandingRecord)
        .filter(MessageUnderstandingRecord.message_id == message_id)
        .one_or_none()
    )

    resolved_task_id = task_id
    if resolved_task_id is None:
        resolved_task_id = _extract_context_value(
            understanding.trace,
            ("context", "latest_active_task_id"),
        )

    resolved_revision_id = derived_revision_id
    if resolved_revision_id is None:
        resolved_revision_id = _extract_context_value(
            understanding.trace,
            ("context", "latest_active_revision_id"),
        )

    resolved_response_mode = response_mode
    if resolved_response_mode is None:
        response_mode_from_trace = understanding.trace.get("response_mode")
        if isinstance(response_mode_from_trace, str) and response_mode_from_trace:
            resolved_response_mode = response_mode_from_trace  # type: ignore[assignment]

    payload = {
        "session_id": session_id,
        "task_id": resolved_task_id,
        "derived_revision_id": resolved_revision_id,
        "history_features_json": _extract_context_object(
            understanding.trace,
            ("context", "history_features"),
        ),
        "intent": understanding.intent,
        "intent_confidence": understanding.intent_confidence,
        "understanding_summary": understanding.understanding_summary,
        "response_mode": resolved_response_mode,
        "field_confidences_json": understanding.field_confidences_dump(),
        "field_evidence_json": understanding.field_evidence_dump(),
        "context_trace_json": understanding.trace,
    }

    if existing is None:
        record = MessageUnderstandingRecord(
            id=make_id("und"),
            message_id=message_id,
            **payload,
        )
        db_session.add(record)
    else:
        record = existing
        for key, value in payload.items():
            setattr(record, key, value)

    db_session.flush()
    resolved_revision = (
        db_session.get(TaskSpecRevisionRecord, resolved_revision_id)
        if resolved_revision_id
        else None
    )
    memory = SessionMemoryService(db_session)
    if (
        resolved_revision is not None
        and getattr(resolved_revision, "change_type", None) != "initial_parse"
    ):
        memory.record_event(
            session_id=session_id,
            event_type="message_understanding_created",
            message_id=message_id,
            task_id=resolved_task_id,
            revision_id=resolved_revision.id if resolved_revision is not None else None,
            event_payload={
                "intent": understanding.intent,
                "response_mode": resolved_response_mode,
            },
        )
    snapshot = memory.refresh_snapshot(
        session_id,
        task_id=resolved_task_id,
        revision_id=resolved_revision_id,
        understanding_id=record.id,
    )
    record.snapshot_id = snapshot.id
    record.summary_id = snapshot.latest_summary_id
    if resolved_revision_id:
        record.lineage_root_id = resolved_revision_id
    db_session.flush()

    if resolved_revision_id and (
        resolved_revision is None
        or getattr(resolved_revision, "change_type", None) != "initial_parse"
    ):
        memory.link_entities(
            session_id=session_id,
            source_type="understanding",
            source_id=record.id,
            target_type="revision",
            target_id=resolved_revision_id,
            link_type="derived_from",
            weight=1.0,
        )
        revision = resolved_revision
        if revision is not None:
            if getattr(revision, "change_type", None) == "correction":
                revision.parent_message_understanding_id = record.id
            revision.history_features_json = dict(record.history_features_json or {})
            if revision.lineage_root_id is None:
                revision.lineage_root_id = revision.id
            if record.lineage_root_id is None:
                record.lineage_root_id = revision.lineage_root_id or revision.id
            db_session.flush()
    return record


def _expand_intent(
    base_intent: IntentResult,
    *,
    context: ConversationContextBundle,
) -> tuple[IntentKind, dict[str, object], float]:
    signals = context.explicit_signals
    has_active_task = context.latest_active_task_id is not None
    has_active_revision = context.latest_active_revision_id is not None
    overlap = signals.get("revision_field_overlap")
    overlap_fields: list[str] = []
    if isinstance(overlap, dict):
        overlap_fields = [str(field) for field in overlap.get("fields", []) if str(field).strip()]

    confirmation_hint = bool(signals.get("confirmation_hint"))
    correction_hint = bool(signals.get("correction_hint"))
    overlap_present = bool(overlap_fields)

    if correction_hint and has_active_revision:
        return (
            "task_correction",
            {
                "rule": "correction_hint_with_active_revision",
                "signals": signals,
            },
            max(base_intent.confidence, 0.95),
        )

    if confirmation_hint and has_active_task:
        return (
            "task_confirmation",
            {
                "rule": "confirmation_hint_with_active_task",
                "signals": signals,
            },
            max(base_intent.confidence, 0.95),
        )

    if base_intent.intent == "task" and has_active_task and overlap_present:
        return (
            "task_followup",
            {
                "rule": "task_intent_with_active_context_overlap",
                "overlap_fields": overlap_fields,
            },
            base_intent.confidence,
        )

    if base_intent.intent == "task":
        return (
            "new_task",
            {
                "rule": "task_intent_without_active_overlap",
                "signals": signals,
            },
            base_intent.confidence,
        )

    if base_intent.intent == "chat":
        return (
            "chat",
            {"rule": "classifier_chat"},
            base_intent.confidence,
        )

    return (
        "ambiguous",
        {"rule": "classifier_ambiguous"},
        base_intent.confidence,
    )


def _build_field_confidences(
    *,
    intent: IntentKind,
    parsed_spec: ParsedTaskSpec | None,
    context: ConversationContextBundle,
    message: str,
    settings: Any,
) -> dict[str, FieldConfidence]:
    if parsed_spec is None:
        return {}

    fields = {
        "aoi_input": _score_aoi_input(parsed_spec, context, message, settings),
        "aoi_source_type": _score_aoi_source_type(parsed_spec, context, message, settings),
        "time_range": _score_time_range(parsed_spec, context, message, settings),
        "analysis_type": _score_analysis_type(parsed_spec, context, message, settings, intent),
    }
    return {
        field_name: _apply_history_confidence(
            field_name=field_name,
            confidence=confidence,
            context=context,
            settings=settings,
        )
        for field_name, confidence in fields.items()
    }


def _score_aoi_input(
    parsed_spec: ParsedTaskSpec,
    context: ConversationContextBundle,
    message: str,
    settings: Any,
) -> FieldConfidence:
    evidence: list[EvidenceItem] = []
    score = 0.0

    if parsed_spec.aoi_input:
        score = 0.78
        evidence.append(
            EvidenceItem(
                field="aoi_input",
                source="parser",
                weight=0.55,
                detail=f"parser extracted aoi_input={parsed_spec.aoi_input}",
                message_ref=context.message_id,
            )
        )

    signals = context.explicit_signals
    overlap = signals.get("revision_field_overlap")
    if isinstance(overlap, dict) and "aoi_input" in {
        str(item) for item in overlap.get("fields", [])
    }:
        score = max(score, 0.86)
        evidence.append(
            EvidenceItem(
                field="aoi_input",
                source="context_overlap",
                weight=0.32,
                detail="active revision overlaps on aoi_input",
                message_ref=context.message_id,
                upload_file_id=_first_upload_id(context),
            )
        )

    if bool(signals.get("upload_file_hint")):
        score = max(score, 0.9)
        evidence.append(
            EvidenceItem(
                field="aoi_input",
                source="explicit_signal",
                weight=0.25,
                detail="message mentions upload/file/shp/boundary terms",
                message_ref=context.message_id,
                upload_file_id=_first_upload_id(context),
            )
        )

    if bool(signals.get("bbox_hint")):
        score = max(score, 0.92)
        evidence.append(
            EvidenceItem(
                field="aoi_input",
                source="explicit_signal",
                weight=0.25,
                detail="message contains bbox-like text",
                message_ref=context.message_id,
            )
        )

    if bool(signals.get("admin_region_hint")):
        score = max(score, 0.88)
        evidence.append(
            EvidenceItem(
                field="aoi_input",
                source="explicit_signal",
                weight=0.2,
                detail="message contains an administrative-region cue",
                message_ref=context.message_id,
            )
        )

    if not evidence and parsed_spec.aoi_input:
        score = 0.7
        evidence.append(
            EvidenceItem(
                field="aoi_input",
                source="parser",
                weight=0.4,
                detail="parser provided aoi_input without stronger white-box signals",
                message_ref=context.message_id,
            )
        )

    score = _bound_score(score)
    return FieldConfidence(score=score, level=_confidence_level(score, settings), evidence=evidence)


def _score_aoi_source_type(
    parsed_spec: ParsedTaskSpec,
    context: ConversationContextBundle,
    message: str,
    settings: Any,
) -> FieldConfidence:
    evidence: list[EvidenceItem] = []
    score = 0.0

    if parsed_spec.aoi_source_type:
        score = 0.76
        evidence.append(
            EvidenceItem(
                field="aoi_source_type",
                source="parser",
                weight=0.5,
                detail=f"parser extracted aoi_source_type={parsed_spec.aoi_source_type}",
                message_ref=context.message_id,
            )
        )

    signals = context.explicit_signals
    if bool(signals.get("upload_file_hint")) and parsed_spec.aoi_source_type == "file_upload":
        score = max(score, 0.92)
        evidence.append(
            EvidenceItem(
                field="aoi_source_type",
                source="explicit_signal",
                weight=0.3,
                detail="upload hint supports file_upload source type",
                message_ref=context.message_id,
                upload_file_id=_first_upload_id(context),
            )
        )

    if bool(signals.get("bbox_hint")) and parsed_spec.aoi_source_type == "bbox":
        score = max(score, 0.92)
        evidence.append(
            EvidenceItem(
                field="aoi_source_type",
                source="explicit_signal",
                weight=0.3,
                detail="bbox hint supports bbox source type",
                message_ref=context.message_id,
            )
        )

    if bool(signals.get("admin_region_hint")) and parsed_spec.aoi_source_type in {
        "admin_name",
        "place_alias",
    }:
        score = max(score, 0.88)
        evidence.append(
            EvidenceItem(
                field="aoi_source_type",
                source="explicit_signal",
                weight=0.25,
                detail="administrative-region cue supports named AOI source type",
                message_ref=context.message_id,
            )
        )

    if not evidence and parsed_spec.aoi_source_type:
        score = 0.68
        evidence.append(
            EvidenceItem(
                field="aoi_source_type",
                source="parser",
                weight=0.35,
                detail="parser provided aoi_source_type without stronger white-box signals",
                message_ref=context.message_id,
            )
        )

    score = _bound_score(score)
    return FieldConfidence(score=score, level=_confidence_level(score, settings), evidence=evidence)


def _score_time_range(
    parsed_spec: ParsedTaskSpec,
    context: ConversationContextBundle,
    message: str,
    settings: Any,
) -> FieldConfidence:
    evidence: list[EvidenceItem] = []
    score = 0.0

    if parsed_spec.time_range:
        score = 0.8
        evidence.append(
            EvidenceItem(
                field="time_range",
                source="parser",
                weight=0.55,
                detail=(
                    "parser extracted time_range="
                    f"{parsed_spec.time_range.get('start')}..{parsed_spec.time_range.get('end')}"
                ),
                message_ref=context.message_id,
            )
        )

    if parsed_spec.time_range and _looks_like_time_message(message):
        score = max(score, 0.9)
        evidence.append(
            EvidenceItem(
                field="time_range",
                source="message_text",
                weight=0.25,
                detail="current message contains an explicit time expression",
                message_ref=context.message_id,
            )
        )

    overlap = context.explicit_signals.get("revision_field_overlap")
    if isinstance(overlap, dict) and "time_range" in {
        str(item) for item in overlap.get("fields", [])
    }:
        score = max(score, 0.86)
        evidence.append(
            EvidenceItem(
                field="time_range",
                source="context_overlap",
                weight=0.25,
                detail="active revision overlaps on time_range",
                message_ref=context.message_id,
            )
        )

    if not evidence and parsed_spec.time_range:
        score = 0.72
        evidence.append(
            EvidenceItem(
                field="time_range",
                source="parser",
                weight=0.4,
                detail="parser provided time_range without stronger white-box signals",
                message_ref=context.message_id,
            )
        )

    score = _bound_score(score)
    return FieldConfidence(score=score, level=_confidence_level(score, settings), evidence=evidence)


def _score_analysis_type(
    parsed_spec: ParsedTaskSpec,
    context: ConversationContextBundle,
    message: str,
    settings: Any,
    intent: IntentKind,
) -> FieldConfidence:
    evidence: list[EvidenceItem] = []
    score = 0.0

    if parsed_spec.analysis_type:
        score = 0.8 if parsed_spec.analysis_type != "WORKFLOW" else 0.72
        evidence.append(
            EvidenceItem(
                field="analysis_type",
                source="parser",
                weight=0.5,
                detail=f"parser extracted analysis_type={parsed_spec.analysis_type}",
                message_ref=context.message_id,
            )
        )

    if _looks_like_analysis_message(message, parsed_spec.analysis_type):
        score = max(score, 0.9)
        evidence.append(
            EvidenceItem(
                field="analysis_type",
                source="message_text",
                weight=0.25,
                detail="message contains analysis-type cues",
                message_ref=context.message_id,
            )
        )

    if intent in {"task_correction", "task_followup"}:
        score = max(score, 0.82)
        evidence.append(
            EvidenceItem(
                field="analysis_type",
                source="intent",
                weight=0.15,
                detail=f"intent {intent} is task-like and preserves analysis type continuity",
                message_ref=context.message_id,
            )
        )

    if not evidence and parsed_spec.analysis_type:
        score = 0.68
        evidence.append(
            EvidenceItem(
                field="analysis_type",
                source="parser",
                weight=0.35,
                detail="parser provided analysis_type without stronger white-box signals",
                message_ref=context.message_id,
            )
        )

    score = _bound_score(score)
    return FieldConfidence(score=score, level=_confidence_level(score, settings), evidence=evidence)


def _build_ranked_candidates(
    field_confidences: dict[str, FieldConfidence],
    parsed_spec: ParsedTaskSpec | None,
) -> dict[str, list[dict[str, object]]]:
    if parsed_spec is None:
        return {}

    ranked_candidates: dict[str, list[dict[str, object]]] = {}
    for field_name, confidence in field_confidences.items():
        value = getattr(parsed_spec, field_name, None)
        if value is None:
            continue
        ranked_candidates[field_name] = [
            {
                "value": _json_safe(value),
                "score": confidence.score,
                "level": confidence.level,
                "reason": "parser + white-box evidence",
            }
        ]
    return ranked_candidates


def _build_understanding_summary(
    *,
    intent: IntentKind,
    parsed_spec: ParsedTaskSpec | None,
    context: ConversationContextBundle,
    base_intent: IntentResult,
) -> str:
    if intent == "chat":
        return "消息更像聊天或非任务请求。"
    if intent == "ambiguous":
        return "消息意图不够明确，需要更多上下文。"

    parts: list[str] = [f"识别为{intent}"]
    if parsed_spec is not None:
        if parsed_spec.analysis_type:
            parts.append(f"analysis_type={parsed_spec.analysis_type}")
        if parsed_spec.aoi_input:
            parts.append(f"aoi_input={parsed_spec.aoi_input}")
        if parsed_spec.time_range:
            parts.append(
                "time_range="
                f"{parsed_spec.time_range.get('start')}..{parsed_spec.time_range.get('end')}"
            )
    if context.latest_active_revision_summary:
        parts.append(f"active_revision={context.latest_active_revision_summary}")
    parts.append(f"base_intent={base_intent.intent}")
    return "，".join(parts) + "。"


def _parser_context_summary(context: ConversationContextBundle) -> str:
    parts: list[str] = []
    if context.latest_active_revision_summary:
        parts.append(f"active_revision={context.latest_active_revision_summary}")
    if context.uploaded_files:
        upload_names = ",".join(
            str(item.get("original_name") or "")
            for item in context.uploaded_files[:3]
            if item.get("original_name")
        )
        if upload_names:
            parts.append(f"uploads={upload_names}")
    for field_name, entries in sorted(context.field_value_history.items()):
        if entries:
            parts.append(f"{field_name}_history={len(entries)}")
    return "；".join(parts)


def _build_history(context: ConversationContextBundle) -> list[dict[str, str]]:
    history_with_order: list[tuple[datetime | None, int, dict[str, str]]] = []
    for item in context.relevant_messages:
        role = str(item.get("role") or "user")
        content = str(item.get("content") or "")
        if not content:
            continue
        history_with_order.append(
            (
                _coerce_message_datetime(item.get("created_at")),
                len(history_with_order),
                {"role": role, "content": content},
            )
        )
    history_with_order.sort(key=lambda item: (item[0] is None, item[0] or datetime.min, item[1]))
    return [item[2] for item in history_with_order]


def _confidence_level(score: float, settings: Any) -> Literal["low", "medium", "high"]:
    if score >= float(settings.understanding_field_high_threshold):
        return "high"
    if score >= float(settings.understanding_field_medium_threshold):
        return "medium"
    return "low"


def _extract_context_value(trace: dict[str, object], path: tuple[str, str]) -> str | None:
    current: object = trace
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, str) and current:
        return current
    return None


def _extract_context_object(trace: dict[str, object], path: tuple[str, str]) -> dict[str, object]:
    current: object = trace
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    if isinstance(current, dict):
        return dict(current)
    return {}


def _apply_history_confidence(
    *,
    field_name: str,
    confidence: FieldConfidence,
    context: ConversationContextBundle,
    settings: Any,
) -> FieldConfidence:
    history_features = {
        name: features.dump()
        for name, features in build_history_features(context.field_value_history).items()
    }
    for name, payload in dict(context.history_features or {}).items():
        if isinstance(payload, dict):
            history_features[name] = dict(payload)

    parser_score = max(
        (item.weight for item in confidence.evidence if item.source == "parser"),
        default=confidence.score * 0.5,
    )
    current_signal_score = max(
        (item.weight for item in confidence.evidence if item.source != "parser"),
        default=confidence.score,
    )
    current_signal_score = max(current_signal_score, confidence.score)
    history_confidence = compute_field_confidence(
        field_name=field_name,
        parser_score=parser_score,
        history_features=history_features,
        current_signal_score=current_signal_score,
        high_threshold=float(settings.understanding_field_high_threshold),
        medium_threshold=float(settings.understanding_field_medium_threshold),
    )
    final_score = max(confidence.score, history_confidence.score)
    history_evidence = [
        EvidenceItem(
            field=field_name,
            source=item.source,
            weight=item.weight,
            detail=item.detail,
            message_ref=context.message_id,
        )
        for item in history_confidence.evidence
        if item.source == "revision_history"
    ]
    return FieldConfidence(
        score=final_score,
        level=_confidence_level(final_score, settings),
        evidence=[*confidence.evidence, *history_evidence],
    )


def _coerce_message_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _format_field_evidence(item: EvidenceItem) -> str:
    parts = [item.source, item.detail]
    if item.message_ref:
        parts.append(f"message={item.message_ref}")
    if item.upload_file_id:
        parts.append(f"upload_file_id={item.upload_file_id}")
    return " | ".join(parts)


def _first_upload_id(context: ConversationContextBundle) -> str | None:
    if not context.uploaded_files:
        return None
    first = context.uploaded_files[0]
    if not isinstance(first, dict):
        return None
    upload_id = first.get("id")
    if isinstance(upload_id, str) and upload_id:
        return upload_id
    return None


def _has_upload_context(context: ConversationContextBundle) -> bool:
    if context.uploaded_files:
        return True
    return bool(context.explicit_signals.get("upload_file_hint"))


def _looks_like_time_message(message: str) -> bool:
    return any(
        token in message for token in ("年", "月", "日", "季度", "春季", "夏季", "秋季", "冬季")
    )


def _looks_like_analysis_message(message: str, analysis_type: str | None) -> bool:
    text = message.lower()
    if analysis_type == "NDVI":
        return "ndvi" in text or "植被" in message
    if analysis_type == "NDWI":
        return "ndwi" in text or "水体" in message
    if analysis_type == "CLIP":
        return any(token in message for token in ("裁剪", "裁切", "clip", "mask"))
    return any(token in message for token in ("分析", "计算", "统计", "处理"))


def _bound_score(score: float) -> float:
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _excerpt(message: str, limit: int = 120) -> str:
    message = message.strip()
    if len(message) <= limit:
        return message
    return f"{message[: limit - 1]}…"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
