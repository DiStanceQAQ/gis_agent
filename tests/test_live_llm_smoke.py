from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

from packages.domain.config import get_settings
from packages.domain.services.catalog import CatalogCandidate
from packages.domain.services.operation_plan_builder import build_operation_plan_from_registry
from packages.domain.services.plan_guard import validate_operation_plan
from packages.domain.services.processing_pipeline import run_processing_pipeline
from packages.schemas.operation_plan import OperationPlan
from packages.schemas.task import ParsedTaskSpec
from packages.domain.services.parser import parse_task_message
from packages.domain.services.planner import build_task_plan
from packages.domain.services.recommendation import build_recommendation


def _live_llm_enabled() -> bool:
    return os.getenv("GIS_AGENT_TEST_USE_REAL_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _live_full_pipeline_mode() -> bool:
    return os.getenv("GIS_AGENT_TEST_LIVE_PIPELINE_FULL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


if not _live_llm_enabled():
    pytest.skip(
        "Live LLM smoke tests are disabled. Set GIS_AGENT_TEST_USE_REAL_LLM=true to enable.",
        allow_module_level=True,
    )

if not (
    os.getenv("GIS_AGENT_LLM_API_KEY", "").strip() or str(get_settings().llm_api_key or "").strip()
):
    pytest.skip(
        "GIS_AGENT_LLM_API_KEY is required for live LLM smoke tests (env var or .env).",
        allow_module_level=True,
    )


GIS_DATA_ROOT = Path("/Users/ljn/gis_data")
LOCAL_RASTER = GIS_DATA_ROOT / "CACD-2020.tif"
LOCAL_RASTER_SECOND = GIS_DATA_ROOT / "xinjiang.tif"
LOCAL_CLIP_VECTOR = GIS_DATA_ROOT / "区划" / "xinjiang_small_clip_for_agent_test.geojson"
LOCAL_VECTOR = GIS_DATA_ROOT / "区划" / "市.shp"


@pytest.fixture(autouse=True)
def _require_local_gis_data() -> None:
    required_paths = [LOCAL_RASTER, LOCAL_RASTER_SECOND, LOCAL_CLIP_VECTOR, LOCAL_VECTOR]
    missing_paths = [str(path) for path in required_paths if not path.exists()]
    if missing_paths:
        pytest.skip(
            f"Missing local GIS data files for live tests: {', '.join(missing_paths)}",
            allow_module_level=False,
        )


@pytest.fixture(autouse=True)
def _live_llm_test_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LOCAL_FILES_ONLY_MODE", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_live_llm_parser_and_planner_smoke() -> None:
    parsed = parse_task_message(
        "请分析 bbox(116.1,39.8,116.5,40.1) 在 2024-06-01 到 2024-06-30 的 NDVI。",
        has_upload=False,
    )

    assert parsed.created_from is not None
    assert parsed.created_from.startswith("llm:")
    assert parsed.analysis_type in {
        "NDVI",
        "NDWI",
        "BAND_MATH",
        "FILTER",
        "SLOPE_ASPECT",
        "BUFFER",
        "CLIP",
    }
    assert parsed.time_range is not None
    assert parsed.time_range.get("start") is not None
    assert parsed.time_range.get("end") is not None

    plan = build_task_plan(parsed)

    assert plan.mode in {
        "llm_plan_execute_gis_workspace",
        "agent_driven_gis_workspace",
        "execution",
    }
    assert len(plan.steps) >= 1
    assert any(step.step_name == "plan_task" for step in plan.steps)


def test_live_llm_recommendation_smoke() -> None:
    candidates = [
        CatalogCandidate(
            dataset_name="sentinel2",
            collection_id="sentinel-2-l2a",
            scene_count=8,
            coverage_ratio=0.92,
            effective_pixel_ratio_estimate=0.82,
            cloud_metric_summary={"median": 12.0, "p75": 18.0},
            spatial_resolution=10,
            temporal_density_note="high",
            suitability_score=0.88,
            recommendation_rank=1,
            summary_json={"source": "live_smoke"},
        ),
        CatalogCandidate(
            dataset_name="landsat89",
            collection_id="landsat-c2-l2",
            scene_count=7,
            coverage_ratio=0.9,
            effective_pixel_ratio_estimate=0.76,
            cloud_metric_summary={"median": 16.0, "p75": 25.0},
            spatial_resolution=30,
            temporal_density_note="medium",
            suitability_score=0.79,
            recommendation_rank=2,
            summary_json={"source": "live_smoke"},
        ),
    ]

    recommendation = build_recommendation(candidates, user_priority="balanced")

    assert recommendation.get("primary_dataset") in {"sentinel2", "landsat89"}
    assert recommendation.get("backup_dataset") in {"sentinel2", "landsat89", None}
    assert isinstance(recommendation.get("scores"), dict)
    assert recommendation.get("reason")


def _parse_and_plan_with_live_llm(message: str) -> tuple[ParsedTaskSpec, OperationPlan]:
    parsed = parse_task_message(message, has_upload=False)
    assert parsed.created_from is not None
    assert parsed.created_from.startswith("llm:")
    assert parsed.need_confirmation is False

    operation_plan = build_operation_plan_from_registry(parsed, status="draft")
    validate_operation_plan(operation_plan)
    return parsed, operation_plan


LIVE_OPERATION_CASES: list[dict[str, str]] = [
    {
        "case_id": "clip_raster_by_geojson",
        "message": (
            f"请将 analysis_type 设为 clip，把栅格 {LOCAL_RASTER} 按矢量 {LOCAL_CLIP_VECTOR} 裁剪，"
            "输出 GeoTIFF。"
        ),
        "expected_op": "raster.clip",
        "expected_artifact_type": "geotiff",
    },
    {
        "case_id": "ndvi_from_local_raster",
        "message": (
            f"请将 analysis_type 设为 ndvi，使用栅格 {LOCAL_RASTER}，"
            "在 bbox(116.1,39.8,116.5,40.1) 和 2024-06-01 到 2024-06-30 条件下计算 NDVI，"
            "输出 GeoTIFF。"
        ),
        "expected_op": "raster.band_math",
        "expected_artifact_type": "geotiff",
    },
    {
        "case_id": "ndwi_from_local_raster",
        "message": (
            f"请将 analysis_type 设为 ndwi，使用栅格 {LOCAL_RASTER_SECOND}，"
            "在 bbox(116.1,39.8,116.5,40.1) 和 2024-06-01 到 2024-06-30 条件下计算 NDWI，"
            "输出 GeoTIFF。"
        ),
        "expected_op": "raster.band_math",
        "expected_artifact_type": "geotiff",
    },
    {
        "case_id": "terrain_slope_from_local_raster",
        "message": (
            f"请将 analysis_type 设为 slope_aspect，使用栅格 {LOCAL_RASTER}，"
            "在 bbox(116.1,39.8,116.5,40.1) 和 2024-06-01 到 2024-06-30 条件下做坡度分析，"
            "输出 GeoTIFF。"
        ),
        "expected_op": "raster.terrain_slope",
        "expected_artifact_type": "geotiff",
    },
    {
        "case_id": "vector_buffer_from_local_shp",
        "message": (
            f"请将 analysis_type 设为 buffer，对矢量 {LOCAL_VECTOR} 做 1000 米缓冲区分析，"
            "AOI 用 bbox(116.1,39.8,116.5,40.1)，时间用 2024-06-01 到 2024-06-30，"
            "并导出 GeoJSON。"
        ),
        "expected_op": "vector.buffer",
        "expected_artifact_type": "geojson",
    },
]

LIVE_PIPELINE_EXECUTION_CASE_IDS = {
    "clip_raster_by_geojson",
    "vector_buffer_from_local_shp",
}

LIVE_PIPELINE_EXECUTION_CASES = (
    LIVE_OPERATION_CASES
    if _live_full_pipeline_mode()
    else [
        case for case in LIVE_OPERATION_CASES if case["case_id"] in LIVE_PIPELINE_EXECUTION_CASE_IDS
    ]
)


@pytest.mark.parametrize(
    ("case_id", "message", "expected_op"),
    [(case["case_id"], case["message"], case["expected_op"]) for case in LIVE_OPERATION_CASES],
)
def test_live_llm_local_gis_operation_cases(case_id: str, message: str, expected_op: str) -> None:
    del case_id
    parsed, operation_plan = _parse_and_plan_with_live_llm(message)

    op_names = {node.op_name for node in operation_plan.nodes}
    assert expected_op in op_names

    if expected_op == "raster.clip":
        source_path = str(parsed.operation_params.get("source_path") or "")
        clip_path = str(parsed.operation_params.get("clip_path") or "")
        assert source_path.endswith("CACD-2020.tif")
        assert clip_path.endswith("xinjiang_small_clip_for_agent_test.geojson")

    if expected_op in {"raster.band_math", "raster.terrain_slope"}:
        source_path = str(parsed.operation_params.get("source_path") or "")
        assert source_path.endswith(".tif")


@pytest.mark.parametrize(
    ("case_id", "message", "expected_artifact_type"),
    [
        (case["case_id"], case["message"], case["expected_artifact_type"])
        for case in LIVE_PIPELINE_EXECUTION_CASES
    ],
)
def test_live_llm_local_gis_pipeline_execution_cases(
    case_id: str,
    message: str,
    expected_artifact_type: str,
    tmp_path: Path,
) -> None:
    _, operation_plan = _parse_and_plan_with_live_llm(message)

    working_dir = tmp_path / case_id
    outputs = run_processing_pipeline(
        task_id=f"live_llm_{case_id}",
        plan_nodes=[node.model_dump() for node in operation_plan.nodes],
        working_dir=working_dir,
    )

    assert outputs.get("mode") == "operation_plan"
    artifacts = outputs.get("artifacts") or []
    assert artifacts
    artifact_types = {str(item.get("artifact_type") or "") for item in artifacts}
    assert expected_artifact_type in artifact_types

    for artifact in artifacts:
        path = Path(str(artifact.get("path") or ""))
        assert path.exists()
