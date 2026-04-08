from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

import numpy as np
from PIL import Image
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import mapping, shape as shapely_shape
from shapely.ops import unary_union
from shapely.wkb import loads as load_wkb

from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.models import UploadedFileRecord
from packages.domain.services.storage import (
    get_storage_backend,
    materialize_storage_path,
    read_storage_bytes,
    storage_exists,
)

TARGET_CRS = "EPSG:4326"
MAX_PREVIEW_FEATURES = 2000
MAX_RASTER_PREVIEW_DIMENSION = 1400


@dataclass
class UploadedFilePreview:
    preview_type: str
    file_type: str
    bbox_bounds: list[float] | None = None
    feature_count: int | None = None
    geojson: dict[str, Any] | None = None
    image_url: str | None = None
    message: str | None = None


def _to_wgs84_geometry(geometry: dict[str, Any], *, source_crs: str | CRS | None) -> dict[str, Any]:
    if source_crs is None:
        return geometry
    source = str(source_crs)
    if not source or source in {TARGET_CRS, "OGC:CRS84"}:
        return geometry
    return transform_geom(source, TARGET_CRS, geometry, precision=-1)


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": features,
        "crs": {"type": "name", "properties": {"name": TARGET_CRS}},
    }


def _compute_bbox_bounds(geometries: list[dict[str, Any]]) -> list[float] | None:
    if not geometries:
        return None
    merged = unary_union([shapely_shape(geometry) for geometry in geometries if geometry])
    if merged.is_empty:
        return None
    min_x, min_y, max_x, max_y = merged.bounds
    return [float(min_x), float(min_y), float(max_x), float(max_y)]


def _limit_preview_features(
    features: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    if len(features) <= MAX_PREVIEW_FEATURES:
        return features, None
    return (
        features[:MAX_PREVIEW_FEATURES],
        f"预览已截断，仅显示前 {MAX_PREVIEW_FEATURES} 个要素。",
    )


def _extract_feature_geometries(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    geometries: list[dict[str, Any]] = []
    for feature in features:
        geometry = feature.get("geometry")
        if isinstance(geometry, dict):
            geometries.append(geometry)
    return geometries


def _load_geojson_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    payload = json.loads(read_storage_bytes(uploaded_file.storage_key).decode("utf-8"))
    features_raw = payload.get("features") if isinstance(payload, dict) else None
    if isinstance(features_raw, list):
        features = [feature for feature in features_raw if isinstance(feature, dict)]
    elif isinstance(payload, dict) and payload.get("type") == "Feature":
        features = [payload]
    elif isinstance(payload, dict) and payload.get("type"):
        features = [{"type": "Feature", "properties": {}, "geometry": payload}]
    else:
        features = []

    limited_features, message = _limit_preview_features(features)
    geometries = _extract_feature_geometries(limited_features)
    return UploadedFilePreview(
        preview_type="vector_geojson",
        file_type=uploaded_file.file_type,
        geojson=_feature_collection(limited_features),
        bbox_bounds=_compute_bbox_bounds(geometries),
        feature_count=len(limited_features),
        message=message,
    )


def _load_shp_features(
    *,
    shp_path: Path,
    source_crs: str | CRS | None,
    properties_builder: Callable[[int], dict[str, Any]],
) -> list[dict[str, Any]]:
    import shapefile

    reader = shapefile.Reader(str(shp_path))
    shapes = reader.shapes()
    fields = [str(field[0]) for field in reader.fields[1:]]
    records: list[Any] = []
    try:
        records = list(reader.records())
    except Exception:
        records = []

    has_attribute_rows = bool(records) and len(records) == len(shapes)

    features: list[dict[str, Any]] = []
    for index, shp in enumerate(shapes):
        geometry = shp.__geo_interface__
        geometry = _to_wgs84_geometry(geometry, source_crs=source_crs)
        properties = properties_builder(index)
        if has_attribute_rows:
            row_values = records[index]
            row_properties = {
                fields[col]: value for col, value in enumerate(row_values) if col < len(fields)
            }
            properties = {**row_properties, **properties}

        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": geometry,
            }
        )
    return features


def _load_prj_crs(shp_path: Path, search_root: Path | None = None) -> CRS | None:
    candidates = [shp_path.with_suffix(".prj")]
    if search_root is not None:
        candidates.extend(search_root.rglob("*.prj"))
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return CRS.from_wkt(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def _load_shp_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    with materialize_storage_path(uploaded_file.storage_key, suffix=".shp") as shp_path:
        source_crs = _load_prj_crs(shp_path)
        features = _load_shp_features(
            shp_path=shp_path,
            source_crs=source_crs,
            properties_builder=lambda index: {"row": index + 1},
        )
    limited_features, message = _limit_preview_features(features)
    geometries = _extract_feature_geometries(limited_features)
    return UploadedFilePreview(
        preview_type="vector_geojson",
        file_type=uploaded_file.file_type,
        geojson=_feature_collection(limited_features),
        bbox_bounds=_compute_bbox_bounds(geometries),
        feature_count=len(limited_features),
        message=message,
    )


def _load_shp_zip_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    with materialize_storage_path(uploaded_file.storage_key, suffix=".zip") as zip_path:
        with tempfile.TemporaryDirectory(prefix="gis-agent-preview-shp-") as temp_dir:
            temp_path = Path(temp_dir)
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(temp_path)

            shp_files = list(temp_path.rglob("*.shp"))
            if not shp_files:
                raise AppError.bad_request(
                    error_code=ErrorCode.AOI_PARSE_FAILED,
                    message="Shapefile 压缩包中未找到 .shp 文件。",
                    detail={"file_id": uploaded_file.id},
                )

            shp_path = shp_files[0]
            source_crs = _load_prj_crs(shp_path, search_root=temp_path)
            features = _load_shp_features(
                shp_path=shp_path,
                source_crs=source_crs,
                properties_builder=lambda index: {"row": index + 1},
            )

    limited_features, message = _limit_preview_features(features)
    geometries = _extract_feature_geometries(limited_features)
    return UploadedFilePreview(
        preview_type="vector_geojson",
        file_type=uploaded_file.file_type,
        geojson=_feature_collection(limited_features),
        bbox_bounds=_compute_bbox_bounds(geometries),
        feature_count=len(limited_features),
        message=message,
    )


def _extract_wkb_from_gpkg_blob(blob: bytes) -> bytes:
    if len(blob) < 8 or blob[:2] != b"GP":
        return blob
    flags = blob[3]
    envelope_code = (flags >> 1) & 0b111
    envelope_size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(envelope_code, 0)
    header_size = 8 + envelope_size
    if len(blob) <= header_size:
        return b""
    return blob[header_size:]


def _quote_sql_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _load_gpkg_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    with materialize_storage_path(uploaded_file.storage_key, suffix=".gpkg") as gpkg_path:
        with sqlite3.connect(gpkg_path) as conn:
            cursor = conn.cursor()
            layers = cursor.execute(
                "SELECT table_name, column_name, srs_id FROM gpkg_geometry_columns"
            ).fetchall()
            features: list[dict[str, Any]] = []

            for table_name, column_name, srs_id in layers:
                table_sql = _quote_sql_identifier(str(table_name))
                column_sql = _quote_sql_identifier(str(column_name))
                rows = cursor.execute(
                    f"SELECT {column_sql} FROM {table_sql} WHERE {column_sql} IS NOT NULL"
                ).fetchall()

                source_crs: str | None = None
                if isinstance(srs_id, int) and srs_id > 0:
                    source_crs = f"EPSG:{srs_id}"

                for index, (blob,) in enumerate(rows):
                    if not isinstance(blob, (bytes, bytearray)):
                        continue
                    wkb_payload = _extract_wkb_from_gpkg_blob(bytes(blob))
                    if not wkb_payload:
                        continue
                    geometry = mapping(load_wkb(wkb_payload))
                    geometry = _to_wgs84_geometry(geometry, source_crs=source_crs)
                    features.append(
                        {
                            "type": "Feature",
                            "properties": {"layer": str(table_name), "row": index + 1},
                            "geometry": geometry,
                        }
                    )

    limited_features, message = _limit_preview_features(features)
    geometries = _extract_feature_geometries(limited_features)
    return UploadedFilePreview(
        preview_type="vector_geojson",
        file_type=uploaded_file.file_type,
        geojson=_feature_collection(limited_features),
        bbox_bounds=_compute_bbox_bounds(geometries),
        feature_count=len(limited_features),
        message=message,
    )


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _parse_kml_coordinates(text: str | None) -> list[list[float]]:
    if not text:
        return []
    coordinates: list[list[float]] = []
    for token in re.split(r"\s+", text.strip()):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        coordinates.append([lon, lat])
    return coordinates


def _ensure_linear_ring(ring: list[list[float]]) -> list[list[float]]:
    if not ring:
        return ring
    if ring[0] != ring[-1]:
        return [*ring, ring[0]]
    return ring


def _parse_kml_geometry(node: ElementTree.Element) -> list[dict[str, Any]]:
    name = _local_name(node.tag)
    if name == "Point":
        coords = _parse_kml_coordinates(node.findtext(".//{*}coordinates"))
        if coords:
            return [{"type": "Point", "coordinates": coords[0]}]
        return []
    if name == "LineString":
        coords = _parse_kml_coordinates(node.findtext(".//{*}coordinates"))
        if len(coords) >= 2:
            return [{"type": "LineString", "coordinates": coords}]
        return []
    if name == "Polygon":
        rings: list[list[list[float]]] = []
        outer = node.find(".//{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
        outer_ring = _ensure_linear_ring(
            _parse_kml_coordinates(outer.text if outer is not None else None)
        )
        if outer_ring:
            rings.append(outer_ring)
        for inner in node.findall(".//{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"):
            ring = _ensure_linear_ring(_parse_kml_coordinates(inner.text))
            if ring:
                rings.append(ring)
        if rings:
            return [{"type": "Polygon", "coordinates": rings}]
        return []
    if name == "MultiGeometry":
        geometries: list[dict[str, Any]] = []
        for child in list(node):
            geometries.extend(_parse_kml_geometry(child))
        return geometries
    return []


def _parse_kml_payload(content: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(content)
    features: list[dict[str, Any]] = []

    placemarks = root.findall(".//{*}Placemark")
    for placemark in placemarks:
        name = placemark.findtext("./{*}name")
        geometries: list[dict[str, Any]] = []
        for child in list(placemark):
            geometries.extend(_parse_kml_geometry(child))
        for geometry in geometries:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"name": name} if name else {},
                    "geometry": geometry,
                }
            )

    if features:
        return features

    geometries: list[dict[str, Any]] = []
    for child in root.iter():
        geometries.extend(_parse_kml_geometry(child))
    return [{"type": "Feature", "properties": {}, "geometry": geometry} for geometry in geometries]


def _load_kml_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    content = read_storage_bytes(uploaded_file.storage_key).decode("utf-8")
    features = _parse_kml_payload(content)
    limited_features, message = _limit_preview_features(features)
    geometries = _extract_feature_geometries(limited_features)
    return UploadedFilePreview(
        preview_type="vector_geojson",
        file_type=uploaded_file.file_type,
        geojson=_feature_collection(limited_features),
        bbox_bounds=_compute_bbox_bounds(geometries),
        feature_count=len(limited_features),
        message=message,
    )


def _load_kmz_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    with materialize_storage_path(uploaded_file.storage_key, suffix=".kmz") as kmz_path:
        with zipfile.ZipFile(kmz_path) as archive:
            kml_names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not kml_names:
                raise AppError.bad_request(
                    error_code=ErrorCode.AOI_PARSE_FAILED,
                    message="KMZ 文件中未找到 KML 内容。",
                    detail={"file_id": uploaded_file.id},
                )
            content = archive.read(kml_names[0]).decode("utf-8")
    features = _parse_kml_payload(content)
    limited_features, message = _limit_preview_features(features)
    geometries = _extract_feature_geometries(limited_features)
    return UploadedFilePreview(
        preview_type="vector_geojson",
        file_type=uploaded_file.file_type,
        geojson=_feature_collection(limited_features),
        bbox_bounds=_compute_bbox_bounds(geometries),
        feature_count=len(limited_features),
        message=message,
    )


def _build_raster_preview_storage_key(uploaded_file: UploadedFileRecord) -> str:
    return f"previews/{uploaded_file.session_id}/{uploaded_file.id}.png"


def _create_raster_preview_image(
    uploaded_file: UploadedFileRecord, *, storage_key: str
) -> list[float]:
    with materialize_storage_path(uploaded_file.storage_key, suffix=".tif") as raster_path:
        import rasterio

        with rasterio.open(raster_path) as dataset:
            source_bounds = dataset.bounds
            if dataset.crs:
                min_x, min_y, max_x, max_y = transform_bounds(
                    dataset.crs,
                    TARGET_CRS,
                    source_bounds.left,
                    source_bounds.bottom,
                    source_bounds.right,
                    source_bounds.top,
                    densify_pts=21,
                )
            else:
                min_x, min_y, max_x, max_y = (
                    source_bounds.left,
                    source_bounds.bottom,
                    source_bounds.right,
                    source_bounds.top,
                )

            scale = min(
                1.0,
                MAX_RASTER_PREVIEW_DIMENSION / max(dataset.width, 1),
                MAX_RASTER_PREVIEW_DIMENSION / max(dataset.height, 1),
            )
            out_width = max(1, int(round(dataset.width * scale)))
            out_height = max(1, int(round(dataset.height * scale)))

            band = dataset.read(
                1,
                out_shape=(out_height, out_width),
                resampling=Resampling.bilinear,
                masked=True,
            )
            values = np.array(band.astype("float32").filled(np.nan), dtype="float32")
            mask = np.isfinite(values)
            if dataset.nodata is not None:
                mask &= values != float(dataset.nodata)

            rgba = np.zeros((out_height, out_width, 4), dtype=np.uint8)
            if mask.any():
                valid = values[mask]
                lo = float(np.percentile(valid, 2))
                hi = float(np.percentile(valid, 98))
                grayscale = np.zeros(values.shape, dtype=np.uint8)
                if hi - lo < 1e-8:
                    grayscale[mask] = 255
                else:
                    normalized = np.clip((values - lo) / (hi - lo), 0.0, 1.0) * 255.0
                    grayscale[mask] = normalized[mask].astype(np.uint8)
                rgba[..., 0] = grayscale
                rgba[..., 1] = grayscale
                rgba[..., 2] = grayscale
                rgba[..., 3] = np.where(mask, 220, 0).astype(np.uint8)

            image = Image.fromarray(rgba)
            payload = BytesIO()
            image.save(payload, format="PNG")
            get_storage_backend().save_bytes(
                storage_key, payload.getvalue(), content_type="image/png"
            )

            return [float(min_x), float(min_y), float(max_x), float(max_y)]


def _load_raster_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    storage_key = _build_raster_preview_storage_key(uploaded_file)
    bbox_bounds = _create_raster_preview_image(uploaded_file, storage_key=storage_key)
    settings = get_settings()
    return UploadedFilePreview(
        preview_type="raster_image",
        file_type=uploaded_file.file_type,
        bbox_bounds=bbox_bounds,
        image_url=f"{settings.api_prefix}/files/{uploaded_file.id}/preview-image",
        message="已使用服务端轻量预览避免浏览器直接加载大栅格导致卡顿。",
    )


def build_uploaded_file_preview(uploaded_file: UploadedFileRecord) -> UploadedFilePreview:
    try:
        if uploaded_file.file_type == "raster_tiff":
            return _load_raster_preview(uploaded_file)
        if uploaded_file.file_type == "geojson":
            return _load_geojson_preview(uploaded_file)
        if uploaded_file.file_type == "shp":
            return _load_shp_preview(uploaded_file)
        if uploaded_file.file_type == "shp_zip":
            return _load_shp_zip_preview(uploaded_file)
        if uploaded_file.file_type == "vector_gpkg":
            return _load_gpkg_preview(uploaded_file)
        if uploaded_file.file_type == "vector_kml":
            return _load_kml_preview(uploaded_file)
        if uploaded_file.file_type == "vector_kmz":
            return _load_kmz_preview(uploaded_file)
    except AppError:
        raise
    except Exception as exc:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="无法生成文件预览。",
            detail={
                "file_id": uploaded_file.id,
                "file_type": uploaded_file.file_type,
                "error": repr(exc),
            },
        ) from exc

    return UploadedFilePreview(
        preview_type="unsupported",
        file_type=uploaded_file.file_type,
        message=f"当前不支持该格式的地图预览：{uploaded_file.file_type}",
    )


def read_uploaded_file_preview_image(uploaded_file: UploadedFileRecord) -> bytes:
    if uploaded_file.file_type != "raster_tiff":
        raise AppError.bad_request(
            error_code=ErrorCode.BAD_REQUEST,
            message="Only raster uploads expose preview images.",
            detail={"file_id": uploaded_file.id, "file_type": uploaded_file.file_type},
        )

    storage_key = _build_raster_preview_storage_key(uploaded_file)
    if not storage_exists(storage_key):
        _create_raster_preview_image(uploaded_file, storage_key=storage_key)
    return read_storage_bytes(storage_key)
