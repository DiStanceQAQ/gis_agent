from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.domain.models import AOIRecord
from packages.domain.services import catalog
from packages.domain.services.catalog import CatalogCandidate, search_candidates
from packages.schemas.task import ParsedTaskSpec


def _spec(
    *,
    user_priority: str = "balanced",
    time_range: dict[str, str] | None | object = ...,
) -> ParsedTaskSpec:
    return ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-08-31"} if time_range is ... else time_range,
        user_priority=user_priority,
    )


def _aoi() -> AOIRecord:
    return AOIRecord(
        id="aoi_test",
        task_id="task_test",
        name="test-aoi",
        bbox_bounds_json=[116.1, 39.8, 116.5, 40.1],
        area_km2=1139.986,
        is_valid=True,
    )


def _candidate(
    *,
    dataset_name: str,
    score: float,
    rank: int,
    source: str = "planetary_computer_live",
) -> CatalogCandidate:
    collection_id = "sentinel-2-l2a" if dataset_name == "sentinel2" else "landsat-c2-l2"
    return CatalogCandidate(
        dataset_name=dataset_name,
        collection_id=collection_id,
        scene_count=6,
        coverage_ratio=0.95,
        effective_pixel_ratio_estimate=0.8,
        cloud_metric_summary={"median": 12.0, "p75": 20.0},
        spatial_resolution=10 if dataset_name == "sentinel2" else 30,
        temporal_density_note="high",
        suitability_score=score,
        recommendation_rank=rank,
        summary_json={"source": source},
    )


def test_search_candidates_returns_consistent_baseline_schema() -> None:
    candidates = search_candidates(_spec(), _aoi(), live_search=False)

    assert [candidate.dataset_name for candidate in candidates] == ["sentinel2", "landsat89"]
    assert candidates[0].collection_id == "sentinel-2-l2a"
    assert candidates[1].collection_id == "landsat-c2-l2"
    assert candidates[0].scene_count >= candidates[1].scene_count
    assert candidates[0].summary_json["source"] == "baseline_catalog"
    assert candidates[0].recommendation_rank == 1
    assert candidates[1].recommendation_rank == 2


def test_search_candidates_prefers_landsat_for_temporal_priority() -> None:
    candidates = search_candidates(
        _spec(
            user_priority="temporal",
            time_range={"start": "2024-01-01", "end": "2024-12-31"},
        ),
        None,
        live_search=False,
    )

    assert candidates[0].dataset_name == "landsat89"
    assert candidates[0].recommendation_rank == 1
    assert candidates[0].suitability_score > candidates[1].suitability_score


def test_search_candidates_uses_live_results_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    live_candidates = [
        _candidate(dataset_name="landsat89", score=0.9, rank=1),
        _candidate(dataset_name="sentinel2", score=0.82, rank=2),
    ]
    monkeypatch.setattr(catalog, "_search_live_candidates", lambda spec, aoi: live_candidates)
    monkeypatch.setattr(
        catalog,
        "get_settings",
        lambda: SimpleNamespace(catalog_live_search=True, catalog_allow_baseline_fallback=True),
    )

    candidates = search_candidates(_spec(), _aoi(), live_search=True)

    assert candidates == live_candidates
    assert [candidate.summary_json["source"] for candidate in candidates] == ["planetary_computer_live"] * 2


def test_search_candidates_falls_back_to_baseline_when_live_search_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "_search_live_candidates",
        lambda spec, aoi: (_ for _ in ()).throw(TimeoutError("simulated timeout")),
    )
    monkeypatch.setattr(
        catalog,
        "get_settings",
        lambda: SimpleNamespace(catalog_live_search=True, catalog_allow_baseline_fallback=True),
    )

    candidates = search_candidates(_spec(), _aoi(), live_search=True)

    assert [candidate.dataset_name for candidate in candidates] == ["sentinel2", "landsat89"]
    assert all(candidate.summary_json["source"] == "baseline_catalog" for candidate in candidates)


def test_search_candidates_raises_when_baseline_fallback_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "_search_live_candidates",
        lambda spec, aoi: (_ for _ in ()).throw(TimeoutError("simulated timeout")),
    )
    monkeypatch.setattr(
        catalog,
        "get_settings",
        lambda: SimpleNamespace(catalog_live_search=True, catalog_allow_baseline_fallback=False),
    )

    with pytest.raises(TimeoutError, match="simulated timeout"):
        search_candidates(_spec(), _aoi(), live_search=True)


def test_search_candidates_skips_live_search_when_time_range_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "_search_live_candidates",
        lambda spec, aoi: (_ for _ in ()).throw(AssertionError("live search should not run")),
    )
    monkeypatch.setattr(
        catalog,
        "get_settings",
        lambda: SimpleNamespace(catalog_live_search=True, catalog_allow_baseline_fallback=False),
    )

    candidates = search_candidates(_spec(time_range=None), _aoi(), live_search=True)

    assert all(candidate.summary_json["source"] == "baseline_catalog" for candidate in candidates)
