from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from statistics import median, quantiles
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.logging import get_logger
from packages.domain.models import AOIRecord, DatasetCandidateRecord, TaskRunRecord
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)


@dataclass(frozen=True)
class CollectionProfile:
    dataset_name: str
    collection_id: str
    spatial_resolution: int
    revisit_days: int


@dataclass
class CatalogCandidate:
    dataset_name: str
    collection_id: str
    scene_count: int
    coverage_ratio: float
    effective_pixel_ratio_estimate: float
    cloud_metric_summary: dict[str, float]
    spatial_resolution: int
    temporal_density_note: str
    suitability_score: float
    recommendation_rank: int
    summary_json: dict[str, object]


COLLECTION_PROFILES = (
    CollectionProfile(
        dataset_name="sentinel2",
        collection_id="sentinel-2-l2a",
        spatial_resolution=10,
        revisit_days=5,
    ),
    CollectionProfile(
        dataset_name="landsat89",
        collection_id="landsat-c2-l2",
        spatial_resolution=30,
        revisit_days=16,
    ),
)


def _date_span_days(spec: ParsedTaskSpec) -> int:
    if not spec.time_range:
        return 90
    start = date.fromisoformat(spec.time_range["start"])
    end = date.fromisoformat(spec.time_range["end"])
    return max(1, (end - start).days + 1)


def _round_ratio(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 2)


def _format_datetime_range(spec: ParsedTaskSpec) -> str | None:
    if not spec.time_range:
        return None
    return f"{spec.time_range['start']}T00:00:00Z/{spec.time_range['end']}T23:59:59Z"


def _bbox_for_search(aoi: AOIRecord | None) -> list[float] | None:
    if aoi is None or not aoi.bbox_bounds_json:
        return None
    return [float(value) for value in aoi.bbox_bounds_json]


def _cloud_cover(item: dict[str, Any]) -> float:
    value = (item.get("properties") or {}).get("eo:cloud_cover")
    return float(value) if isinstance(value, (int, float)) else 100.0


def _sort_items_by_cloud(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=_cloud_cover)


def _post_json(url: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.load(response)


def _fetch_stac_items(
    *,
    collection_id: str,
    bbox: list[float],
    datetime_range: str,
    timeout_seconds: int,
    page_size: int,
    max_items: int,
    search_url: str,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "collections": [collection_id],
        "bbox": bbox,
        "datetime": datetime_range,
        "limit": min(page_size, max_items),
    }

    features: list[dict[str, Any]] = []
    next_payload: dict[str, Any] | None = payload

    while next_payload and len(features) < max_items:
        data = _post_json(search_url, next_payload, timeout_seconds)
        page_features = data.get("features") or []
        remaining = max_items - len(features)
        features.extend(page_features[:remaining])

        next_link = next(
            (link for link in data.get("links") or [] if link.get("rel") == "next"),
            None,
        )
        if not next_link or not isinstance(next_link.get("body"), dict) or len(features) >= max_items:
            break
        next_payload = next_link["body"]

    return features


def get_collection_profile(dataset_name: str) -> CollectionProfile:
    for profile in COLLECTION_PROFILES:
        if profile.dataset_name == dataset_name:
            return profile
    raise ValueError(f"Unsupported dataset profile: {dataset_name}")


def _cloud_values(items: list[dict[str, Any]]) -> list[float]:
    clouds: list[float] = []
    for item in items:
        value = (item.get("properties") or {}).get("eo:cloud_cover")
        if isinstance(value, (int, float)):
            clouds.append(float(value))
    return clouds


def _cloud_summary(items: list[dict[str, Any]]) -> dict[str, float]:
    clouds = _cloud_values(items)
    if not clouds:
        return {"median": 0.0, "p75": 0.0}
    if len(clouds) == 1:
        percentile_75 = clouds[0]
    else:
        percentile_75 = quantiles(clouds, n=4, method="inclusive")[2]
    return {
        "median": round(median(clouds), 3),
        "p75": round(percentile_75, 3),
    }


def _temporal_density_note(scene_count: int, span_days: int, revisit_days: int) -> str:
    expected = max(1, span_days / revisit_days)
    ratio = scene_count / expected
    if ratio >= 0.85:
        return "high"
    if ratio >= 0.45:
        return "medium"
    if ratio > 0:
        return "low"
    return "none"


def _resolution_score(profile: CollectionProfile) -> float:
    return 1.0 if profile.spatial_resolution <= 10 else 0.65


def _score_candidate(
    *,
    spec: ParsedTaskSpec,
    profile: CollectionProfile,
    coverage_ratio: float,
    effective_pixel_ratio: float,
    scene_count: int,
    span_days: int,
) -> float:
    resolution_score = _resolution_score(profile)
    temporal_score = _round_ratio(scene_count / max(1.0, span_days / profile.revisit_days))

    if spec.user_priority == "spatial":
        weights = {
            "coverage": 0.25,
            "effective": 0.25,
            "resolution": 0.35,
            "temporal": 0.15,
        }
    elif spec.user_priority == "temporal":
        weights = {
            "coverage": 0.30,
            "effective": 0.25,
            "resolution": 0.10,
            "temporal": 0.35,
        }
    else:
        weights = {
            "coverage": 0.30,
            "effective": 0.30,
            "resolution": 0.20,
            "temporal": 0.20,
        }

    return round(
        weights["coverage"] * coverage_ratio
        + weights["effective"] * effective_pixel_ratio
        + weights["resolution"] * resolution_score
        + weights["temporal"] * temporal_score,
        2,
    )


def _candidate_from_items(
    *,
    spec: ParsedTaskSpec,
    aoi: AOIRecord | None,
    profile: CollectionProfile,
    items: list[dict[str, Any]],
    source: str,
) -> CatalogCandidate:
    area_km2 = float(aoi.area_km2 or 800.0) if aoi else 800.0
    span_days = _date_span_days(spec)
    scene_count = len(items)
    expected_scene_count = max(1, round(span_days / profile.revisit_days))
    area_penalty = min(0.08, area_km2 / 500000.0 * 0.02)
    coverage_ratio = _round_ratio(scene_count / expected_scene_count - area_penalty)
    cloud_summary = _cloud_summary(items)
    effective_pixel_ratio = _round_ratio(
        coverage_ratio * (1 - cloud_summary["median"] / 100.0)
    )
    temporal_density_note = _temporal_density_note(scene_count, span_days, profile.revisit_days)
    suitability_score = _score_candidate(
        spec=spec,
        profile=profile,
        coverage_ratio=coverage_ratio,
        effective_pixel_ratio=effective_pixel_ratio,
        scene_count=scene_count,
        span_days=span_days,
    )

    if scene_count == 0:
        temporal_density_note = "none"
        suitability_score = 0.0

    return CatalogCandidate(
        dataset_name=profile.dataset_name,
        collection_id=profile.collection_id,
        scene_count=scene_count,
        coverage_ratio=coverage_ratio if scene_count else 0.0,
        effective_pixel_ratio_estimate=effective_pixel_ratio if scene_count else 0.0,
        cloud_metric_summary=cloud_summary,
        spatial_resolution=profile.spatial_resolution,
        temporal_density_note=temporal_density_note,
        suitability_score=suitability_score,
        recommendation_rank=99,
        summary_json={
            "source": source,
            "estimated_area_km2": round(area_km2, 3),
            "time_range_days": span_days,
            "sampled_items": scene_count,
        },
    )


def _build_mock_candidates(spec: ParsedTaskSpec, aoi: AOIRecord | None) -> list[CatalogCandidate]:
    area_km2 = float(aoi.area_km2 or 800.0) if aoi else 800.0
    span_days = _date_span_days(spec)

    candidates = [
        CatalogCandidate(
            dataset_name="sentinel2",
            collection_id="sentinel-2-l2a",
            scene_count=max(4, min(24, span_days // 5)),
            coverage_ratio=_round_ratio(0.99 - min(area_km2 / 250000.0, 0.08)),
            effective_pixel_ratio_estimate=_round_ratio(0.84 - min(area_km2 / 400000.0, 0.06)),
            cloud_metric_summary={"median": 0.12, "p75": 0.2},
            spatial_resolution=10,
            temporal_density_note="high" if span_days <= 120 else "medium",
            suitability_score=0.84 if spec.user_priority != "temporal" else 0.72,
            recommendation_rank=1 if spec.user_priority != "temporal" else 2,
            summary_json={
                "source": "mock_catalog",
                "estimated_area_km2": round(area_km2, 3),
                "time_range_days": span_days,
            },
        ),
        CatalogCandidate(
            dataset_name="landsat89",
            collection_id="landsat-c2-l2",
            scene_count=max(3, min(18, span_days // 8)),
            coverage_ratio=_round_ratio(0.97 - min(area_km2 / 350000.0, 0.05)),
            effective_pixel_ratio_estimate=_round_ratio(0.79 - min(area_km2 / 450000.0, 0.04)),
            cloud_metric_summary={"median": 0.16, "p75": 0.24},
            spatial_resolution=30,
            temporal_density_note="medium" if span_days <= 180 else "high",
            suitability_score=0.74 if spec.user_priority != "temporal" else 0.86,
            recommendation_rank=2 if spec.user_priority != "temporal" else 1,
            summary_json={
                "source": "mock_catalog",
                "estimated_area_km2": round(area_km2, 3),
                "time_range_days": span_days,
            },
        ),
    ]
    return candidates


def _assign_recommendation_rank(candidates: list[CatalogCandidate]) -> list[CatalogCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (-item.suitability_score, -item.coverage_ratio, item.spatial_resolution),
    )
    for index, candidate in enumerate(ordered, start=1):
        candidate.recommendation_rank = index
    return ordered


def _search_live_candidates(spec: ParsedTaskSpec, aoi: AOIRecord) -> list[CatalogCandidate]:
    settings = get_settings()
    bbox = _bbox_for_search(aoi)
    datetime_range = _format_datetime_range(spec)
    if bbox is None or datetime_range is None:
        raise ValueError("Live catalog search requires both bbox and time_range.")

    search_url = f"{settings.catalog_stac_url.rstrip('/')}/search"
    candidates: list[CatalogCandidate] = []
    for profile in COLLECTION_PROFILES:
        items = _sort_items_by_cloud(
            _fetch_stac_items(
                collection_id=profile.collection_id,
                bbox=bbox,
                datetime_range=datetime_range,
                timeout_seconds=settings.catalog_timeout_seconds,
                page_size=settings.catalog_page_size,
                max_items=settings.catalog_max_items,
                search_url=search_url,
            )
        )
        candidates.append(
            _candidate_from_items(
                spec=spec,
                aoi=aoi,
                profile=profile,
                items=items,
                source="planetary_computer_live",
            )
        )
    return _assign_recommendation_rank(candidates)


def search_items_for_dataset(
    spec: ParsedTaskSpec,
    aoi: AOIRecord,
    *,
    dataset_name: str,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    profile = get_collection_profile(dataset_name)
    bbox = _bbox_for_search(aoi)
    datetime_range = _format_datetime_range(spec)
    if bbox is None or datetime_range is None:
        return []

    search_url = f"{settings.catalog_stac_url.rstrip('/')}/search"
    items = _fetch_stac_items(
        collection_id=profile.collection_id,
        bbox=bbox,
        datetime_range=datetime_range,
        timeout_seconds=settings.catalog_timeout_seconds,
        page_size=settings.catalog_page_size,
        max_items=max_items or settings.catalog_max_items,
        search_url=search_url,
    )
    return _sort_items_by_cloud(items)


def search_candidates(
    spec: ParsedTaskSpec,
    aoi: AOIRecord | None,
    *,
    live_search: bool | None = None,
) -> list[CatalogCandidate]:
    settings = get_settings()
    use_live_search = settings.catalog_live_search if live_search is None else live_search

    if use_live_search and aoi is not None and _bbox_for_search(aoi) and spec.time_range:
        try:
            live_candidates = _search_live_candidates(spec, aoi)
            logger.info(
                "catalog.search.live_success dataset_names=%s",
                [candidate.dataset_name for candidate in live_candidates],
            )
            return live_candidates
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("catalog.search.live_failed reason=%s", repr(exc))
            if not settings.catalog_allow_mock_fallback:
                raise

    mock_candidates = _assign_recommendation_rank(_build_mock_candidates(spec, aoi))
    logger.info(
        "catalog.search.mock_fallback dataset_names=%s",
        [candidate.dataset_name for candidate in mock_candidates],
    )
    return mock_candidates


def persist_candidates(
    db: Session,
    *,
    task: TaskRunRecord,
    candidates: list[CatalogCandidate],
) -> list[DatasetCandidateRecord]:
    for candidate in list(task.candidates):
        db.delete(candidate)
    db.flush()

    records: list[DatasetCandidateRecord] = []
    for candidate in candidates:
        record = DatasetCandidateRecord(task_id=task.id, **asdict(candidate))
        db.add(record)
        records.append(record)
    db.flush()
    return records
