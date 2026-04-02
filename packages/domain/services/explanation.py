from __future__ import annotations

from dataclasses import dataclass, field


DATASET_LABELS = {
    "sentinel2": "Sentinel-2",
    "landsat89": "Landsat 8/9",
}

ARTIFACT_LABELS = {
    "png_map": "PNG 地图",
    "geotiff": "GeoTIFF",
    "methods_text": "方法说明",
    "methods_md": "方法说明",
    "summary_md": "结果摘要",
}


@dataclass
class ExplanationContext:
    dataset_name: str
    mode: str
    aoi_input: str | None
    requested_time_range: dict[str, str] | None
    actual_time_range: dict[str, str] | None
    preferred_output: list[str] = field(default_factory=list)
    output_artifact_types: list[str] = field(default_factory=list)
    item_count: int = 0
    valid_pixel_ratio: float | None = None
    ndvi_min: float | None = None
    ndvi_max: float | None = None
    ndvi_mean: float | None = None
    output_width: int | None = None
    output_height: int | None = None
    output_crs: str | None = None
    recommendation_reason: str | None = None
    recommendation_risk_note: str | None = None
    fallback_used: bool = False


def _dataset_label(dataset_name: str) -> str:
    return DATASET_LABELS.get(dataset_name, dataset_name)


def _time_range_label(time_range: dict[str, str] | None) -> str:
    if not time_range:
        return "未记录"
    return f"{time_range['start']} 至 {time_range['end']}"


def _artifact_label_list(artifact_types: list[str], preferred_output: list[str]) -> str:
    ordered = artifact_types or preferred_output
    labels = [ARTIFACT_LABELS.get(item, item) for item in ordered]
    unique = list(dict.fromkeys(labels))
    return "、".join(unique) if unique else "标准结果产物"


def build_methods_text(context: ExplanationContext) -> str:
    dataset_label = _dataset_label(context.dataset_name)
    requested_range = _time_range_label(context.requested_time_range)
    actual_range = _time_range_label(context.actual_time_range or context.requested_time_range)
    artifact_labels = _artifact_label_list(context.output_artifact_types, context.preferred_output)
    aoi_label = context.aoi_input or "未记录 AOI"

    lines = [
        f"本次任务研究区为 {aoi_label}，用户请求时间窗为 {requested_range}，实际执行时间窗为 {actual_range}。",
    ]

    if context.mode == "real":
        detail = (
            f"本次使用 {dataset_label} 的真实 STAC 候选影像执行 NDVI 工作流，"
            f"按 AOI 窗口读取红光/近红外波段，完成基础质量掩膜、逐景 NDVI 计算和中位数合成。"
        )
        if context.item_count > 0:
            detail += f" 实际参与合成的影像数量为 {context.item_count} 景。"
        if context.output_width and context.output_height and context.output_crs:
            detail += (
                f" 输出网格约为 {context.output_width}x{context.output_height}，"
                f"输出 CRS 为 {context.output_crs}。"
            )
        lines.append(detail)
    else:
        lines.append(
            f"本次使用 {dataset_label} 的 baseline 数据链路执行 NDVI 占位流程，"
            "用于验证任务状态、候选推荐、导出和结果展示。"
        )
        if context.output_width and context.output_height and context.output_crs:
            lines.append(
                f"当前 baseline 输出网格约为 {context.output_width}x{context.output_height}，"
                f"输出 CRS 为 {context.output_crs}。"
            )

    lines.append(f"本次导出产物包括：{artifact_labels}。")

    if context.fallback_used:
        lines.append("本次执行发生了 fallback，最终结果应按实际执行参数而不是原始意图理解。")

    return " ".join(lines)


def build_summary_text(context: ExplanationContext) -> str:
    dataset_label = _dataset_label(context.dataset_name)

    if context.mode == "real":
        metrics = []
        if context.item_count > 0:
            metrics.append(f"共使用 {context.item_count} 景影像")
        if context.valid_pixel_ratio is not None:
            metrics.append(f"有效像元占比约 {context.valid_pixel_ratio:.2%}")
        if context.ndvi_mean is not None:
            metrics.append(f"NDVI 均值 {context.ndvi_mean:.3f}")
        if context.ndvi_min is not None and context.ndvi_max is not None:
            metrics.append(f"最小值 {context.ndvi_min:.3f}，最大值 {context.ndvi_max:.3f}")
        summary = f"真实 NDVI 合成已完成，数据源为 {dataset_label}。"
        if metrics:
            summary += " " + "，".join(metrics) + "。"
    else:
        summary = f"当前结果为 {dataset_label} 的 baseline 输出，用于验证工程骨架阶段的任务流、导出和展示。"
        if context.valid_pixel_ratio is not None and context.ndvi_mean is not None:
            summary += (
                f" 当前 baseline 栅格的有效像元占比约 {context.valid_pixel_ratio:.2%}，"
                f"NDVI 均值 {context.ndvi_mean:.3f}。"
            )

    if context.recommendation_reason:
        summary += f" 推荐依据：{context.recommendation_reason}"
    if context.recommendation_risk_note:
        summary += f" 风险提示：{context.recommendation_risk_note}"
    if context.fallback_used:
        summary += " 本次执行发生 fallback，建议结合事件流和方法说明一起解读结果。"

    return summary
