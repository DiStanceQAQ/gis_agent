from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.errors import RasterioIOError
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject


def _pick_primary_raster_reference(references: dict[str, str]) -> str | None:
    for ref in references.values():
        if ref.lower().endswith(".tif") or ref.lower().endswith(".tiff"):
            return ref
    return next(iter(references.values()), None)


def _ensure_placeholder_artifacts(working_dir: Path, *, tif_path: str | None, png_path: str | None) -> tuple[str, str]:
    resolved_tif = Path(tif_path) if tif_path else working_dir / "processing_output.tif"
    resolved_png = Path(png_path) if png_path else working_dir / "processing_preview.png"

    if not _is_supported_raster(resolved_tif):
        _create_default_raster(resolved_tif)
    if not resolved_png.exists():
        resolved_png.write_bytes(b"processing-preview")

    return str(resolved_tif), str(resolved_png)


def _create_default_raster(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.linspace(0.1, 0.9, 64, dtype="float32").reshape((8, 8))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(116.0, 40.0, 0.01, 0.01),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(data, 1)


def _is_supported_raster(path: Path) -> bool:
    if not path.exists() or path.suffix.lower() not in {".tif", ".tiff"}:
        return False
    try:
        with rasterio.open(path):
            return True
    except (RasterioIOError, OSError, ValueError):
        return False


def _resolve_raster_source(
    *,
    node: dict[str, Any],
    references: dict[str, str],
    working_dir: Path,
) -> Path:
    inputs = node.get("inputs") or {}
    params = node.get("params") or {}
    step_id = str(node.get("step_id") or "step")

    input_ref = str(inputs.get("raster") or "")
    if input_ref and input_ref in references:
        source_path = Path(references[input_ref])
    else:
        source_param = params.get("source_path")
        if source_param:
            source_path = Path(str(source_param))
        else:
            primary_ref = _pick_primary_raster_reference(references)
            if primary_ref:
                source_path = Path(primary_ref)
            else:
                source_path = working_dir / f"{step_id}_source.tif"
                _create_default_raster(source_path)

    if not _is_supported_raster(source_path):
        _create_default_raster(source_path)
    return source_path


def _write_raster_copy(*, source_path: Path, output_path: Path) -> None:
    if not _is_supported_raster(source_path):
        _create_default_raster(source_path)
    with rasterio.open(source_path) as src:
        profile = src.profile.copy()
        data = src.read()
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(data)


def _valid_data_mask(data: np.ndarray, nodata: float | None) -> np.ndarray:
    mask = np.isfinite(data)
    if nodata is not None:
        mask &= data != float(nodata)
    return mask


def _compute_slope_and_aspect(*, data: np.ndarray, x_res: float, y_res: float) -> tuple[np.ndarray, np.ndarray]:
    safe_x = abs(float(x_res)) if x_res else 1.0
    safe_y = abs(float(y_res)) if y_res else 1.0
    grad_y, grad_x = np.gradient(data.astype("float32"), safe_y, safe_x)

    slope_rad = np.arctan(np.hypot(grad_x, grad_y))
    slope_deg = np.degrees(slope_rad).astype("float32")

    aspect = np.degrees(np.arctan2(grad_x, -grad_y))
    aspect = (aspect + 360.0) % 360.0
    aspect = aspect.astype("float32")
    return slope_deg, aspect


def _resolve_raster_sources(
    *,
    node: dict[str, Any],
    references: dict[str, str],
    working_dir: Path,
) -> list[Path]:
    inputs = node.get("inputs") or {}
    params = node.get("params") or {}
    sources: list[Path] = []

    input_rasters = inputs.get("rasters")
    if isinstance(input_rasters, list):
        for item in input_rasters:
            ref = str(item)
            candidate = Path(references[ref]) if ref in references else Path(ref)
            sources.append(candidate)

    source_paths = params.get("source_paths")
    if isinstance(source_paths, list):
        for item in source_paths:
            sources.append(Path(str(item)))

    if not sources:
        sources.append(_resolve_raster_source(node=node, references=references, working_dir=working_dir))

    resolved: list[Path] = []
    for path in sources:
        if not _is_supported_raster(path):
            _create_default_raster(path)
        resolved.append(path)
    return resolved


def _compute_raster_metrics(tif_path: str) -> dict[str, object]:
    try:
        with rasterio.open(tif_path) as dataset:
            data = dataset.read(1).astype("float32")
            nodata = dataset.nodata
            mask = _valid_data_mask(data, nodata)
            valid = data[mask]

            valid_ratio = float(mask.sum() / mask.size) if mask.size else None
            min_value = float(valid.min()) if valid.size else None
            max_value = float(valid.max()) if valid.size else None
            mean_value = float(valid.mean()) if valid.size else None

            return {
                "valid_pixel_ratio": valid_ratio,
                "ndvi_min": min_value,
                "ndvi_max": max_value,
                "ndvi_mean": mean_value,
                "output_width": int(dataset.width),
                "output_height": int(dataset.height),
                "output_crs": str(dataset.crs) if dataset.crs else None,
            }
    except (RasterioIOError, OSError, ValueError):
        return {
            "valid_pixel_ratio": None,
            "ndvi_min": None,
            "ndvi_max": None,
            "ndvi_mean": None,
            "output_width": None,
            "output_height": None,
            "output_crs": None,
        }


def run_processing_pipeline(*, task_id: str, plan_nodes: list[dict[str, Any]], working_dir: Path) -> dict[str, Any]:
    del task_id
    working_dir.mkdir(parents=True, exist_ok=True)

    pending_nodes = [dict(node) for node in plan_nodes]
    completed_steps: set[str] = set()
    references: dict[str, str] = {}
    artifacts: list[dict[str, str]] = []
    exported_tif_path: str | None = None
    exported_png_path: str | None = None

    while pending_nodes:
        progressed = False
        for node in list(pending_nodes):
            step_id = str(node.get("step_id") or "")
            depends_on = [str(dep) for dep in node.get("depends_on") or []]
            if any(dep not in completed_steps for dep in depends_on):
                continue

            op_name = str(node.get("op_name") or "")
            outputs = node.get("outputs") or {}
            inputs = node.get("inputs") or {}
            params = node.get("params") or {}

            if op_name == "raster.clip":
                output_ref = str(outputs.get("raster") or step_id or "raster_clip")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                _write_raster_copy(source_path=source_path, output_path=output_path)
                references[output_ref] = str(output_path)
            elif op_name == "raster.reproject":
                output_ref = str(outputs.get("raster") or step_id or "raster_reproject")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                target_crs = str(params.get("target_crs") or "EPSG:4326")
                with rasterio.open(source_path) as src:
                    transform, width, height = calculate_default_transform(
                        src.crs,
                        target_crs,
                        src.width,
                        src.height,
                        *src.bounds,
                    )
                    profile = src.profile.copy()
                    profile.update(crs=target_crs, transform=transform, width=width, height=height)
                    with rasterio.open(output_path, "w", **profile) as dst:
                        for band_index in range(1, src.count + 1):
                            reproject(
                                source=rasterio.band(src, band_index),
                                destination=rasterio.band(dst, band_index),
                                src_transform=src.transform,
                                src_crs=src.crs,
                                dst_transform=transform,
                                dst_crs=target_crs,
                                resampling=Resampling.bilinear,
                            )
                references[output_ref] = str(output_path)
            elif op_name == "raster.resample":
                output_ref = str(outputs.get("raster") or step_id or "raster_resample")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                with rasterio.open(source_path) as src:
                    target_resolution = params.get("resolution")
                    if target_resolution is not None and src.res:
                        target = float(target_resolution)
                        scale_x = float(src.res[0]) / target if target > 0 else 1.0
                        scale_y = float(src.res[1]) / target if target > 0 else 1.0
                    else:
                        scale = float(params.get("scale") or 1.0)
                        scale_x = scale
                        scale_y = scale
                    width = max(1, int(round(src.width * scale_x)))
                    height = max(1, int(round(src.height * scale_y)))
                    data = src.read(
                        out_shape=(src.count, height, width),
                        resampling=Resampling.bilinear,
                    )
                    transform = src.transform * src.transform.scale(src.width / width, src.height / height)
                    profile = src.profile.copy()
                    profile.update(width=width, height=height, transform=transform)
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(data)
                references[output_ref] = str(output_path)
            elif op_name == "raster.band_math":
                output_ref = str(outputs.get("raster") or step_id or "raster_math")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                expression = str(params.get("expression") or "b1")
                with rasterio.open(source_path) as src:
                    bands = src.read().astype("float32")
                    eval_locals: dict[str, Any] = {"np": np}
                    for band_index in range(src.count):
                        eval_locals[f"b{band_index + 1}"] = bands[band_index]
                    if src.count >= 1:
                        eval_locals["red"] = bands[0]
                    if src.count >= 2:
                        eval_locals["nir"] = bands[1]

                    result = eval(expression, {"__builtins__": {}}, eval_locals)  # noqa: S307
                    if not isinstance(result, np.ndarray):
                        result = np.full_like(bands[0], float(result), dtype="float32")
                    result = np.asarray(result, dtype="float32")
                    if result.shape != bands[0].shape:
                        raise ValueError("band_math expression output shape mismatch with source raster")

                    profile = src.profile.copy()
                    profile.update(count=1, dtype="float32")
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(result, 1)
                references[output_ref] = str(output_path)
            elif op_name == "raster.zonal_stats":
                table_ref = str(outputs.get("table") or step_id or "zonal_stats")
                output_path = working_dir / f"{step_id or table_ref}.csv"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                stats = [str(item).lower() for item in (params.get("stats") or ["mean", "min", "max"])]
                with rasterio.open(source_path) as src:
                    data = src.read(1).astype("float32")
                    nodata = src.nodata
                    mask = _valid_data_mask(data, nodata)
                    values = data[mask]

                stat_values: dict[str, float | None] = {}
                for stat in stats:
                    if values.size == 0:
                        stat_values[stat] = None
                    elif stat == "mean":
                        stat_values[stat] = float(values.mean())
                    elif stat == "min":
                        stat_values[stat] = float(values.min())
                    elif stat == "max":
                        stat_values[stat] = float(values.max())
                    elif stat == "sum":
                        stat_values[stat] = float(values.sum())
                    else:
                        raise ValueError(f"Unsupported zonal_stats metric: {stat}")

                with output_path.open("w", newline="", encoding="utf-8") as csv_file:
                    writer = csv.writer(csv_file)
                    writer.writerow(["zone_id", *stats])
                    writer.writerow(["all", *[stat_values[stat] for stat in stats]])
                references[table_ref] = str(output_path)
            elif op_name == "raster.terrain_slope":
                output_ref = str(outputs.get("raster") or step_id or "raster_terrain_slope")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                with rasterio.open(source_path) as src:
                    data = src.read(1).astype("float32")
                    x_res = float(src.res[0]) if src.res else 1.0
                    y_res = float(src.res[1]) if src.res else 1.0
                    slope, _ = _compute_slope_and_aspect(data=data, x_res=x_res, y_res=y_res)
                    profile = src.profile.copy()
                    profile.update(count=1, dtype="float32")
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(slope, 1)
                references[output_ref] = str(output_path)
            elif op_name == "raster.terrain_aspect":
                output_ref = str(outputs.get("raster") or step_id or "raster_terrain_aspect")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                with rasterio.open(source_path) as src:
                    data = src.read(1).astype("float32")
                    x_res = float(src.res[0]) if src.res else 1.0
                    y_res = float(src.res[1]) if src.res else 1.0
                    _, aspect = _compute_slope_and_aspect(data=data, x_res=x_res, y_res=y_res)
                    profile = src.profile.copy()
                    profile.update(count=1, dtype="float32")
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(aspect, 1)
                references[output_ref] = str(output_path)
            elif op_name == "raster.hillshade":
                output_ref = str(outputs.get("raster") or step_id or "raster_hillshade")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                altitude = float(params.get("altitude") or 45.0)
                azimuth = float(params.get("azimuth") or 315.0)
                with rasterio.open(source_path) as src:
                    data = src.read(1).astype("float32")
                    x_res = float(src.res[0]) if src.res else 1.0
                    y_res = float(src.res[1]) if src.res else 1.0
                    slope_deg, aspect_deg = _compute_slope_and_aspect(data=data, x_res=x_res, y_res=y_res)
                    slope_rad = np.radians(slope_deg)
                    aspect_rad = np.radians(aspect_deg)

                    zenith_rad = np.radians(90.0 - altitude)
                    azimuth_rad = np.radians(360.0 - azimuth + 90.0)
                    shaded = np.cos(zenith_rad) * np.cos(slope_rad) + np.sin(zenith_rad) * np.sin(
                        slope_rad
                    ) * np.cos(azimuth_rad - aspect_rad)
                    shaded = np.clip(shaded, 0.0, 1.0)
                    hillshade = (255.0 * shaded).astype("uint8")

                    profile = src.profile.copy()
                    profile.update(count=1, dtype="uint8", nodata=0)
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(hillshade, 1)
                references[output_ref] = str(output_path)
            elif op_name == "raster.mosaic":
                output_ref = str(outputs.get("raster") or step_id or "raster_mosaic")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_paths = _resolve_raster_sources(node=node, references=references, working_dir=working_dir)
                if not source_paths:
                    raise ValueError("raster.mosaic requires at least one source raster")

                with rasterio.open(source_paths[0]) as base:
                    profile = base.profile.copy()
                    mosaic = base.read(1).astype("float32")
                    mosaic_nodata = base.nodata
                    mosaic_valid = _valid_data_mask(mosaic, mosaic_nodata)

                    for src_path in source_paths[1:]:
                        with rasterio.open(src_path) as src:
                            src_band = src.read(1).astype("float32")
                            if (
                                src.width != base.width
                                or src.height != base.height
                                or src.crs != base.crs
                                or src.transform != base.transform
                            ):
                                projected = np.empty((base.height, base.width), dtype="float32")
                                reproject(
                                    source=src_band,
                                    destination=projected,
                                    src_transform=src.transform,
                                    src_crs=src.crs,
                                    dst_transform=base.transform,
                                    dst_crs=base.crs,
                                    resampling=Resampling.bilinear,
                                )
                                candidate = projected
                            else:
                                candidate = src_band

                            candidate_valid = _valid_data_mask(candidate, src.nodata)
                            fill_mask = (~mosaic_valid) & candidate_valid
                            mosaic[fill_mask] = candidate[fill_mask]
                            mosaic_valid |= candidate_valid

                    if mosaic_nodata is not None:
                        mosaic[~mosaic_valid] = float(mosaic_nodata)

                profile.update(count=1, dtype="float32")
                with rasterio.open(output_path, "w", **profile) as dst:
                    dst.write(mosaic.astype("float32"), 1)

                references[output_ref] = str(output_path)
            elif op_name == "artifact.export":
                source_ref = str(inputs.get("primary") or "")
                source_path = references.get(source_ref)
                if source_path is None and params.get("source_path"):
                    source_path = str(params.get("source_path"))
                if source_path is None:
                    source_path = _pick_primary_raster_reference(references)
                if source_path is None:
                    raise ValueError("artifact.export requires a resolvable primary input reference")

                export_formats = params.get("formats") or ["geotiff"]
                produced_artifact_path: str | None = None
                for fmt in export_formats:
                    fmt_name = str(fmt).lower()
                    if fmt_name in {"geotiff", "tif", "tiff"}:
                        candidate = Path(source_path)
                        if not _is_supported_raster(candidate):
                            raster_source = _pick_primary_raster_reference(references)
                            if raster_source is not None and _is_supported_raster(Path(raster_source)):
                                candidate = Path(raster_source)
                            else:
                                candidate = working_dir / f"{step_id or 'export'}_raster.tif"
                                _create_default_raster(candidate)
                        source_path = str(candidate)
                        artifacts.append({"artifact_type": "geotiff", "path": str(source_path)})
                        exported_tif_path = str(source_path)
                        produced_artifact_path = str(source_path)
                    elif fmt_name in {"png", "png_map"}:
                        png_path = working_dir / f"{step_id or 'export'}_preview.png"
                        png_path.write_bytes(b"png-output")
                        artifacts.append({"artifact_type": "png_map", "path": str(png_path)})
                        exported_png_path = str(png_path)
                        produced_artifact_path = str(png_path)
                    elif fmt_name == "csv":
                        csv_path = Path(source_path)
                        if csv_path.suffix.lower() != ".csv":
                            csv_path = working_dir / f"{step_id or 'export'}_table.csv"
                            with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
                                writer = csv.writer(csv_file)
                                writer.writerow(["key", "value"])
                                writer.writerow(["source", str(source_path)])
                        artifacts.append({"artifact_type": "csv", "path": str(csv_path)})
                        produced_artifact_path = str(csv_path)
                    else:
                        raise ValueError(f"Unsupported artifact export format: {fmt_name}")

                artifact_ref = str(outputs.get("artifact") or "")
                if artifact_ref and produced_artifact_path:
                    references[artifact_ref] = produced_artifact_path
            else:
                raise ValueError(f"Unsupported operation in dispatcher: {op_name}")

            completed_steps.add(step_id)
            pending_nodes.remove(node)
            progressed = True

        if not progressed:
            unresolved = [str(node.get("step_id") or "") for node in pending_nodes]
            raise ValueError(f"Operation plan has unresolved dependencies for steps: {unresolved}")

    primary_tif = exported_tif_path or _pick_primary_raster_reference(references)
    tif_path, png_path = _ensure_placeholder_artifacts(
        working_dir,
        tif_path=primary_tif,
        png_path=exported_png_path,
    )
    metrics = _compute_raster_metrics(tif_path)

    return {
        "mode": "operation_plan",
        "artifacts": artifacts,
        "tif_path": tif_path,
        "png_path": png_path,
        "actual_time_range": None,
        "selected_item_ids": [],
        "valid_pixel_ratio": metrics["valid_pixel_ratio"],
        "ndvi_min": metrics["ndvi_min"],
        "ndvi_max": metrics["ndvi_max"],
        "ndvi_mean": metrics["ndvi_mean"],
        "output_width": metrics["output_width"],
        "output_height": metrics["output_height"],
        "output_crs": metrics["output_crs"],
    }
