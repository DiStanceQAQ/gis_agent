from packages.domain.services.catalog import CatalogCandidate
from packages.domain.services.qc import build_fallback_detail, evaluate_candidate_qc


def test_evaluate_candidate_qc_returns_warning_for_low_quality_primary() -> None:
    result = evaluate_candidate_qc(
        [
            CatalogCandidate(
                dataset_name="landsat89",
                collection_id="landsat-c2-l2",
                scene_count=2,
                coverage_ratio=0.95,
                effective_pixel_ratio_estimate=0.42,
                cloud_metric_summary={"median": 45.0, "p75": 55.0},
                spatial_resolution=30,
                temporal_density_note="low",
                suitability_score=0.81,
                recommendation_rank=1,
                summary_json={"source": "planetary_computer_live"},
            )
        ]
    )

    assert result.status == "warn"
    codes = {issue["code"] for issue in result.issues}
    assert {"low_scene_count", "low_effective_pixel_ratio", "high_cloud_cover"} <= codes


def test_evaluate_candidate_qc_returns_error_for_empty_candidates() -> None:
    result = evaluate_candidate_qc([])

    assert result.status == "error"
    assert result.issues[0]["code"] == "no_candidates"


def test_build_fallback_detail_for_real_pipeline_to_baseline() -> None:
    detail = build_fallback_detail(
        "real_pipeline_to_baseline",
        reason="ValueError('No live STAC items found')",
        from_mode="real",
        to_mode="baseline",
    )

    assert detail["strategy"] == "real_pipeline_to_baseline"
    assert detail["step_name"] == "run_processing_pipeline"
    assert detail["from_mode"] == "real"
    assert detail["to_mode"] == "baseline"
