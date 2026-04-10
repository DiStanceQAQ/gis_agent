from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from packages.domain.models import (
    MessageRecord,
    TaskRunRecord,
    UploadedFileRecord,
)
from packages.domain.services.aoi import normalize_active_revision_aoi
from packages.domain.services.response_policy import ResponseDecision
from packages.domain.services.task_revisions import (
    activate_revision,
    create_correction_revision,
)
from packages.domain.services.task_state import (
    TASK_STATUS_AWAITING_APPROVAL,
    set_task_status,
)
from packages.domain.services.understanding import MessageUnderstanding
from packages.domain.utils import merge_dicts
from packages.schemas.task import ParsedTaskSpec


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
            force=True,  # user-driven correction: can arrive from any task status
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
