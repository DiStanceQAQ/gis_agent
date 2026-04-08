from __future__ import annotations

from dataclasses import dataclass

from packages.domain.services.understanding import (
    EvidenceItem,
    FieldConfidence,
    MessageUnderstanding,
)
from packages.domain.services.response_policy import decide_response
from packages.schemas.task import ParsedTaskSpec


KEY_FIELDS = ("aoi_input", "aoi_source_type", "time_range", "analysis_type")


@dataclass(slots=True)
class ActiveRevisionStub:
    execution_blocked: bool = False
    execution_blocked_reason: str | None = None
    understanding_summary: str | None = None
    raw_spec_json: dict[str, object] | None = None
    response_mode: str | None = None
    revision_number: int = 1
    change_type: str = "initial_parse"


def _confidence(score: float) -> FieldConfidence:
    if score < 0.60:
        level = "low"
    elif score < 0.80:
        level = "medium"
    else:
        level = "high"
    return FieldConfidence(
        score=score,
        level=level,
        evidence=[
            EvidenceItem(
                field="analysis",
                source="current_message",
                weight=score,
                detail="stub evidence",
            )
        ],
    )


def _understanding(
    *,
    intent: str,
    field_scores: dict[str, float],
    parsed_spec: ParsedTaskSpec | None = None,
    understanding_summary: str = "理解任务。",
) -> MessageUnderstanding:
    field_confidences = {
        name: _confidence(field_scores.get(name, 0.0)) for name in KEY_FIELDS
    }
    return MessageUnderstanding(
        intent=intent,  # type: ignore[arg-type]
        intent_confidence=0.91,
        understanding_summary=understanding_summary,
        parsed_spec=parsed_spec,
        parsed_candidates=parsed_spec.model_dump() if parsed_spec is not None else {},
        field_confidences=field_confidences,
        ranked_candidates={},
        trace={"source": "test"},
    )


def test_chat_maps_to_chat_reply() -> None:
    understanding = _understanding(intent="chat", field_scores={})

    decision = decide_response(
        understanding,
        active_revision=None,
        require_approval=False,
    )

    assert decision.mode == "chat_reply"
    assert decision.execution_blocked is False
    assert decision.requires_execution is False
    assert decision.editable_fields == []
    assert decision.response_payload["execution_blocked"] is False


def test_low_confidence_key_field_requests_missing_fields() -> None:
    understanding = _understanding(
        intent="new_task",
        field_scores={
            "aoi_input": 0.42,
            "aoi_source_type": 0.88,
            "time_range": 0.86,
            "analysis_type": 0.92,
        },
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="WORKFLOW",
        ),
    )

    decision = decide_response(
        understanding,
        active_revision=None,
        require_approval=False,
    )

    assert decision.mode == "ask_missing_fields"
    assert decision.execution_blocked is False
    assert decision.requires_execution is False
    assert decision.response_payload["missing_fields"] == ["aoi_input"]
    assert decision.editable_fields == ["aoi_input"]


def test_task_correction_with_structure_shows_revision() -> None:
    understanding = _understanding(
        intent="task_correction",
        field_scores={
            "aoi_input": 0.86,
            "aoi_source_type": 0.88,
            "time_range": 0.84,
            "analysis_type": 0.87,
        },
        parsed_spec=ParsedTaskSpec(
            aoi_input="上海",
            aoi_source_type="admin_name",
            time_range={"start": "2024-07-01", "end": "2024-07-31"},
            analysis_type="WORKFLOW",
        ),
    )
    active_revision = ActiveRevisionStub(
        execution_blocked=False,
        understanding_summary="使用江西作为 AOI。",
        raw_spec_json={
            "aoi_input": "江西",
            "aoi_source_type": "admin_name",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "WORKFLOW",
        },
        response_mode="confirm_understanding",
        revision_number=1,
        change_type="initial_parse",
    )

    decision = decide_response(
        understanding,
        active_revision=active_revision,
        require_approval=False,
    )

    assert decision.mode == "show_revision"
    assert decision.execution_blocked is False
    assert decision.requires_revision_creation is True
    assert decision.requires_execution is False
    assert decision.editable_fields == list(KEY_FIELDS)
    assert decision.response_payload["revision_number"] == 1


def test_medium_confidence_task_confirms_understanding() -> None:
    understanding = _understanding(
        intent="new_task",
        field_scores={
            "aoi_input": 0.82,
            "aoi_source_type": 0.76,
            "time_range": 0.85,
            "analysis_type": 0.83,
        },
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="WORKFLOW",
        ),
    )

    decision = decide_response(
        understanding,
        active_revision=None,
        require_approval=False,
    )

    assert decision.mode == "confirm_understanding"
    assert decision.execution_blocked is False
    assert decision.requires_execution is False
    assert decision.editable_fields == ["aoi_source_type"]


def test_all_high_with_no_approval_executes_now() -> None:
    understanding = _understanding(
        intent="new_task",
        field_scores={
            "aoi_input": 0.92,
            "aoi_source_type": 0.91,
            "time_range": 0.88,
            "analysis_type": 0.95,
        },
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="WORKFLOW",
        ),
    )
    active_revision = ActiveRevisionStub(
        execution_blocked=False,
        understanding_summary="理解完整。",
        raw_spec_json={
            "aoi_input": "江西",
            "aoi_source_type": "admin_name",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "WORKFLOW",
        },
    )

    decision = decide_response(
        understanding,
        active_revision=active_revision,
        require_approval=False,
    )

    assert decision.mode == "execute_now"
    assert decision.execution_blocked is False
    assert decision.requires_execution is True
    assert decision.requires_task_creation is False
    assert decision.response_payload["execution_blocked"] is False


def test_all_high_with_approval_required_still_executes_now() -> None:
    understanding = _understanding(
        intent="new_task",
        field_scores={
            "aoi_input": 0.92,
            "aoi_source_type": 0.91,
            "time_range": 0.88,
            "analysis_type": 0.95,
        },
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="WORKFLOW",
        ),
    )

    decision = decide_response(
        understanding,
        active_revision=None,
        require_approval=True,
    )

    assert decision.mode == "execute_now"
    assert decision.requires_execution is True
    assert decision.response_payload["require_approval"] is True


def test_blocked_active_revision_forces_non_executable_response() -> None:
    understanding = _understanding(
        intent="new_task",
        field_scores={
            "aoi_input": 0.92,
            "aoi_source_type": 0.91,
            "time_range": 0.88,
            "analysis_type": 0.95,
        },
        parsed_spec=ParsedTaskSpec(
            aoi_input="江西",
            aoi_source_type="admin_name",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="WORKFLOW",
        ),
    )
    active_revision = ActiveRevisionStub(
        execution_blocked=True,
        execution_blocked_reason="AOI normalization failed: uploaded boundary is invalid",
        understanding_summary="理解完整但被门控阻止。",
        raw_spec_json={
            "aoi_input": "uploaded_aoi",
            "aoi_source_type": "file_upload",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "WORKFLOW",
        },
        response_mode="confirm_understanding",
    )

    decision = decide_response(
        understanding,
        active_revision=active_revision,
        require_approval=False,
    )

    assert decision.mode == "ask_missing_fields"
    assert decision.execution_blocked is True
    assert decision.requires_execution is False
    assert decision.response_payload["execution_blocked"] is True
    assert decision.response_payload["blocked_reason"] == "AOI normalization failed: uploaded boundary is invalid"
    assert decision.editable_fields == list(KEY_FIELDS)
