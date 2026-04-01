from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

import numpy as np
import rasterio
from geoalchemy2.shape import to_shape
from PIL import Image
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform_bounds, transform_geom
from rasterio.windows import from_bounds as window_from_bounds
from shapely.geometry import box, mapping

from packages.domain.config import get_settings
from packages.domain.logging import get_logger
from packages.domain.models import AOIRecord
from packages.domain.services.catalog import get_collection_profile, search_items_for_dataset
from packages.domain.services.storage import build_artifact_path
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)


LANDSAT_CLEAR_MASK_BITS = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
SENTINEL_INVALID_SCL = {0, 1, 3, 8, 9, 10, 11}


@dataclass
class NDVIPipelineResult:
    tif_path: str
    png_path: str
    methods_text: str
    summary_text: str
    actual_time_range: dict[str, str] | None
    selected_item_ids: list[str]
    valid_pixel_ratio: float
    ndvi_min: float
    ndvi_max: float
    ndvi_mean: float
    mode: str


@dataclass
class TargetGrid:
    width: int
    height: int
    bounds: tuple[float, float, float, float]
    crs: str
    transform: Any


def _estimate_output_shape(
    bbox: list[float],
    *,
    spatial_resolution: int,
    max_dimension: int,
) -> tuple[int, int]:
    xmin, ymin, xmax, ymax = bbox
    latitude_center = (ymin + ymax) / 2
    width_m = max(1.0, (xmax - xmin) * 111320.0 * np.cos(np.radians(latitude_center)))
    height_m = max(1.0, (ymax - ymin) * 111320.0)
    width = max(64, min(max_dimension, int(round(width_m / spatial_resolution))))
    height = max(64, min(max_dimension, int(round(height_m / spatial_resolution))))
    return width, height


def _estimate_projected_shape(
    bounds: tuple[float, float, float, float],
    *,
    spatial_resolution: int,
    max_dimension: int,
) -> tuple[int, int]:
    xmin, ymin, xmax, ymax = bounds
    width = max(64, min(max_dimension, int(round(max(1.0, xmax - xmin) / spatial_resolution))))
    height = max(64, min(max_dimension, int(round(max(1.0, ymax - ymin) / spatial_resolution))))
    return width, height


def _signed_asset_href(href: str) -> str:
    parsed = urlparse(href)
    storage_account = parsed.netloc.split(".")[0]
    container = parsed.path.strip("/").split("/")[0]
    sas_url = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{storage_account}/{container}/"
    with urlopen(sas_url, timeout=get_settings().catalog_timeout_seconds) as response:
        token = json.load(response)["token"]
    separator = "&" if "?" in href else "?"
    return f"{href}{separator}{token}"


def _read_asset_to_grid(
    href: str,
    *,
    grid: TargetGrid,
    resampling: Resampling,
    dst_dtype: str,
    dst_nodata: float | int,
) -> np.ndarray:
    destination = np.full((grid.height, grid.width), dst_nodata, dtype=dst_dtype)
    with rasterio.open(_signed_asset_href(href)) as src:
        if str(src.crs) == grid.crs:
            window = window_from_bounds(*grid.bounds, transform=src.transform)
            data = src.read(
                1,
                window=window,
                out_shape=(grid.height, grid.width),
                boundless=True,
                fill_value=dst_nodata,
                resampling=resampling,
            )
            return data.astype(dst_dtype, copy=False)

        reproject(
            source=rasterio.band(src, 1),
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=dst_nodata,
            resampling=resampling,
        )
    return destination


def _resolve_target_grid(
    *,
    bbox_wgs84: list[float],
    sample_asset_href: str,
    spatial_resolution: int,
    max_dimension: int,
) -> TargetGrid:
    with rasterio.open(_signed_asset_href(sample_asset_href)) as src:
        crs = str(src.crs)
        if crs == "EPSG:4326":
            width, height = _estimate_output_shape(
                bbox_wgs84,
                spatial_resolution=spatial_resolution,
                max_dimension=max_dimension,
            )
            bounds = tuple(bbox_wgs84)
        else:
            bounds = transform_bounds("EPSG:4326", crs, *bbox_wgs84, densify_pts=21)
            width, height = _estimate_projected_shape(
                bounds,
                spatial_resolution=spatial_resolution,
                max_dimension=max_dimension,
            )
    return TargetGrid(
        width=width,
        height=height,
        bounds=bounds,
        crs=crs,
        transform=from_bounds(*bounds, width=width, height=height),
    )


def _landsat_clear_mask(qa_array: np.ndarray, red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    qa = qa_array.astype("uint16", copy=False)
    return ((qa & LANDSAT_CLEAR_MASK_BITS) == 0) & np.isfinite(red) & np.isfinite(nir) & (red > 0) & (nir > 0)


def _sentinel_clear_mask(scl_array: np.ndarray, red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    invalid = np.isin(scl_array.astype("uint8", copy=False), list(SENTINEL_INVALID_SCL))
    return (~invalid) & np.isfinite(red) & np.isfinite(nir) & (red > 0) & (nir > 0)


def _compute_landsat_ndvi(item: dict[str, Any], *, grid: TargetGrid) -> np.ndarray:
    red_raw = _read_asset_to_grid(
        item["assets"]["red"]["href"],
        grid=grid,
        resampling=Resampling.bilinear,
        dst_dtype="float32",
        dst_nodata=np.nan,
    )
    nir_raw = _read_asset_to_grid(
        item["assets"]["nir08"]["href"],
        grid=grid,
        resampling=Resampling.bilinear,
        dst_dtype="float32",
        dst_nodata=np.nan,
    )
    qa = _read_asset_to_grid(
        item["assets"]["qa_pixel"]["href"],
        grid=grid,
        resampling=Resampling.nearest,
        dst_dtype="uint16",
        dst_nodata=0,
    )

    red = red_raw * 0.0000275 - 0.2
    nir = nir_raw * 0.0000275 - 0.2
    valid = _landsat_clear_mask(qa, red, nir)
    ndvi = np.full((grid.height, grid.width), np.nan, dtype="float32")
    denominator = nir + red
    safe = valid & np.isfinite(denominator) & (np.abs(denominator) > 1e-6)
    ndvi[safe] = ((nir[safe] - red[safe]) / denominator[safe]).astype("float32")
    return np.clip(ndvi, -1.0, 1.0)


def _compute_sentinel_ndvi(item: dict[str, Any], *, grid: TargetGrid) -> np.ndarray:
    red_raw = _read_asset_to_grid(
        item["assets"]["B04"]["href"],
        grid=grid,
        resampling=Resampling.bilinear,
        dst_dtype="float32",
        dst_nodata=np.nan,
    )
    nir_raw = _read_asset_to_grid(
        item["assets"]["B08"]["href"],
        grid=grid,
        resampling=Resampling.bilinear,
        dst_dtype="float32",
        dst_nodata=np.nan,
    )
    scl = _read_asset_to_grid(
        item["assets"]["SCL"]["href"],
        grid=grid,
        resampling=Resampling.nearest,
        dst_dtype="uint8",
        dst_nodata=0,
    )

    red = red_raw / 10000.0
    nir = nir_raw / 10000.0
    valid = _sentinel_clear_mask(scl, red, nir)
    ndvi = np.full((grid.height, grid.width), np.nan, dtype="float32")
    denominator = nir + red
    safe = valid & np.isfinite(denominator) & (np.abs(denominator) > 1e-6)
    ndvi[safe] = ((nir[safe] - red[safe]) / denominator[safe]).astype("float32")
    return np.clip(ndvi, -1.0, 1.0)


def _compute_item_ndvi(dataset_name: str, item: dict[str, Any], *, grid: TargetGrid) -> np.ndarray:
    if dataset_name == "landsat89":
        return _compute_landsat_ndvi(item, grid=grid)
    if dataset_name == "sentinel2":
        return _compute_sentinel_ndvi(item, grid=grid)
    raise ValueError(f"Unsupported dataset_name for NDVI pipeline: {dataset_name}")


def _render_png(ndvi_array: np.ndarray, png_path: str) -> None:
    visual = np.nan_to_num((ndvi_array + 1.0) / 2.0, nan=0.0)
    normalized = np.clip(visual * 255.0, 0, 255).astype("uint8")
    rgb = np.zeros((normalized.shape[0], normalized.shape[1], 3), dtype="uint8")
    rgb[..., 1] = normalized
    rgb[..., 0] = (255 - normalized) // 3
    rgb[..., 2] = 60
    Image.fromarray(rgb, mode="RGB").save(png_path)


def _write_tif(ndvi_array: np.ndarray, *, tif_path: str, grid: TargetGrid) -> None:
    nodata = -9999.0
    output = np.where(np.isfinite(ndvi_array), ndvi_array, nodata).astype("float32")
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        height=grid.height,
        width=grid.width,
        count=1,
        dtype="float32",
        crs=grid.crs,
        transform=grid.transform,
        nodata=nodata,
    ) as dataset:
        dataset.write(output, 1)


def _primary_asset_key(dataset_name: str) -> str:
    return "red" if dataset_name == "landsat89" else "B04"


def _build_aoi_mask(aoi: AOIRecord, *, grid: TargetGrid) -> np.ndarray:
    if aoi.geom is not None:
        geometry = mapping(to_shape(aoi.geom))
    elif aoi.bbox_bounds_json:
        geometry = mapping(box(*[float(value) for value in aoi.bbox_bounds_json]))
    else:
        raise ValueError("AOI geometry is required for masking the NDVI output.")

    if grid.crs != "EPSG:4326":
        geometry = transform_geom("EPSG:4326", grid.crs, geometry, precision=-1)

    return geometry_mask(
        [geometry],
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        invert=True,
    )


def run_real_ndvi_pipeline(
    *,
    task_id: str,
    dataset_name: str,
    spec: ParsedTaskSpec,
    aoi: AOIRecord,
) -> NDVIPipelineResult:
    settings = get_settings()
    bbox = [float(value) for value in (aoi.bbox_bounds_json or [])]
    if len(bbox) != 4:
        raise ValueError("AOI bbox is required for the real NDVI pipeline.")

    profile = get_collection_profile(dataset_name)
    items = search_items_for_dataset(
        spec,
        aoi,
        dataset_name=dataset_name,
        max_items=settings.real_pipeline_max_items,
    )
    if not items:
        raise ValueError(f"No live STAC items found for dataset {dataset_name}.")

    primary_asset_href = items[0]["assets"][_primary_asset_key(dataset_name)]["href"]
    grid = _resolve_target_grid(
        bbox_wgs84=bbox,
        sample_asset_href=primary_asset_href,
        spatial_resolution=profile.spatial_resolution,
        max_dimension=settings.real_pipeline_max_dimension,
    )
    aoi_mask = _build_aoi_mask(aoi, grid=grid)

    ndvi_layers: list[np.ndarray] = []
    selected_item_ids: list[str] = []
    for item in items:
        with suppress(KeyError):
            ndvi = _compute_item_ndvi(dataset_name, item, grid=grid)
            ndvi_layers.append(np.where(aoi_mask, ndvi, np.nan).astype("float32"))
            selected_item_ids.append(item["id"])

    if not ndvi_layers:
        raise ValueError(f"Failed to build NDVI layers for dataset {dataset_name}.")

    stack = np.stack(ndvi_layers, axis=0)
    with np.errstate(all="ignore"):
        ndvi_composite = np.nanmedian(stack, axis=0).astype("float32")
    ndvi_composite = np.where(aoi_mask, ndvi_composite, np.nan).astype("float32")

    valid = np.isfinite(ndvi_composite)
    if not np.any(valid):
        raise ValueError("All NDVI pixels were filtered out during compositing.")

    ndvi_min = float(np.nanmin(ndvi_composite))
    ndvi_max = float(np.nanmax(ndvi_composite))
    ndvi_mean = float(np.nanmean(ndvi_composite))
    valid_pixel_ratio = float(valid.sum() / valid.size)

    tif_path = build_artifact_path(task_id, "ndvi_real.tif")
    png_path = build_artifact_path(task_id, "map_real.png")
    _write_tif(ndvi_composite, tif_path=tif_path, grid=grid)
    _render_png(ndvi_composite, png_path)

    source_dates = sorted(
        item.get("properties", {}).get("datetime")
        for item in items
        if item.get("properties", {}).get("datetime")
    )
    actual_time_range = None
    if source_dates:
        actual_time_range = {
            "start": source_dates[0][:10],
            "end": source_dates[-1][:10],
        }

    methods_text = (
        f"本次任务使用 {dataset_name} 的真实 STAC 候选影像执行 NDVI 流程，"
        f"按目标网格窗口读取红光/近红外波段，完成基础质量掩膜、逐景 NDVI 计算和中位数合成。"
        f"本次共使用 {len(selected_item_ids)} 景影像，输出网格尺寸约为 {grid.width}x{grid.height}，输出 CRS 为 {grid.crs}。"
    )
    summary_text = (
        f"真实 NDVI 合成已完成。共使用 {len(selected_item_ids)} 景影像，"
        f"有效像元占比约 {valid_pixel_ratio:.2%}，NDVI 均值 {ndvi_mean:.3f}，"
        f"最小值 {ndvi_min:.3f}，最大值 {ndvi_max:.3f}。"
    )
    logger.info(
        "ndvi_pipeline.completed task_id=%s dataset=%s item_count=%s valid_pixel_ratio=%.3f",
        task_id,
        dataset_name,
        len(selected_item_ids),
        valid_pixel_ratio,
    )

    return NDVIPipelineResult(
        tif_path=tif_path,
        png_path=png_path,
        methods_text=methods_text,
        summary_text=summary_text,
        actual_time_range=actual_time_range,
        selected_item_ids=selected_item_ids,
        valid_pixel_ratio=round(valid_pixel_ratio, 4),
        ndvi_min=round(ndvi_min, 4),
        ndvi_max=round(ndvi_max, 4),
        ndvi_mean=round(ndvi_mean, 4),
        mode="real",
    )
