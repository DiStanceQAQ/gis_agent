from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


ALLOWED_FALLBACK_STRATEGIES = {
    "real_pipeline_to_baseline": {
        "step_name": "run_processing_pipeline",
        "description": "真实处理链路失败后，降级为 baseline 输出以保障链路可验证。",
    }
}


@dataclass
class QCResult:
    status: str
    issues: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _candidate_cloud_median(candidate: Any) -> float:
    summary = getattr(candidate, "cloud_metric_summary", None) or {}
    value = summary.get("median", 0.0)
    return float(value or 0.0)


def evaluate_candidate_qc(candidates: Iterable[Any]) -> QCResult:
    candidates = list(candidates)
    if not candidates:
        return QCResult(
            status="error",
            issues=[
                {
                    "code": "no_candidates",
                    "severity": "error",
                    "message": "当前时间窗内没有可用候选数据源。",
                }
            ],
        )

    primary = sorted(
        candidates,
        key=lambda item: float(getattr(item, "suitability_score", 0.0) or 0.0),
        reverse=True,
    )[0]
    issues: list[dict[str, object]] = []

    if int(getattr(primary, "scene_count", 0) or 0) <= 2:
        issues.append(
            {
                "code": "low_scene_count",
                "severity": "warning",
                "message": f"{getattr(primary, 'dataset_name', 'unknown')} 当前可用景数偏少。",
            }
        )

    if float(getattr(primary, "effective_pixel_ratio_estimate", 0.0) or 0.0) < 0.5:
        issues.append(
            {
                "code": "low_effective_pixel_ratio",
                "severity": "warning",
                "message": f"{getattr(primary, 'dataset_name', 'unknown')} 的有效像元比例预估偏低。",
            }
        )

    if _candidate_cloud_median(primary) >= 40.0:
        issues.append(
            {
                "code": "high_cloud_cover",
                "severity": "warning",
                "message": f"{getattr(primary, 'dataset_name', 'unknown')} 的目录级云量统计偏高。",
            }
        )

    return QCResult(status="warn" if issues else "ok", issues=issues)


def build_fallback_detail(
    strategy: str,
    *,
    reason: str,
    from_mode: str,
    to_mode: str,
) -> dict[str, object]:
    if strategy not in ALLOWED_FALLBACK_STRATEGIES:
        raise ValueError(f"Unsupported fallback strategy: {strategy}")

    metadata = ALLOWED_FALLBACK_STRATEGIES[strategy]
    return {
        "strategy": strategy,
        "step_name": metadata["step_name"],
        "description": metadata["description"],
        "reason": reason,
        "from_mode": from_mode,
        "to_mode": to_mode,
    }
