from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_bounds

from packages.domain.database import SessionLocal
from packages.domain.errors import ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import (
    ArtifactRecord,
    TaskRunRecord,
    TaskStepRecord,
)
from packages.domain.services.catalog import persist_candidates, search_candidates
from packages.domain.services.ndvi_pipeline import run_real_ndvi_pipeline
from packages.domain.services.recommendation import build_recommendation
from packages.domain.config import get_settings
from packages.domain.services.storage import build_artifact_path, file_metadata
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)


STEP_SEQUENCE = [
    "normalize_aoi",
    "search_candidates",
    "recommend_dataset",
    "run_ndvi_pipeline",
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


def _require_task_bbox(task: TaskRunRecord) -> list[float]:
    if task.aoi and task.aoi.bbox_bounds_json:
        return [float(value) for value in task.aoi.bbox_bounds_json]
    raise ValueError("Task AOI was not normalized; refusing to run pipeline with a mock bbox.")


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


def _build_mock_pipeline_outputs(task: TaskRunRecord, bbox: list[float]) -> dict[str, object]:
    tif_path, png_path = _write_mock_raster(task.id, bbox)
    methods_text = (
        f"本次任务使用 {task.selected_dataset} mock 数据执行 NDVI 流程，占位展示了中位数合成、"
        f"结果导出和方法说明链路。研究区为 {task.task_spec.aoi_input if task.task_spec else 'unknown'}，"
        f"时间范围为 {task.requested_time_range}。"
    )
    summary_text = (
        "当前结果为工程骨架阶段的 mock 输出，用于验证对话、状态流转、下载和结果页展示。"
    )
    return {
        "tif_path": tif_path,
        "png_path": png_path,
        "methods_text": methods_text,
        "summary_text": summary_text,
        "actual_time_range": task.requested_time_range,
        "mode": "mock",
        "selected_item_ids": [],
        "valid_pixel_ratio": None,
    }


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
        logger.info("mock_pipeline.started task_id=%s", task.id)

        bbox = _require_task_bbox(task)
        _set_step_status(task, "normalize_aoi", "running")
        _set_step_status(task, "normalize_aoi", "success", {"bbox": bbox})

        _set_step_status(task, "search_candidates", "running")
        parsed_spec = ParsedTaskSpec(**(task.task_spec.raw_spec_json if task.task_spec else {}))
        candidates = search_candidates(parsed_spec, task.aoi)
        persist_candidates(db, task=task, candidates=candidates)
        db.commit()
        _set_step_status(
            task,
            "search_candidates",
            "success",
            {
                "candidate_count": len(candidates),
                "collections": [candidate.collection_id for candidate in candidates],
            },
        )

        _set_step_status(task, "recommend_dataset", "running")
        db.refresh(task)
        recommendation = build_recommendation(candidates, user_priority=parsed_spec.user_priority)
        task.selected_dataset = recommendation["primary_dataset"]
        task.recommendation_json = recommendation
        db.commit()
        _set_step_status(task, "recommend_dataset", "success", task.recommendation_json)

        _set_step_status(task, "run_ndvi_pipeline", "running")
        settings = get_settings()
        try:
            if settings.real_pipeline_enabled and task.aoi is not None:
                real_result = run_real_ndvi_pipeline(
                    task_id=task.id,
                    dataset_name=task.selected_dataset,
                    spec=parsed_spec,
                    aoi=task.aoi,
                )
                pipeline_outputs: dict[str, object] = {
                    "tif_path": real_result.tif_path,
                    "png_path": real_result.png_path,
                    "methods_text": real_result.methods_text,
                    "summary_text": real_result.summary_text,
                    "actual_time_range": real_result.actual_time_range,
                    "mode": real_result.mode,
                    "selected_item_ids": real_result.selected_item_ids,
                    "valid_pixel_ratio": real_result.valid_pixel_ratio,
                }
            else:
                pipeline_outputs = _build_mock_pipeline_outputs(task, bbox)
        except Exception as exc:
            logger.warning("ndvi_pipeline.fallback_to_mock task_id=%s reason=%r", task.id, exc)
            task.fallback_used = True
            db.commit()
            pipeline_outputs = _build_mock_pipeline_outputs(task, bbox)
        _set_step_status(
            task,
            "run_ndvi_pipeline",
            "success",
            {
                "mode": pipeline_outputs["mode"],
                "selected_item_ids": pipeline_outputs["selected_item_ids"],
                "valid_pixel_ratio": pipeline_outputs["valid_pixel_ratio"],
            },
        )

        _set_step_status(task, "generate_outputs", "running")
        tif_path = str(pipeline_outputs["tif_path"])
        png_path = str(pipeline_outputs["png_path"])
        methods_text = str(pipeline_outputs["methods_text"])
        summary_text = str(pipeline_outputs["summary_text"])
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
                    metadata_json={"mode": pipeline_outputs["mode"]},
                )
            )
        task.methods_text = methods_text
        task.result_summary_text = summary_text
        task.actual_time_range = pipeline_outputs["actual_time_range"]
        task.current_step = "generate_outputs"
        task.status = "success"
        task.duration_seconds = int(perf_counter() - start)
        db.commit()
        _set_step_status(task, "generate_outputs", "success")
        logger.info(
            "mock_pipeline.succeeded task_id=%s dataset=%s duration_seconds=%s",
            task.id,
            task.selected_dataset,
            task.duration_seconds,
        )
    except Exception as exc:  # pragma: no cover - defensive scaffold handling
        task = db.get(TaskRunRecord, task_id)
        if task is not None:
            task.status = "failed"
            task.error_code = ErrorCode.MOCK_PIPELINE_FAILED
            task.error_message = str(exc)
            db.commit()
        logger.exception("mock_pipeline.failed task_id=%s", task_id)
    finally:
        db.close()
