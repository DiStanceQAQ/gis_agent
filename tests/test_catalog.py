from __future__ import annotations

from packages.domain.models import AOIRecord
from packages.domain.services.catalog import search_candidates
from packages.schemas.task import ParsedTaskSpec


def test_search_candidates_returns_consistent_schema() -> None:
    spec = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-08-31"},
        user_priority="balanced",
    )
    aoi = AOIRecord(
        id="aoi_test",
        task_id="task_test",
        name="test-aoi",
        bbox_bounds_json=[116.1, 39.8, 116.5, 40.1],
        area_km2=1139.986,
        is_valid=True,
    )

    candidates = search_candidates(spec, aoi, live_search=False)

    assert [candidate.dataset_name for candidate in candidates] == ["sentinel2", "landsat89"]
    assert candidates[0].collection_id == "sentinel-2-l2a"
    assert candidates[1].collection_id == "landsat-c2-l2"
    assert candidates[0].scene_count >= candidates[1].scene_count
    assert candidates[0].summary_json["source"] == "mock_catalog"


def test_search_candidates_prefers_landsat_for_temporal_priority() -> None:
    spec = ParsedTaskSpec(
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        time_range={"start": "2024-01-01", "end": "2024-12-31"},
        user_priority="temporal",
    )

    candidates = search_candidates(spec, None, live_search=False)

    assert candidates[0].dataset_name == "landsat89"
    assert candidates[0].recommendation_rank == 1
    assert candidates[0].suitability_score > candidates[1].suitability_score
