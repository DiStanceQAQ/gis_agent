from __future__ import annotations

from typing import Any, Iterable


DATASET_LABELS = {
    "sentinel2": "Sentinel-2",
    "landsat89": "Landsat 8/9",
}


def _candidate_sort_key(candidate: Any) -> tuple[float, float, int]:
    return (
        float(getattr(candidate, "suitability_score", 0.0) or 0.0),
        float(getattr(candidate, "coverage_ratio", 0.0) or 0.0),
        -int(getattr(candidate, "spatial_resolution", 9999) or 9999),
    )


def _label(dataset_name: str) -> str:
    return DATASET_LABELS.get(dataset_name, dataset_name)


def _cloud_median(candidate: Any) -> float:
    summary = getattr(candidate, "cloud_metric_summary", None) or {}
    value = summary.get("median", 0.0)
    return float(value or 0.0)


def _source(candidate: Any) -> str | None:
    summary = getattr(candidate, "summary_json", None) or {}
    value = summary.get("source")
    return str(value) if value is not None else None


def _build_reason(primary: Any, backup: Any | None, user_priority: str) -> str:
    primary_name = _label(getattr(primary, "dataset_name"))
    if backup is None:
        return f"当前时间窗内只有 {primary_name} 给出了可用候选，因此先采用它作为主方案。"

    primary_effective = float(getattr(primary, "effective_pixel_ratio_estimate", 0.0) or 0.0)
    backup_effective = float(getattr(backup, "effective_pixel_ratio_estimate", 0.0) or 0.0)
    primary_scene_count = int(getattr(primary, "scene_count", 0) or 0)
    backup_scene_count = int(getattr(backup, "scene_count", 0) or 0)
    primary_resolution = int(getattr(primary, "spatial_resolution", 0) or 0)
    backup_resolution = int(getattr(backup, "spatial_resolution", 0) or 0)
    primary_coverage = float(getattr(primary, "coverage_ratio", 0.0) or 0.0)
    backup_coverage = float(getattr(backup, "coverage_ratio", 0.0) or 0.0)

    if primary_effective >= backup_effective + 0.08:
        return (
            f"当前时间窗内 {primary_name} 的有效像元比例更高、云量风险更低，"
            "因此先把它作为主方案。"
        )
    if user_priority == "spatial" and primary_resolution < backup_resolution:
        return f"当前任务更看重空间细节，因此优先选择 {primary_resolution}m 的 {primary_name}。"
    if user_priority == "temporal" and primary_scene_count >= backup_scene_count + 3:
        return f"当前时间窗内 {primary_name} 的可用景数更多，更适合做时间窗口内的稳妥合成。"
    if primary_coverage >= backup_coverage + 0.1:
        return f"{primary_name} 在当前 AOI 上的覆盖率更稳，因此先作为主推荐。"

    return (
        f"综合覆盖率、有效像元比例和空间分辨率后，{primary_name} 的总分更高，"
        "因此被选为主方案。"
    )


def _build_risk_note(primary: Any, backup: Any | None) -> str | None:
    if _source(primary) == "mock_catalog":
        return "当前候选仍来自 mock catalog，后续需要继续替换为真实 STAC 与像元级质量检查。"

    primary_name = _label(getattr(primary, "dataset_name"))
    primary_cloud = _cloud_median(primary)
    primary_scene_count = int(getattr(primary, "scene_count", 0) or 0)

    if primary_scene_count <= 2:
        return f"{primary_name} 当前可用景数偏少，后续可能需要放宽时间窗或切换备选方案。"
    if primary_cloud >= 40:
        return f"{primary_name} 当前目录级云量统计偏高，进入像元级处理后仍需继续做质量过滤。"
    if backup is not None:
        primary_score = float(getattr(primary, "suitability_score", 0.0) or 0.0)
        backup_score = float(getattr(backup, "suitability_score", 0.0) or 0.0)
        if abs(primary_score - backup_score) <= 0.05:
            return "主备方案评分接近，后续进入真实 NDVI pipeline 后仍可能因为 QA 结果发生切换。"
    return "当前推荐基于目录级候选摘要，实际像元级质量会在后续 NDVI pipeline 中继续校验。"


def build_recommendation(candidates: Iterable[Any], *, user_priority: str) -> dict[str, Any]:
    ordered = sorted(candidates, key=_candidate_sort_key, reverse=True)
    if not ordered:
        return {
            "primary_dataset": "unknown",
            "backup_dataset": None,
            "scores": {},
            "reason": "当前没有可用候选数据源。",
            "risk_note": "需要先完成候选搜索后才能做推荐。",
        }

    primary = ordered[0]
    backup = ordered[1] if len(ordered) > 1 else None
    return {
        "primary_dataset": getattr(primary, "dataset_name"),
        "backup_dataset": getattr(backup, "dataset_name", None) if backup else None,
        "scores": {
            getattr(candidate, "dataset_name"): round(
                float(getattr(candidate, "suitability_score", 0.0) or 0.0), 2
            )
            for candidate in ordered
        },
        "reason": _build_reason(primary, backup, user_priority),
        "risk_note": _build_risk_note(primary, backup),
    }
