from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.domain.config import get_settings
from packages.domain.services.understanding import MessageUnderstanding, ResponseMode
from packages.schemas.analysis import analysis_type_requires_time_range, normalize_analysis_type


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
    user_preference_profile: dict[str, object] | None = None,
    risk_profile: dict[str, object] | None = None,
) -> ResponseDecision:
    settings = get_settings()
    key_fields = _required_fields(understanding, active_revision)
    key_field_levels = _key_field_levels(understanding, settings, key_fields=key_fields)
    parser_missing_fields = _parser_missing_fields(understanding, key_fields)
    low_fields = list(
        dict.fromkeys(
            [field for field in key_fields if key_field_levels[field] == "low"] + parser_missing_fields
        )
    )
    medium_fields = [field for field in key_fields if key_field_levels[field] == "medium"]
    all_high = all(key_field_levels[field] == "high" for field in key_fields)
    confirmation_preference = _confirmation_preference(understanding, user_preference_profile)
    task_risk = _task_risk(understanding, active_revision, risk_profile)

    if active_revision is not None and active_revision.execution_blocked:
        editable_fields = _editable_fields_from_revision(active_revision, key_fields=key_fields)
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
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
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
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
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
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
        )

    if understanding.intent == "task_correction" and _has_enough_correction_structure(
        understanding,
        active_revision,
    ):
        editable_fields = _editable_fields_for_revision(
            understanding,
            active_revision,
            key_fields=key_fields,
        )
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
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
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
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
        )

    if all_high and _should_force_confirmation(
        confirmation_preference=confirmation_preference,
        task_risk=task_risk,
    ):
        return _build_decision(
            mode="confirm_understanding",
            understanding=understanding,
            editable_fields=list(key_fields),
            execution_blocked=False,
            blocked_reason=None,
            missing_fields=[],
            active_revision=active_revision,
            require_approval=require_approval,
            requires_task_creation=_requires_task_creation(understanding, active_revision),
            requires_revision_creation=False,
            requires_execution=False,
            assistant_message=_confirm_understanding_assistant_message(understanding),
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
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
            task_risk=task_risk,
            confirmation_preference=confirmation_preference,
            required_fields=key_fields,
        )

    return _build_decision(
        mode="confirm_understanding",
        understanding=understanding,
        editable_fields=[field for field in key_fields if key_field_levels[field] != "low"],
        execution_blocked=False,
        blocked_reason=None,
        missing_fields=[],
        active_revision=active_revision,
        require_approval=require_approval,
        requires_task_creation=_requires_task_creation(understanding, active_revision),
        requires_revision_creation=False,
        requires_execution=False,
        assistant_message=_confirm_understanding_assistant_message(understanding),
        task_risk=task_risk,
        confirmation_preference=confirmation_preference,
        required_fields=key_fields,
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
    task_risk: str,
    confirmation_preference: str,
    required_fields: list[str],
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
        "task_risk": task_risk,
        "confirmation_preference": confirmation_preference,
        "required_fields": required_fields,
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
    *,
    key_fields: list[str] | tuple[str, ...] = KEY_FIELDS,
) -> dict[str, str]:
    medium_threshold = float(getattr(settings, "understanding_field_medium_threshold"))
    high_threshold = float(getattr(settings, "understanding_field_high_threshold"))

    levels: dict[str, str] = {}
    for field in key_fields:
        confidence = understanding.field_confidences.get(field)
        score = confidence.score if confidence is not None else 0.0
        if score >= high_threshold:
            levels[field] = "high"
        elif score >= medium_threshold:
            levels[field] = "medium"
        else:
            levels[field] = "low"
    return levels


def _editable_fields_from_revision(
    active_revision: ActiveRevisionLike,
    *,
    key_fields: list[str] | tuple[str, ...] = KEY_FIELDS,
) -> list[str]:
    if not isinstance(active_revision.raw_spec_json, dict):
        return list(key_fields)
    editable_fields = [field for field in key_fields if field in active_revision.raw_spec_json]
    return editable_fields or list(key_fields)


def _editable_fields_for_revision(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
    *,
    key_fields: list[str] | tuple[str, ...] = KEY_FIELDS,
) -> list[str]:
    if active_revision is not None:
        return _editable_fields_from_revision(active_revision, key_fields=key_fields)
    if understanding.parsed_spec is None:
        return list(key_fields)
    parsed = understanding.parsed_spec.model_dump()
    editable_fields = [field for field in key_fields if parsed.get(field) is not None]
    return editable_fields or list(key_fields)


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


def _required_fields(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
) -> list[str]:
    analysis_type = normalize_analysis_type(_analysis_type(understanding, active_revision))
    required_fields = ["aoi_input", "aoi_source_type", "analysis_type"]
    if analysis_type_requires_time_range(analysis_type):
        return ["aoi_input", "aoi_source_type", "time_range", "analysis_type"]
    return required_fields


def _analysis_type(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
) -> str:
    if understanding.parsed_spec is not None:
        return str(understanding.parsed_spec.analysis_type or "WORKFLOW")
    if isinstance(active_revision, object) and isinstance(getattr(active_revision, "raw_spec_json", None), dict):
        raw_spec = getattr(active_revision, "raw_spec_json", {}) or {}
        raw_analysis_type = raw_spec.get("analysis_type")
        if isinstance(raw_analysis_type, str) and raw_analysis_type.strip():
            return raw_analysis_type.strip().upper()
    return "WORKFLOW"


def _parser_missing_fields(
    understanding: MessageUnderstanding,
    key_fields: list[str],
) -> list[str]:
    parsed_spec = understanding.parsed_spec
    if parsed_spec is None:
        return []
    return [field for field in parsed_spec.missing_fields if field in key_fields]


def _confirmation_preference(
    understanding: MessageUnderstanding,
    user_preference_profile: dict[str, object] | None,
) -> str:
    profile = dict(user_preference_profile or {})
    explicit = profile.get("confirmation_preference")
    if isinstance(explicit, str):
        token = explicit.strip().lower()
        if token in {"always_confirm", "balanced", "prefer_execute"}:
            return token

    user_priority = profile.get("user_priority")
    if not isinstance(user_priority, str) and understanding.parsed_spec is not None:
        user_priority = understanding.parsed_spec.user_priority
    if isinstance(user_priority, str):
        token = user_priority.strip().lower()
        if token == "speed":
            return "prefer_execute"
        if token in {"quality", "careful", "safe"}:
            return "always_confirm"
    return "balanced"


def _task_risk(
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
    risk_profile: dict[str, object] | None,
) -> str:
    profile = dict(risk_profile or {})
    for key in ("task_risk", "risk_level"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    if bool(profile.get("execution_blocked")):
        return "high"
    if active_revision is not None and active_revision.execution_blocked:
        return "high"
    analysis_type = _analysis_type(understanding, active_revision)
    if analysis_type in {"WORKFLOW", "BAND_MATH"}:
        return "medium"
    return "low"


def _should_force_confirmation(
    *,
    confirmation_preference: str,
    task_risk: str,
) -> bool:
    if confirmation_preference == "always_confirm":
        return True
    return task_risk in {"high", "critical"}
