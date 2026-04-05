from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.schemas.agent import LLMParsedSpec, LLMReactStepDecision, LLMRecommendation, LLMTaskPlan
from packages.schemas.task import ParsedTaskSpec, TaskPlanApproveRequest


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


def test_parsed_task_spec_supports_clip_analysis_type() -> None:
    payload = ParsedTaskSpec(
        analysis_type="clip",
        operation_params={
            "source_path": "/Users/ljn/gis_data/CACD-2020.tif",
            "clip_path": "/Users/ljn/gis_data/区划/xinjiang.geojson",
            "output_path": "/Users/ljn/gis_data/xinjiang.tif",
        },
    )

    assert payload.analysis_type == "CLIP"
    assert payload.operation_params["source_path"].endswith("CACD-2020.tif")
    assert payload.operation_params["clip_path"].endswith("xinjiang.geojson")


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


def test_llm_react_step_decision_continue_requires_function_name() -> None:
    with pytest.raises(ValidationError):
        LLMReactStepDecision(
            decision="continue",
            function_name=None,
            arguments={},
            reasoning_summary="需要继续执行",
        )


def test_llm_react_step_decision_continue_accepts_function_call() -> None:
    payload = LLMReactStepDecision(
        decision="continue",
        function_name="processing.run",
        arguments={"step_name": "run_processing_pipeline"},
        reasoning_summary="依赖满足，继续执行处理步骤。",
    )
    assert payload.decision == "continue"
    assert payload.function_name == "processing.run"


def test_llm_react_step_decision_skip_or_fail_does_not_require_function_name() -> None:
    skip_payload = LLMReactStepDecision(
        decision="skip",
        reasoning_summary="该步骤可以跳过。",
    )
    fail_payload = LLMReactStepDecision(
        decision="fail",
        reasoning_summary="前置条件未满足，终止。",
    )
    assert skip_payload.function_name is None
    assert fail_payload.function_name is None


@pytest.mark.parametrize("approved_version", [0, -1])
def test_task_plan_approve_request_requires_positive_version(approved_version: int) -> None:
    with pytest.raises(ValidationError):
        TaskPlanApproveRequest(approved_version=approved_version)
