from __future__ import annotations

import pytest

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.services.catalog import CatalogCandidate
from packages.domain.services.llm_client import LLMResponse, LLMUsage
from packages.domain.services.recommendation import build_recommendation


def _candidate(
    *,
    dataset_name: str,
    scene_count: int,
    effective_pixel_ratio_estimate: float,
    cloud_median: float,
    spatial_resolution: int,
    suitability_score: float,
    coverage_ratio: float = 0.96,
    source: str = "planetary_computer_live",
) -> CatalogCandidate:
    return CatalogCandidate(
        dataset_name=dataset_name,
        collection_id="sentinel-2-l2a" if dataset_name == "sentinel2" else "landsat-c2-l2",
        scene_count=scene_count,
        coverage_ratio=coverage_ratio,
        effective_pixel_ratio_estimate=effective_pixel_ratio_estimate,
        cloud_metric_summary={"median": cloud_median, "p75": cloud_median + 8},
        spatial_resolution=spatial_resolution,
        temporal_density_note="high",
        suitability_score=suitability_score,
        recommendation_rank=1,
        summary_json={"source": source},
    )


def _mock_llm_recommendation_response(payload: dict) -> LLMResponse:
    return LLMResponse(
        model="gpt-4o-mini",
        request_id="req_recommendation_mock",
        content_text=str(payload),
        content_json=payload,
        usage=LLMUsage(input_tokens=10, output_tokens=10, total_tokens=20),
        latency_ms=12,
        raw_payload={"choices": [{"message": {"content": str(payload)}}]},
    )


def test_build_recommendation_returns_placeholder_for_empty_candidates() -> None:
    recommendation = build_recommendation([], user_priority="balanced")

    assert recommendation["primary_dataset"] == "unknown"
    assert recommendation["scores"] == {}
    assert "没有可用候选" in recommendation["reason"]


def test_build_recommendation_prefers_lower_cloud_option() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="landsat89",
                scene_count=36,
                effective_pixel_ratio_estimate=0.78,
                cloud_median=12.0,
                spatial_resolution=30,
                suitability_score=0.86,
            ),
            _candidate(
                dataset_name="sentinel2",
                scene_count=38,
                effective_pixel_ratio_estimate=0.46,
                cloud_median=54.0,
                spatial_resolution=10,
                suitability_score=0.84,
            ),
        ],
        user_priority="balanced",
    )

    assert recommendation["primary_dataset"] == "landsat89"
    assert "有效像元比例更高" in recommendation["reason"]
    assert recommendation["scores"]["landsat89"] == 0.86


def test_build_recommendation_prefers_higher_resolution_for_spatial_priority() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=10,
                effective_pixel_ratio_estimate=0.68,
                cloud_median=14.0,
                spatial_resolution=10,
                suitability_score=0.78,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=10,
                effective_pixel_ratio_estimate=0.66,
                cloud_median=15.0,
                spatial_resolution=30,
                suitability_score=0.75,
            ),
        ],
        user_priority="spatial",
    )

    assert recommendation["primary_dataset"] == "sentinel2"
    assert "空间细节" in recommendation["reason"]


def test_build_recommendation_prefers_higher_scene_count_for_temporal_priority() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="landsat89",
                scene_count=12,
                effective_pixel_ratio_estimate=0.68,
                cloud_median=12.0,
                spatial_resolution=30,
                suitability_score=0.8,
            ),
            _candidate(
                dataset_name="sentinel2",
                scene_count=7,
                effective_pixel_ratio_estimate=0.69,
                cloud_median=11.0,
                spatial_resolution=10,
                suitability_score=0.79,
            ),
        ],
        user_priority="temporal",
    )

    assert recommendation["primary_dataset"] == "landsat89"
    assert "可用景数更多" in recommendation["reason"]


def test_build_recommendation_reports_baseline_risk() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=18,
                effective_pixel_ratio_estimate=0.81,
                cloud_median=12.0,
                spatial_resolution=10,
                suitability_score=0.87,
                source="baseline_catalog",
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=11,
                effective_pixel_ratio_estimate=0.78,
                cloud_median=16.0,
                spatial_resolution=30,
                suitability_score=0.74,
                source="baseline_catalog",
            ),
        ],
        user_priority="spatial",
    )

    assert recommendation["primary_dataset"] == "sentinel2"
    assert recommendation["risk_note"] is not None
    assert "baseline catalog" in recommendation["risk_note"]


def test_build_recommendation_warns_on_low_scene_count() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=2,
                effective_pixel_ratio_estimate=0.83,
                cloud_median=12.0,
                spatial_resolution=10,
                suitability_score=0.9,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=1,
                effective_pixel_ratio_estimate=0.72,
                cloud_median=14.0,
                spatial_resolution=30,
                suitability_score=0.71,
            ),
        ],
        user_priority="balanced",
    )

    assert "可用景数偏少" in (recommendation["risk_note"] or "")


def test_build_recommendation_warns_on_high_cloud_cover() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=8,
                effective_pixel_ratio_estimate=0.71,
                cloud_median=48.0,
                spatial_resolution=10,
                suitability_score=0.84,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=7,
                effective_pixel_ratio_estimate=0.7,
                cloud_median=42.0,
                spatial_resolution=30,
                suitability_score=0.8,
            ),
        ],
        user_priority="balanced",
    )

    assert "云量统计偏高" in (recommendation["risk_note"] or "")


def test_build_recommendation_warns_when_primary_and_backup_are_close() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=8,
                effective_pixel_ratio_estimate=0.76,
                cloud_median=12.0,
                spatial_resolution=10,
                suitability_score=0.82,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=8,
                effective_pixel_ratio_estimate=0.74,
                cloud_median=13.0,
                spatial_resolution=30,
                suitability_score=0.79,
            ),
        ],
        user_priority="balanced",
    )

    assert "评分接近" in (recommendation["risk_note"] or "")


def test_build_recommendation_respects_requested_dataset() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=18,
                effective_pixel_ratio_estimate=0.86,
                cloud_median=12.0,
                spatial_resolution=10,
                suitability_score=0.9,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=11,
                effective_pixel_ratio_estimate=0.78,
                cloud_median=16.0,
                spatial_resolution=30,
                suitability_score=0.74,
            ),
        ],
        user_priority="balanced",
        requested_dataset="landsat89",
    )

    assert recommendation["primary_dataset"] == "landsat89"
    assert "用户明确指定" in recommendation["reason"]


def test_build_recommendation_warns_when_forced_choice_is_weaker_than_backup() -> None:
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=18,
                effective_pixel_ratio_estimate=0.86,
                cloud_median=12.0,
                spatial_resolution=10,
                suitability_score=0.91,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=11,
                effective_pixel_ratio_estimate=0.7,
                cloud_median=20.0,
                spatial_resolution=30,
                suitability_score=0.72,
            ),
        ],
        user_priority="balanced",
        requested_dataset="landsat89",
    )

    assert recommendation["primary_dataset"] == "landsat89"
    assert "更稳妥的备选" in (recommendation["risk_note"] or "")


def test_build_recommendation_returns_error_code_when_llm_schema_validation_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_SCHEMA_RETRIES", "0")
    get_settings.cache_clear()

    invalid_payload = {
        "primary_dataset": "modis",
        "backup_dataset": "landsat89",
        "scores": {"sentinel2": 0.87},
        "reason": "sentinel2 更优",
        "risk_note": None,
    }

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self, kwargs
        return _mock_llm_recommendation_response(invalid_payload)

    monkeypatch.setattr("packages.domain.services.recommendation.LLMClient.chat_json", _fake_chat_json)
    recommendation = build_recommendation(
        [
            _candidate(
                dataset_name="sentinel2",
                scene_count=18,
                effective_pixel_ratio_estimate=0.82,
                cloud_median=10.0,
                spatial_resolution=10,
                suitability_score=0.9,
            ),
            _candidate(
                dataset_name="landsat89",
                scene_count=12,
                effective_pixel_ratio_estimate=0.77,
                cloud_median=14.0,
                spatial_resolution=30,
                suitability_score=0.76,
            ),
        ],
        user_priority="balanced",
    )

    assert recommendation["primary_dataset"] == "unknown"
    assert recommendation["error_code"] == ErrorCode.TASK_LLM_RECOMMENDATION_SCHEMA_VALIDATION_FAILED
    assert recommendation["error_message"] is not None
    get_settings.cache_clear()
