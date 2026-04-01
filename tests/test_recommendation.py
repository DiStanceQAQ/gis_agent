from __future__ import annotations

from packages.domain.services.catalog import CatalogCandidate
from packages.domain.services.recommendation import build_recommendation


def test_build_recommendation_prefers_lower_cloud_option() -> None:
    recommendation = build_recommendation(
        [
            CatalogCandidate(
                dataset_name="landsat89",
                collection_id="landsat-c2-l2",
                scene_count=36,
                coverage_ratio=1.0,
                effective_pixel_ratio_estimate=0.78,
                cloud_metric_summary={"median": 12.0, "p75": 18.0},
                spatial_resolution=30,
                temporal_density_note="high",
                suitability_score=0.86,
                recommendation_rank=1,
                summary_json={"source": "planetary_computer_live"},
            ),
            CatalogCandidate(
                dataset_name="sentinel2",
                collection_id="sentinel-2-l2a",
                scene_count=38,
                coverage_ratio=1.0,
                effective_pixel_ratio_estimate=0.46,
                cloud_metric_summary={"median": 54.0, "p75": 78.0},
                spatial_resolution=10,
                temporal_density_note="high",
                suitability_score=0.84,
                recommendation_rank=2,
                summary_json={"source": "planetary_computer_live"},
            ),
        ],
        user_priority="balanced",
    )

    assert recommendation["primary_dataset"] == "landsat89"
    assert "有效像元比例更高" in recommendation["reason"]
    assert recommendation["scores"]["landsat89"] == 0.86


def test_build_recommendation_reports_mock_risk() -> None:
    recommendation = build_recommendation(
        [
            CatalogCandidate(
                dataset_name="sentinel2",
                collection_id="sentinel-2-l2a",
                scene_count=18,
                coverage_ratio=0.98,
                effective_pixel_ratio_estimate=0.81,
                cloud_metric_summary={"median": 12.0, "p75": 20.0},
                spatial_resolution=10,
                temporal_density_note="high",
                suitability_score=0.87,
                recommendation_rank=1,
                summary_json={"source": "mock_catalog"},
            ),
            CatalogCandidate(
                dataset_name="landsat89",
                collection_id="landsat-c2-l2",
                scene_count=11,
                coverage_ratio=0.96,
                effective_pixel_ratio_estimate=0.78,
                cloud_metric_summary={"median": 16.0, "p75": 24.0},
                spatial_resolution=30,
                temporal_density_note="medium",
                suitability_score=0.74,
                recommendation_rank=2,
                summary_json={"source": "mock_catalog"},
            ),
        ],
        user_priority="spatial",
    )

    assert recommendation["primary_dataset"] == "sentinel2"
    assert recommendation["risk_note"] is not None
    assert "mock catalog" in recommendation["risk_note"]
