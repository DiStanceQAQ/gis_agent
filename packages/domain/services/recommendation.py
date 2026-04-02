from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.logging import get_logger
from packages.domain.services.llm_client import LLMClient, LLMClientError
from packages.schemas.agent import LLMRecommendation

logger = get_logger(__name__)


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


def _build_forced_reason(primary: Any, backup: Any | None) -> str:
    primary_name = _label(getattr(primary, "dataset_name"))
    if backup is None:
        return f"用户明确指定使用 {primary_name}，当前候选里也只有这一种可执行方案，因此直接按该偏好执行。"

    backup_name = _label(getattr(backup, "dataset_name"))
    return (
        f"用户明确指定使用 {primary_name}，因此本次按该数据源执行；"
        f"{backup_name} 仍保留为备选参考。"
    )


def _build_risk_note(primary: Any, backup: Any | None) -> str | None:
    if _source(primary) == "baseline_catalog":
        return "当前候选仍来自 baseline catalog，后续需要继续替换为真实 STAC 与像元级质量检查。"

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


def _build_forced_risk_note(primary: Any, backup: Any | None) -> str | None:
    base_risk = _build_risk_note(primary, backup)
    if backup is None:
        return base_risk

    primary_score = float(getattr(primary, "suitability_score", 0.0) or 0.0)
    backup_score = float(getattr(backup, "suitability_score", 0.0) or 0.0)
    if backup_score > primary_score + 0.05:
        return (
            f"当前是按用户指定数据源执行；从目录级评分看，{_label(getattr(backup, 'dataset_name'))} "
            "仍可能是更稳妥的备选。"
        )
    return base_risk


def _build_recommendation_legacy(
    candidates: Iterable[Any],
    *,
    user_priority: str,
    requested_dataset: str | None = None,
) -> dict[str, Any]:
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
    if requested_dataset is not None:
        forced_primary = next(
            (candidate for candidate in ordered if getattr(candidate, "dataset_name", None) == requested_dataset),
            None,
        )
        if forced_primary is not None:
            primary = forced_primary
            remaining = [candidate for candidate in ordered if candidate is not forced_primary]
            backup = remaining[0] if remaining else None
            return {
                "primary_dataset": getattr(primary, "dataset_name"),
                "backup_dataset": getattr(backup, "dataset_name", None) if backup else None,
                "scores": {
                    getattr(candidate, "dataset_name"): round(
                        float(getattr(candidate, "suitability_score", 0.0) or 0.0), 2
                    )
                    for candidate in ordered
                },
                "reason": _build_forced_reason(primary, backup),
                "risk_note": _build_forced_risk_note(primary, backup),
            }

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


def _build_recommendation_failure(*, reason: str) -> dict[str, Any]:
    error_code = (
        ErrorCode.TASK_LLM_RECOMMENDATION_SCHEMA_VALIDATION_FAILED
        if reason.startswith("schema_validation_failed")
        else ErrorCode.TASK_LLM_RECOMMENDATION_FAILED
    )
    error_message = (
        "Recommendation LLM 输出未通过 schema 校验。"
        if reason.startswith("schema_validation_failed")
        else f"Recommendation LLM 处理失败（{reason[:120]}）。"
    )
    return {
        "primary_dataset": "unknown",
        "backup_dataset": None,
        "scores": {},
        "reason": f"推荐阶段失败（{reason[:120]}），请重试或切换到规则推荐。",
        "risk_note": "当前无法生成可靠推荐结果。",
        "confidence": None,
        "error_code": error_code,
        "error_message": error_message,
    }


def _candidate_to_summary(candidate: Any) -> dict[str, Any]:
    return {
        "dataset_name": getattr(candidate, "dataset_name", None),
        "collection_id": getattr(candidate, "collection_id", None),
        "scene_count": int(getattr(candidate, "scene_count", 0) or 0),
        "coverage_ratio": float(getattr(candidate, "coverage_ratio", 0.0) or 0.0),
        "effective_pixel_ratio_estimate": float(
            getattr(candidate, "effective_pixel_ratio_estimate", 0.0) or 0.0
        ),
        "cloud_metric_summary": getattr(candidate, "cloud_metric_summary", None),
        "spatial_resolution": int(getattr(candidate, "spatial_resolution", 0) or 0),
        "temporal_density_note": getattr(candidate, "temporal_density_note", None),
        "suitability_score": float(getattr(candidate, "suitability_score", 0.0) or 0.0),
        "recommendation_rank": int(getattr(candidate, "recommendation_rank", 0) or 0),
        "summary_json": getattr(candidate, "summary_json", None),
    }


@lru_cache
def _load_recommendation_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "recommendation.md"
    return prompt_path.read_text(encoding="utf-8")


def _build_recommendation_user_prompt(
    candidates: list[Any],
    *,
    user_priority: str,
    requested_dataset: str | None,
) -> str:
    payload = {
        "task": "recommend_dataset",
        "language": "zh-CN",
        "user_priority": user_priority,
        "requested_dataset": requested_dataset,
        "candidate_summaries": [_candidate_to_summary(candidate) for candidate in candidates],
        "required_fields": [
            "primary_dataset",
            "backup_dataset",
            "scores",
            "reason",
            "risk_note",
            "confidence",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_recommendation_repair_user_prompt(
    *,
    base_prompt: str,
    previous_json: dict[str, Any],
    validation_errors: list[dict[str, Any]],
) -> str:
    payload = {
        "task": "repair_invalid_json_output",
        "instructions": "上一次 JSON 未通过后端 schema 校验。请仅返回修复后的 JSON，不要输出解释。",
        "original_prompt": base_prompt,
        "previous_json": previous_json,
        "validation_errors": validation_errors,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _pick_backup_from_ordered(
    ordered: list[Any],
    *,
    primary_dataset: str,
) -> str | None:
    for candidate in ordered:
        dataset_name = str(getattr(candidate, "dataset_name", ""))
        if dataset_name and dataset_name != primary_dataset:
            return dataset_name
    return None


def _normalize_llm_recommendation(
    payload: LLMRecommendation,
    *,
    candidates: list[Any],
    user_priority: str,
    requested_dataset: str | None,
) -> dict[str, Any]:
    legacy = _build_recommendation_legacy(
        candidates,
        user_priority=user_priority,
        requested_dataset=requested_dataset,
    )
    if not candidates:
        return legacy

    ordered = sorted(candidates, key=_candidate_sort_key, reverse=True)
    candidate_names = [str(getattr(candidate, "dataset_name")) for candidate in ordered]

    primary_dataset = payload.primary_dataset
    if primary_dataset not in candidate_names:
        raise ValueError(f"primary_dataset {primary_dataset!r} is not in candidate list")

    backup_dataset = payload.backup_dataset
    if backup_dataset is not None and backup_dataset not in candidate_names:
        raise ValueError(f"backup_dataset {backup_dataset!r} is not in candidate list")

    if requested_dataset is not None and requested_dataset in candidate_names:
        primary_dataset = requested_dataset
        if backup_dataset == primary_dataset:
            backup_dataset = None

    if backup_dataset == primary_dataset:
        backup_dataset = None
    if backup_dataset is None and len(candidate_names) > 1:
        backup_dataset = _pick_backup_from_ordered(ordered, primary_dataset=primary_dataset)

    scores = dict(legacy.get("scores", {}))
    for dataset_name, value in payload.scores.items():
        if dataset_name not in candidate_names:
            continue
        try:
            scores[dataset_name] = round(float(value), 2)
        except (TypeError, ValueError):
            continue

    reason = (payload.reason or "").strip() or str(legacy.get("reason") or "")
    risk_note = payload.risk_note
    if risk_note is not None:
        risk_note = risk_note.strip()
    if not risk_note:
        risk_note = legacy.get("risk_note")

    return {
        "primary_dataset": primary_dataset,
        "backup_dataset": backup_dataset,
        "scores": scores,
        "reason": reason,
        "risk_note": risk_note,
        "confidence": payload.confidence,
    }


def _build_recommendation_with_llm(
    candidates: list[Any],
    *,
    user_priority: str,
    requested_dataset: str | None,
) -> dict[str, Any]:
    settings = get_settings()
    recommendation_retries = max(0, settings.llm_recommendation_schema_retries)
    client = LLMClient(settings)
    system_prompt = _load_recommendation_system_prompt()
    base_user_prompt = _build_recommendation_user_prompt(
        candidates,
        user_priority=user_priority,
        requested_dataset=requested_dataset,
    )
    user_prompt = base_user_prompt
    attempt = 0
    last_error = "unknown"

    while attempt <= recommendation_retries:
        response = client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
        )
        try:
            payload = LLMRecommendation.model_validate(response.content_json)
            return _normalize_llm_recommendation(
                payload,
                candidates=candidates,
                user_priority=user_priority,
                requested_dataset=requested_dataset,
            )
        except (ValidationError, ValueError) as exc:
            last_error = "schema_validation_failed"
            if attempt >= recommendation_retries:
                break
            attempt += 1
            validation_errors = exc.errors() if isinstance(exc, ValidationError) else [{"msg": str(exc)}]
            user_prompt = _build_recommendation_repair_user_prompt(
                base_prompt=base_user_prompt,
                previous_json=response.content_json,
                validation_errors=validation_errors,
            )

    return _build_recommendation_failure(reason=last_error)


def build_recommendation(
    candidates: Iterable[Any],
    *,
    user_priority: str,
    requested_dataset: str | None = None,
) -> dict[str, Any]:
    candidate_list = list(candidates)
    settings = get_settings()

    if not settings.llm_recommendation_enabled:
        return _build_recommendation_legacy(
            candidate_list,
            user_priority=user_priority,
            requested_dataset=requested_dataset,
        )

    if not candidate_list:
        return _build_recommendation_legacy(
            candidate_list,
            user_priority=user_priority,
            requested_dataset=requested_dataset,
        )

    try:
        return _build_recommendation_with_llm(
            candidate_list,
            user_priority=user_priority,
            requested_dataset=requested_dataset,
        )
    except LLMClientError as exc:
        logger.warning("recommendation.llm_client_failed detail=%s", exc.detail)
        if settings.llm_recommendation_legacy_fallback:
            return _build_recommendation_legacy(
                candidate_list,
                user_priority=user_priority,
                requested_dataset=requested_dataset,
            )
        return _build_recommendation_failure(reason="llm_client_error")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("recommendation.llm_unexpected_error")
        if settings.llm_recommendation_legacy_fallback:
            return _build_recommendation_legacy(
                candidate_list,
                user_priority=user_priority,
                requested_dataset=requested_dataset,
            )
        return _build_recommendation_failure(reason=repr(exc))
