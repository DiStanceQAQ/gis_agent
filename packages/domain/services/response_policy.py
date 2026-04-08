from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.domain.config import get_settings
from packages.domain.services.understanding import MessageUnderstanding, ResponseMode


KEY_FIELDS: tuple[str, ...] = ("aoi_input", "aoi_source_type", "time_range", "analysis_type")


class ActiveRevisionLike(Protocol):
    execution_blocked: bool
    execution_blocked_reason: str | None
    understanding_summary: str | None
    raw_spec_json: dict[str, object] | None
    response_mode: str | None
    revision_number: int
    change_type: str


@dataclass(slots=True)
class ResponseDecision:
    mode: ResponseMode
    assistant_message: str
    editable_fields: list[str]
    response_payload: dict[str, object]
    execution_blocked: bool
    requires_task_creation: bool
    requires_revision_creation: bool
    requires_execution: bool


def decide_response(
    understanding: MessageUnderstanding,
    *,
    active_revision: ActiveRevisionLike | None,
    require_approval: bool,
) -> ResponseDecision:
    settings = get_settings()
    key_field_levels = _key_field_levels(understanding, settings)
    low_fields = [field for field in KEY_FIELDS if key_field_levels[field] == "low"]
    medium_fields = [field for field in KEY_FIELDS if key_field_levels[field] == "medium"]
    all_high = all(key_field_levels[field] == "high" for field in KEY_FIELDS)

    if active_revision is not None and active_revision.execution_blocked:
        editable_fields = _editable_fields_from_revision(active_revision)
        return _build_decision(
            mode="ask_missing_fields",
            understanding=understanding,
            editable_fields=editable_fields,
            execution_blocked=True,
            blocked_reason=active_revision.execution_blocked_reason,
            missing_fields=editable_fields,
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=False,
            requires_revision_creation=False,
            requires_execution=False,
            assistant_message=_blocked_assistant_message(active_revision.execution_blocked_reason),
        )

    if understanding.intent == "chat":
        return _build_decision(
            mode="chat_reply",
            understanding=understanding,
            editable_fields=[],
            execution_blocked=False,
            blocked_reason=None,
            missing_fields=[],
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=False,
            requires_revision_creation=False,
            requires_execution=False,
            assistant_message="当然，我们可以继续聊这个。",
        )

    if low_fields:
        return _build_decision(
            mode="ask_missing_fields",
            understanding=understanding,
            editable_fields=low_fields,
            execution_blocked=False,
            blocked_reason=None,
            missing_fields=low_fields,
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=_requires_task_creation(understanding, active_revision),
            requires_revision_creation=False,
            requires_execution=False,
            assistant_message=_missing_fields_assistant_message(low_fields),
        )

    if understanding.intent == "task_correction" and _has_enough_correction_structure(
        understanding,
        active_revision,
    ):
        editable_fields = _editable_fields_for_revision(understanding, active_revision)
        return _build_decision(
            mode="show_revision",
            understanding=understanding,
            editable_fields=editable_fields,
            execution_blocked=False,
            blocked_reason=None,
            missing_fields=[],
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=_requires_task_creation(understanding, active_revision),
            requires_revision_creation=True,
            requires_execution=False,
            assistant_message=_show_revision_assistant_message(understanding),
        )

    if medium_fields:
        return _build_decision(
            mode="confirm_understanding",
            understanding=understanding,
            editable_fields=medium_fields,
            execution_blocked=False,
            blocked_reason=None,
            missing_fields=[],
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=_requires_task_creation(understanding, active_revision),
            requires_revision_creation=False,
            requires_execution=False,
            assistant_message=_confirm_understanding_assistant_message(understanding),
        )

    if all_high:
        return _build_decision(
            mode="execute_now",
            understanding=understanding,
            editable_fields=[],
            execution_blocked=False,
            blocked_reason=None,
            missing_fields=[],
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=_requires_task_creation(understanding, active_revision),
            requires_revision_creation=False,
            requires_execution=True,
            assistant_message="关键信息齐了，我会直接开始执行。",
        )

    return _build_decision(
        mode="confirm_understanding",
        understanding=understanding,
        editable_fields=[field for field in KEY_FIELDS if key_field_levels[field] != "low"],
        execution_blocked=False,
        blocked_reason=None,
        missing_fields=[],
        active_revision=active_revision,
        require_approval=require_approval,
        requires_task_creation=_requires_task_creation(understanding, active_revision),
        requires_revision_creation=False,
        requires_execution=False,
        assistant_message=_confirm_understanding_assistant_message(understanding),
    )


def _build_decision(
    *,
    mode: ResponseMode,
    understanding: MessageUnderstanding,
    editable_fields: list[str],
    execution_blocked: bool,
    blocked_reason: str | None,
    missing_fields: list[str],
    active_revision: ActiveRevisionLike | None,
    require_approval: bool,
    requires_task_creation: bool,
    requires_revision_creation: bool,
    requires_execution: bool,
    assistant_message: str,
) -> ResponseDecision:
    response_payload = {
        "response_mode": mode,
        "intent": understanding.intent,
        "intent_confidence": understanding.intent_confidence,
        "understanding_summary": understanding.understanding_summary,
        "editable_fields": editable_fields,
        "missing_fields": missing_fields,
        "require_approval": require_approval,
        "field_confidences": understanding.field_confidences_dump(),
        "field_evidence": understanding.field_evidence_dump(),
        "ranked_candidates": understanding.ranked_candidates,
        "execution_blocked": execution_blocked,
        "blocked_reason": blocked_reason,
        "requires_task_creation": requires_task_creation,
        "requires_revision_creation": requires_revision_creation,
        "requires_execution": requires_execution,
    }
    if active_revision is not None:
        response_payload["revision_number"] = active_revision.revision_number
        response_payload["revision_change_type"] = active_revision.change_type
        response_payload["active_revision_summary"] = active_revision.understanding_summary
        response_payload["active_response_mode"] = active_revision.response_mode

    return ResponseDecision(
        mode=mode,
        assistant_message=assistant_message,
        editable_fields=editable_fields,
        response_payload=response_payload,
        execution_blocked=execution_blocked,
        requires_task_creation=requires_task_creation,
        requires_revision_creation=requires_revision_creation,
        requires_execution=requires_execution,
    )


def _key_field_levels(
    understanding: MessageUnderstanding,
    settings: object,
) -> dict[str, str]:
    medium_threshold = float(getattr(settings, "understanding_field_medium_threshold"))
    high_threshold = float(getattr(settings, "understanding_field_high_threshold"))

    levels: dict[str, str] = {}
    for field in KEY_FIELDS:
        confidence = understanding.field_confidences.get(field)
        score = confidence.score if confidence is not None else 0.0
        if score >= high_threshold:
            levels[field] = "high"
        elif score >= medium_threshold:
            levels[field] = "medium"
        else:
            levels[field] = "low"
    return levels


def _editable_fields_from_revision(active_revision: ActiveRevisionLike) -> list[str]:
    if not isinstance(active_revision.raw_spec_json, dict):
        return list(KEY_FIELDS)
    editable_fields = [field for field in KEY_FIELDS if field in active_revision.raw_spec_json]
    return editable_fields or list(KEY_FIELDS)


def _editable_fields_for_revision(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
) -> list[str]:
    if active_revision is not None:
        return _editable_fields_from_revision(active_revision)
    if understanding.parsed_spec is None:
        return list(KEY_FIELDS)
    parsed = understanding.parsed_spec.model_dump()
    editable_fields = [field for field in KEY_FIELDS if parsed.get(field) is not None]
    return editable_fields or list(KEY_FIELDS)


def _has_enough_correction_structure(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
) -> bool:
    if active_revision is None:
        return False
    if understanding.parsed_spec is not None:
        return True
    if not isinstance(active_revision.raw_spec_json, dict):
        return False
    return any(active_revision.raw_spec_json.get(field) is not None for field in KEY_FIELDS)


def _requires_task_creation(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
) -> bool:
    return active_revision is None and understanding.intent != "chat"


def _blocked_assistant_message(blocked_reason: str | None) -> str:
    if blocked_reason:
        return f"当前版本已被阻止执行：{blocked_reason}。请先修正可编辑字段。"
    return "当前版本已被阻止执行。请先修正可编辑字段。"


def _missing_fields_assistant_message(missing_fields: list[str]) -> str:
    joined = "、".join(missing_fields)
    return f"我还缺少这些关键信息：{joined}。你可以直接补充。"


def _show_revision_assistant_message(understanding: MessageUnderstanding) -> str:
    return f"这是我当前的理解：{understanding.understanding_summary}。你可以直接修改。"


def _confirm_understanding_assistant_message(understanding: MessageUnderstanding) -> str:
    return f"我理解的是：{understanding.understanding_summary}。如果没问题，我就继续。"
