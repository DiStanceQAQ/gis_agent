from __future__ import annotations

from packages.domain.services.session_memory_policy import decide_memory_aware_response
from packages.domain.services.response_policy import ResponseDecision
from packages.domain.services.understanding import (
    EvidenceItem,
    FieldConfidence,
    MessageUnderstanding,
)
from packages.schemas.task import ParsedTaskSpec


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
                field="field",
                source="test",
                weight=score,
                detail="stub",
            )
        ],
    )


def _understanding(
    *,
    parsed_spec: ParsedTaskSpec,
    field_scores: dict[str, float],
) -> MessageUnderstanding:
    return MessageUnderstanding(
        intent="new_task",
        intent_confidence=0.94,
        understanding_summary="理解任务。",
        parsed_spec=parsed_spec,
        parsed_candidates=parsed_spec.model_dump(),
        field_confidences={
            "aoi_input": _confidence(field_scores.get("aoi_input", 0.0)),
            "aoi_source_type": _confidence(field_scores.get("aoi_source_type", 0.0)),
            "time_range": _confidence(field_scores.get("time_range", 0.0)),
            "analysis_type": _confidence(field_scores.get("analysis_type", 0.0)),
        },
        ranked_candidates={},
        trace={"source": "test"},
    )


def test_risk_aware_policy_requires_confirmation_for_high_risk_even_when_fields_are_high() -> None:
    decision = decide_memory_aware_response(
        understanding=_understanding(
            parsed_spec=ParsedTaskSpec(
                aoi_input="江西",
                aoi_source_type="admin_name",
                time_range={"start": "2024-06-01", "end": "2024-06-30"},
                analysis_type="NDVI",
            ),
            field_scores={
                "aoi_input": 0.95,
                "aoi_source_type": 0.94,
                "time_range": 0.93,
                "analysis_type": 0.96,
            },
        ),
        active_revision=None,
        user_preference_profile={"confirmation_preference": "always_confirm"},
        risk_profile={"task_risk": "high"},
        require_approval=False,
    )

    assert isinstance(decision, ResponseDecision)
    assert decision.mode == "confirm_understanding"


def test_low_risk_clip_with_explicit_upload_context_can_execute_without_time_range() -> None:
    decision = decide_memory_aware_response(
        understanding=_understanding(
            parsed_spec=ParsedTaskSpec(
                aoi_input="uploaded_aoi",
                aoi_source_type="file_upload",
                analysis_type="CLIP",
                operation_params={
                    "source_path": "/tmp/source.tif",
                    "clip_path": "/tmp/clip.geojson",
                },
            ),
            field_scores={
                "aoi_input": 0.93,
                "aoi_source_type": 0.92,
                "time_range": 0.0,
                "analysis_type": 0.95,
            },
        ),
        active_revision=None,
        user_preference_profile={"confirmation_preference": "balanced"},
        risk_profile={"task_risk": "low"},
        require_approval=False,
    )

    assert decision.mode in {"execute_now", "confirm_understanding"}
    assert "time_range" not in decision.response_payload["missing_fields"]
