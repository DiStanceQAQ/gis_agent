from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.schemas.agent import LLMParsedSpec, LLMRecommendation, LLMTaskPlan
from packages.schemas.task import ParsedTaskSpec


def test_llm_parsed_spec_with_operation_params() -> None:
    payload = LLMParsedSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        analysis_type="NDWI",
        operation_params={"index": "ndwi", "window_size": 3},
    )

    assert payload.analysis_type == "NDWI"
    assert payload.operation_params["index"] == "ndwi"


def test_parsed_task_spec_normalizes_analysis_type_and_operation_params() -> None:
    payload = ParsedTaskSpec(
        analysis_type="ndwi",
        operation_params={},
    )

    assert payload.analysis_type == "NDWI"
    assert payload.operation_params["index"] == "ndwi"


def test_llm_parsed_spec_band_math_requires_expression() -> None:
    with pytest.raises(ValidationError):
        LLMParsedSpec(
            analysis_type="band_math",
            operation_params={},
        )


def test_llm_parsed_spec_buffer_requires_positive_distance() -> None:
    with pytest.raises(ValidationError):
        LLMParsedSpec(
            analysis_type="buffer",
            operation_params={"distance_m": -20},
        )


def test_llm_task_plan_requires_unique_step_names() -> None:
    with pytest.raises(ValidationError):
        LLMTaskPlan(
            objective="test",
            reasoning_summary="test",
            steps=[
                {
                    "step_name": "plan_task",
                    "tool_name": "planner.build",
                    "title": "规划任务",
                    "purpose": "生成计划",
                },
                {
                    "step_name": "plan_task",
                    "tool_name": "planner.build",
                    "title": "重复步骤",
                    "purpose": "触发校验",
                },
            ],
        )


def test_llm_recommendation_confidence_range() -> None:
    payload = LLMRecommendation(
        primary_dataset="sentinel2",
        backup_dataset="landsat89",
        reason="sentinel2 覆盖与分辨率更优",
        confidence=0.86,
    )

    assert payload.confidence == 0.86
