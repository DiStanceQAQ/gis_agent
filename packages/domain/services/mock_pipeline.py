from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_bounds

from packages.domain.database import SessionLocal
from packages.domain.models import (
    AOIRecord,
    ArtifactRecord,
    DatasetCandidateRecord,
    TaskRunRecord,
    TaskStepRecord,
)
from packages.domain.services.storage import build_artifact_path, file_metadata
from packages.domain.utils import make_id


STEP_SEQUENCE = [
    "normalize_aoi",
    "search_candidates",
    "recommend_dataset",
    "run_mock_workflow",
    "generate_outputs",
]


def _ensure_steps(db, task: TaskRunRecord) -> None:
    if task.steps:
        return
    for step_name in STEP_SEQUENCE:
        db.add(TaskStepRecord(task_id=task.id, step_name=step_name, status="pending"))
    db.commit()
    db.refresh(task)


def _set_step_status(task: TaskRunRecord, step_name: str, status: str, detail: dict | None = None) -> None:
    db = SessionLocal()
    try:
        task_ref = db.get(TaskRunRecord, task.id)
        if task_ref is None:
            return
        for step in task_ref.steps:
            if step.step_name == step_name:
                if status == "running" and step.started_at is None:
                    step.started_at = datetime.now(timezone.utc)
                if status in {"success", "failed", "skipped"}:
                    step.ended_at = datetime.now(timezone.utc)
                step.status = status
                step.detail_json = detail
                break
        task_ref.current_step = step_name
        db.commit()
    finally:
        db.close()


def _mock_bbox(task: TaskRunRecord) -> list[float]:
    aoi_name = (task.task_spec.aoi_input or "").lower() if task.task_spec else ""
    if "北京" in aoi_name:
        return [115.72, 39.86, 116.18, 40.06]
    return [100.0, 30.0, 100.6, 30.4]


def _write_mock_raster(task_id: str, bbox: list[float]) -> tuple[str, str]:
    width = 96
    height = 96
    xv = np.linspace(-1.0, 1.0, width)
    yv = np.linspace(-1.0, 1.0, height)
    xx, yy = np.meshgrid(xv, yv)
    data = np.clip(0.55 + 0.25 * np.sin(xx * 3) - 0.18 * np.cos(yy * 4), -1.0, 1.0).astype("float32")

    tif_path = build_artifact_path(task_id, "ndvi_mock.tif")
    transform = from_bounds(*bbox, width=width, height=height)
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999.0,
    ) as dataset:
        dataset.write(data, 1)

    normalized = ((data + 1.0) / 2.0 * 255.0).astype("uint8")
    rgb = np.zeros((height, width, 3), dtype="uint8")
    rgb[..., 1] = normalized
    rgb[..., 0] = (255 - normalized) // 3
    rgb[..., 2] = 60
    png_path = build_artifact_path(task_id, "map_mock.png")
    Image.fromarray(rgb, mode="RGB").save(png_path)
    return tif_path, png_path


def run_mock_task(task_id: str) -> None:
    db = SessionLocal()
    start = perf_counter()
    try:
        task = db.get(TaskRunRecord, task_id)
        if task is None:
            return
        _ensure_steps(db, task)
        task.status = "running"
        task.current_step = "normalize_aoi"
        db.commit()

        bbox = _mock_bbox(task)
        _set_step_status(task, "normalize_aoi", "running")
        if task.aoi is None:
            db.add(
                AOIRecord(
                    id=make_id("aoi"),
                    task_id=task.id,
                    name=task.task_spec.aoi_input if task.task_spec else "mock_aoi",
                    geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
                    bbox_json=bbox,
                    area_km2=326.5,
                    is_valid=True,
                    validation_message="mock geometry",
                )
            )
            db.commit()
        _set_step_status(task, "normalize_aoi", "success", {"bbox": bbox})

        _set_step_status(task, "search_candidates", "running")
        for candidate in list(task.candidates):
            db.delete(candidate)
        db.commit()
        db.add_all(
            [
                DatasetCandidateRecord(
                    task_id=task.id,
                    dataset_name="sentinel2",
                    collection_id="sentinel-2-l2a",
                    scene_count=18,
                    coverage_ratio=0.98,
                    effective_pixel_ratio_estimate=0.81,
                    cloud_metric_summary={"median": 0.12, "p75": 0.20},
                    spatial_resolution=10,
                    temporal_density_note="high",
                    suitability_score=0.87 if task.task_spec.user_priority != "temporal" else 0.72,
                    recommendation_rank=1 if task.task_spec.user_priority != "temporal" else 2,
                    summary_json={"note": "mock candidate"},
                ),
                DatasetCandidateRecord(
                    task_id=task.id,
                    dataset_name="landsat89",
                    collection_id="landsat-c2-l2",
                    scene_count=11,
                    coverage_ratio=0.96,
                    effective_pixel_ratio_estimate=0.78,
                    cloud_metric_summary={"median": 0.16, "p75": 0.24},
                    spatial_resolution=30,
                    temporal_density_note="medium",
                    suitability_score=0.74 if task.task_spec.user_priority != "temporal" else 0.86,
                    recommendation_rank=2 if task.task_spec.user_priority != "temporal" else 1,
                    summary_json={"note": "mock candidate"},
                ),
            ]
        )
        db.commit()
        _set_step_status(task, "search_candidates", "success")

        _set_step_status(task, "recommend_dataset", "running")
        db.refresh(task)
        ordered_candidates = sorted(task.candidates, key=lambda item: item.recommendation_rank)
        primary = ordered_candidates[0]
        backup = ordered_candidates[1] if len(ordered_candidates) > 1 else None
        task.selected_dataset = primary.dataset_name
        task.recommendation_json = {
            "primary_dataset": primary.dataset_name,
            "backup_dataset": backup.dataset_name if backup else None,
            "reason": (
                "当前任务更偏向空间细节，因此优先使用 Sentinel-2。"
                if primary.dataset_name == "sentinel2"
                else "当前任务更偏向稳妥的时间覆盖，因此优先使用 Landsat 8/9。"
            ),
            "risk_note": "当前为 mock 执行结果，后续需替换为真实数据搜索与评分。",
        }
        db.commit()
        _set_step_status(task, "recommend_dataset", "success", task.recommendation_json)

        _set_step_status(task, "run_mock_workflow", "running")
        tif_path, png_path = _write_mock_raster(task.id, bbox)
        _set_step_status(task, "run_mock_workflow", "success")

        _set_step_status(task, "generate_outputs", "running")
        methods_text = (
            f"本次任务使用 {task.selected_dataset} mock 数据执行 NDVI 流程，占位展示了中位数合成、"
            f"结果导出和方法说明链路。研究区为 {task.task_spec.aoi_input if task.task_spec else 'unknown'}，"
            f"时间范围为 {task.requested_time_range}。"
        )
        summary_text = (
            "当前结果为工程骨架阶段的 mock 输出，用于验证对话、状态流转、下载和结果页展示。"
        )
        methods_path = build_artifact_path(task.id, "methods.md")
        summary_path = build_artifact_path(task.id, "summary.md")
        Path(methods_path).write_text(methods_text, encoding="utf-8")
        Path(summary_path).write_text(summary_text, encoding="utf-8")

        artifact_specs = [
            ("png_map", png_path, "image/png"),
            ("geotiff", tif_path, "image/tiff"),
            ("methods_md", methods_path, "text/markdown"),
            ("summary_md", summary_path, "text/markdown"),
        ]
        for artifact in list(task.artifacts):
            db.delete(artifact)
        db.commit()
        for artifact_type, path, mime in artifact_specs:
            size_bytes, checksum = file_metadata(path)
            db.add(
                ArtifactRecord(
                    id=make_id("art"),
                    task_id=task.id,
                    artifact_type=artifact_type,
                    storage_key=path,
                    mime_type=mime,
                    size_bytes=size_bytes,
                    checksum=checksum,
                    metadata_json={"mock": True},
                )
            )
        task.methods_text = methods_text
        task.result_summary_text = summary_text
        task.actual_time_range = task.requested_time_range
        task.current_step = "generate_outputs"
        task.status = "success"
        task.duration_seconds = int(perf_counter() - start)
        db.commit()
        _set_step_status(task, "generate_outputs", "success")
    except Exception as exc:  # pragma: no cover - defensive scaffold handling
        task = db.get(TaskRunRecord, task_id)
        if task is not None:
            task.status = "failed"
            task.error_code = "mock_pipeline_failed"
            task.error_message = str(exc)
            db.commit()
    finally:
        db.close()

