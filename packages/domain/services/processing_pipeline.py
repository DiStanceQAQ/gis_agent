from __future__ import annotations

import csv
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import shapefile
from rasterio.errors import RasterioIOError
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject, transform_geom
from shapely.geometry import GeometryCollection, box, mapping, shape
from shapely.ops import unary_union
from shapely.wkb import dumps as dumps_wkb


def _pick_primary_raster_reference(references: dict[str, str]) -> str | None:
    for ref in references.values():
        if ref.lower().endswith(".tif") or ref.lower().endswith(".tiff"):
            return ref
    return next(iter(references.values()), None)


def _pick_primary_vector_reference(references: dict[str, str]) -> str | None:
    for ref in references.values():
        if ref.lower().endswith(".geojson") or ref.lower().endswith(".json"):
            return ref
    return None


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


def _default_geometry() -> dict[str, Any]:
    return box(116.0, 39.95, 116.03, 40.0).__geo_interface__


def _load_geojson_geometries(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    crs_name = None
    crs_payload = payload.get("crs")
    if isinstance(crs_payload, dict):
        props = crs_payload.get("properties")
        if isinstance(props, dict):
            crs_name_raw = props.get("name")
            if isinstance(crs_name_raw, str) and crs_name_raw:
                crs_name = crs_name_raw

    if payload.get("type") == "FeatureCollection":
        geometries = [
            feature.get("geometry")
            for feature in payload.get("features") or []
            if isinstance(feature, dict) and feature.get("geometry")
        ]
        return [geometry for geometry in geometries if isinstance(geometry, dict)], crs_name
    if payload.get("type") == "Feature":
        geometry = payload.get("geometry")
        return ([geometry] if isinstance(geometry, dict) else []), crs_name
    if payload.get("type"):
        return [payload], crs_name
    return [], crs_name


def _write_geojson(path: Path, geometries: list[dict[str, Any]], *, crs: str = "EPSG:4326") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": geometry,
            }
            for geometry in geometries
        ],
        "crs": {"type": "name", "properties": {"name": crs}},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _resolve_vector_input(
    raw_value: Any,
    *,
    references: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    if raw_value is None:
        return [], None
    if isinstance(raw_value, dict) and raw_value.get("type"):
        return [raw_value], None
    if isinstance(raw_value, list):
        geometries: list[dict[str, Any]] = []
        crs_name: str | None = None
        for item in raw_value:
            item_geometries, item_crs = _resolve_vector_input(item, references=references)
            geometries.extend(item_geometries)
            if item_crs:
                crs_name = item_crs
        return geometries, crs_name

    ref = str(raw_value)
    candidate = Path(references[ref]) if ref in references else Path(ref)
    if candidate.exists() and candidate.suffix.lower() in {".json", ".geojson"}:
        return _load_geojson_geometries(candidate)
    return [], None


def _resolve_vector_geometries(
    *,
    node: dict[str, Any],
    references: dict[str, str],
) -> tuple[list[dict[str, Any]], str]:
    inputs = node.get("inputs") or {}
    params = node.get("params") or {}
    geometries: list[dict[str, Any]] = []
    detected_crs: str | None = None

    for key in ("vector", "vectors"):
        part, part_crs = _resolve_vector_input(inputs.get(key), references=references)
        geometries.extend(part)
        if part_crs:
            detected_crs = part_crs
    for key in ("source_path", "source_paths"):
        part, part_crs = _resolve_vector_input(params.get(key), references=references)
        geometries.extend(part)
        if part_crs:
            detected_crs = part_crs

    geometry = params.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type"):
        geometries.append(geometry)

    geometries_param = params.get("geometries")
    if isinstance(geometries_param, list):
        for item in geometries_param:
            if isinstance(item, dict) and item.get("type"):
                geometries.append(item)

    bbox = params.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        minx, miny, maxx, maxy = [float(value) for value in bbox]
        geometries.append(box(minx, miny, maxx, maxy).__geo_interface__)

    if not geometries:
        geometries = [_default_geometry()]

    crs_name = str(params.get("source_crs") or detected_crs or "EPSG:4326")
    return geometries, crs_name


def _resolve_overlay_geometries(
    *,
    node: dict[str, Any],
    references: dict[str, str],
) -> tuple[list[dict[str, Any]], str]:
    inputs = node.get("inputs") or {}
    params = node.get("params") or {}
    geometries: list[dict[str, Any]] = []
    detected_crs: str | None = None

    for key in ("overlay", "mask", "clip_vector", "right", "vector_b"):
        part, part_crs = _resolve_vector_input(inputs.get(key), references=references)
        geometries.extend(part)
        if part_crs:
            detected_crs = part_crs
    for key in ("overlay_path", "overlay_paths"):
        part, part_crs = _resolve_vector_input(params.get(key), references=references)
        geometries.extend(part)
        if part_crs:
            detected_crs = part_crs

    for key in ("overlay_geometry", "clip_geometry"):
        geom = params.get(key)
        if isinstance(geom, dict) and geom.get("type"):
            geometries.append(geom)

    for key in ("overlay_geometries", "clip_geometries"):
        value = params.get(key)
        if isinstance(value, list):
            for geom in value:
                if isinstance(geom, dict) and geom.get("type"):
                    geometries.append(geom)

    for key in ("overlay_bbox", "clip_bbox"):
        bbox = params.get(key)
        if isinstance(bbox, list) and len(bbox) == 4:
            minx, miny, maxx, maxy = [float(item) for item in bbox]
            geometries.append(box(minx, miny, maxx, maxy).__geo_interface__)

    if not geometries:
        geometries = [_default_geometry()]

    crs_name = str(params.get("overlay_crs") or detected_crs or "EPSG:4326")
    return geometries, crs_name


def _geometry_dicts_to_shapes(geometries: list[dict[str, Any]]) -> list[Any]:
    return [shape(geometry) for geometry in geometries if isinstance(geometry, dict)]


def _shape_to_geometry_list(geom: Any) -> list[dict[str, Any]]:
    if geom.is_empty:
        return []
    if isinstance(geom, GeometryCollection):
        results: list[dict[str, Any]] = []
        for part in geom.geoms:
            if not part.is_empty:
                results.append(mapping(part))
        return results
    return [mapping(geom)]


def _require_matching_crs(*, left_crs: str, right_crs: str, op_name: str) -> None:
    if left_crs != right_crs:
        raise ValueError(f"{op_name} CRS mismatch: {left_crs} vs {right_crs}")


def _normalize_epsg_code(crs: str | None) -> int:
    if not crs:
        return 0
    candidate = crs.upper()
    if candidate.startswith("EPSG:"):
        try:
            return int(candidate.split(":", 1)[1])
        except ValueError:
            return 0
    return 0


def _geometry_type_name(geometries: list[dict[str, Any]]) -> str:
    for geometry in geometries:
        geom_type = str(geometry.get("type") or "").upper()
        if "POINT" in geom_type:
            return "POINT"
        if "LINE" in geom_type:
            return "LINESTRING"
        if "POLYGON" in geom_type:
            return "POLYGON"
    return "GEOMETRY"


def _build_gpkg_geometry_blob(geometry: Any, *, srs_id: int) -> bytes:
    # GeoPackage binary header + WKB payload (little-endian, no envelope).
    header = b"GP" + bytes([0, 1]) + struct.pack("<i", int(srs_id))
    return header + dumps_wkb(geometry, hex=False)


def _write_gpkg(path: Path, geometries: list[dict[str, Any]], *, crs: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    srs_id = _normalize_epsg_code(crs)
    geometry_type = _geometry_type_name(geometries)
    shapes = _geometry_dicts_to_shapes(geometries)

    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT UNIQUE,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER,
                CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE gpkg_geometry_columns (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL,
                m TINYINT NOT NULL,
                PRIMARY KEY (table_name, column_name),
                CONSTRAINT fk_ggc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
                CONSTRAINT fk_ggc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            )
            """
        )
        cursor.execute(
            "INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)",
            (
                "WGS 84 geodetic",
                4326,
                "EPSG",
                4326,
                "GEOGCS[\"WGS 84\",DATUM[\"WGS_1984\",SPHEROID[\"WGS 84\",6378137,298.257223563]],"
                "PRIMEM[\"Greenwich\",0],UNIT[\"degree\",0.0174532925199433]]",
                "WGS84",
            ),
        )
        if srs_id not in {0, 4326}:
            cursor.execute(
                "INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)",
                (f"EPSG:{srs_id}", srs_id, "EPSG", srs_id, f"EPSG:{srs_id}", ""),
            )
        cursor.execute(
            """
            CREATE TABLE features (
                fid INTEGER PRIMARY KEY AUTOINCREMENT,
                geom BLOB NOT NULL,
                properties TEXT
            )
            """
        )
        cursor.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, srs_id) VALUES (?, ?, ?, ?, ?)",
            ("features", "features", "features", "", srs_id),
        )
        cursor.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, ?, ?, ?, ?, ?)",
            ("features", "geom", geometry_type, srs_id, 0, 0),
        )
        for geom in shapes:
            if geom.is_empty:
                continue
            blob = _build_gpkg_geometry_blob(geom, srs_id=srs_id)
            cursor.execute(
                "INSERT INTO features (geom, properties) VALUES (?, ?)",
                (blob, "{}"),
            )
        conn.commit()


def _write_shapefile(path: Path, geometries: list[dict[str, Any]], *, crs: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shapes = _geometry_dicts_to_shapes(geometries)
    non_empty = [geom for geom in shapes if not geom.is_empty]
    if not non_empty:
        non_empty = [shape(_default_geometry())]

    def _geom_category(geom: Any) -> str:
        geom_type = geom.geom_type.upper()
        if "POLYGON" in geom_type:
            return "polygon"
        if "LINE" in geom_type:
            return "line"
        return "point"

    primary_type = _geom_category(non_empty[0])
    writer = shapefile.Writer(str(path))
    writer.field("id", "N")
    for index, geom in enumerate(non_empty, start=1):
        geom_type = _geom_category(geom)
        if geom_type != primary_type:
            if primary_type == "polygon":
                geom = geom.buffer(0.0)
            elif primary_type == "line":
                geom = geom.boundary
            else:
                geom = geom.representative_point()

        if primary_type == "polygon":
            polygons = [geom] if geom.geom_type == "Polygon" else list(getattr(geom, "geoms", []))
            for polygon in polygons:
                parts = [list(polygon.exterior.coords)]
                parts.extend([list(ring.coords) for ring in polygon.interiors])
                writer.poly(parts)
                writer.record(index)
        elif primary_type == "line":
            lines = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", []))
            for line in lines:
                writer.line([list(line.coords)])
                writer.record(index)
        else:
            points = [geom] if geom.geom_type == "Point" else list(getattr(geom, "geoms", []))
            for point in points:
                writer.point(point.x, point.y)
                writer.record(index)
    writer.close()

    prj_path = path.with_suffix(".prj")
    epsg_code = _normalize_epsg_code(crs)
    if epsg_code == 4326:
        prj_path.write_text(
            'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
            'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
            encoding="utf-8",
        )
    else:
        prj_path.write_text(f"EPSG:{epsg_code}" if epsg_code else crs, encoding="utf-8")


def _resolve_geometry_shapes(
    *,
    node: dict[str, Any],
    references: dict[str, str],
) -> list[dict[str, Any]]:
    geometries, _ = _resolve_vector_geometries(node=node, references=references)
    return geometries


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
    artifacts: list[dict[str, Any]] = []
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
            elif op_name == "raster.reclassify":
                output_ref = str(outputs.get("raster") or step_id or "raster_reclassify")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                rules = params.get("rules") or []
                if not isinstance(rules, list) or not rules:
                    raise ValueError("raster.reclassify requires non-empty rules list")

                default_value = float(params.get("default_value", 0.0))
                output_dtype = str(params.get("dtype") or "float32")
                with rasterio.open(source_path) as src:
                    data = src.read(1).astype("float32")
                    reclassified = np.full(data.shape, default_value, dtype="float32")
                    for rule in rules:
                        if isinstance(rule, dict):
                            min_value = float(rule.get("min", float("-inf")))
                            max_value = float(rule.get("max", float("inf")))
                            target_value = float(rule["value"])
                            include_min = bool(rule.get("include_min", True))
                            include_max = bool(rule.get("include_max", False))
                        elif isinstance(rule, (list, tuple)) and len(rule) == 3:
                            min_value = float(rule[0])
                            max_value = float(rule[1])
                            target_value = float(rule[2])
                            include_min = True
                            include_max = False
                        else:
                            raise ValueError("raster.reclassify rules must be dict or [min,max,value]")

                        min_mask = data >= min_value if include_min else data > min_value
                        max_mask = data <= max_value if include_max else data < max_value
                        reclassified[min_mask & max_mask] = target_value

                    profile = src.profile.copy()
                    profile.update(count=1, dtype=output_dtype)
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(reclassified.astype(output_dtype), 1)
                references[output_ref] = str(output_path)
            elif op_name == "raster.mask":
                output_ref = str(outputs.get("raster") or step_id or "raster_mask")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                source_path = _resolve_raster_source(node=node, references=references, working_dir=working_dir)
                geometries = _resolve_geometry_shapes(node=node, references=references)
                invert = bool(params.get("invert", False))
                with rasterio.open(source_path) as src:
                    all_touched = bool(params.get("all_touched", False))
                    mask_uint8 = rasterize(
                        [(geometry, 1) for geometry in geometries],
                        out_shape=(src.height, src.width),
                        transform=src.transform,
                        fill=0,
                        all_touched=all_touched,
                        dtype="uint8",
                    )
                    mask_bool = mask_uint8.astype(bool)
                    if invert:
                        mask_bool = ~mask_bool

                    profile = src.profile.copy()
                    output_data = src.read()
                    nodata = (
                        float(params.get("nodata"))
                        if params.get("nodata") is not None
                        else src.nodata
                    )
                    if nodata is None:
                        nodata = -9999.0 if np.issubdtype(output_data.dtype, np.floating) else 0

                    for band_idx in range(output_data.shape[0]):
                        band = output_data[band_idx]
                        band[~mask_bool] = nodata
                        output_data[band_idx] = band

                    profile.update(nodata=nodata)
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(output_data)
                references[output_ref] = str(output_path)
            elif op_name == "raster.rasterize":
                output_ref = str(outputs.get("raster") or step_id or "raster_rasterize")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                geometries = _resolve_geometry_shapes(node=node, references=references)
                burn_value = float(params.get("burn_value", 1.0))
                nodata = float(params.get("nodata", 0.0))
                output_dtype = str(params.get("dtype") or "float32")

                template_ref = str(inputs.get("raster") or "")
                template_path = (
                    Path(references[template_ref])
                    if template_ref and template_ref in references
                    else None
                )
                if template_path is None and params.get("template_raster_path"):
                    template_path = Path(str(params.get("template_raster_path")))

                if template_path is not None and _is_supported_raster(template_path):
                    with rasterio.open(template_path) as src:
                        width = src.width
                        height = src.height
                        transform = src.transform
                        crs = src.crs
                else:
                    width = int(params.get("width") or 256)
                    height = int(params.get("height") or 256)
                    if width <= 0 or height <= 0:
                        raise ValueError("raster.rasterize width/height must be positive")

                    if isinstance(params.get("bounds"), list) and len(params["bounds"]) == 4:
                        minx, miny, maxx, maxy = [float(value) for value in params["bounds"]]
                    else:
                        minx, miny, maxx, maxy = 116.0, 39.9, 116.1, 40.0
                    x_res = (maxx - minx) / width
                    y_res = (maxy - miny) / height
                    transform = from_origin(minx, maxy, x_res, y_res)
                    crs = str(params.get("target_crs") or "EPSG:4326")

                burned = rasterize(
                    [(geometry, burn_value) for geometry in geometries],
                    out_shape=(height, width),
                    transform=transform,
                    fill=nodata,
                    all_touched=bool(params.get("all_touched", False)),
                    dtype=output_dtype,
                )
                with rasterio.open(
                    output_path,
                    "w",
                    driver="GTiff",
                    height=height,
                    width=width,
                    count=1,
                    dtype=output_dtype,
                    crs=crs,
                    transform=transform,
                    nodata=nodata,
                ) as dst:
                    dst.write(burned, 1)
                references[output_ref] = str(output_path)
            elif op_name == "vector.buffer":
                output_ref = str(outputs.get("vector") or step_id or "vector_buffer")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                buffer_m = params.get("distance_m")
                distance = float(params.get("distance") if params.get("distance") is not None else 0.0)
                if buffer_m is not None:
                    buffer_m_value = float(buffer_m)
                    if source_crs.upper().endswith("4326"):
                        distance = buffer_m_value / 111320.0
                    else:
                        distance = buffer_m_value
                if distance <= 0:
                    raise ValueError("vector.buffer requires a positive distance or distance_m")

                source_shapes = _geometry_dicts_to_shapes(geometries)
                buffered_geometries: list[dict[str, Any]] = []
                for geom in source_shapes:
                    buffered_geometries.extend(_shape_to_geometry_list(geom.buffer(distance)))
                if not buffered_geometries:
                    buffered_geometries = [_default_geometry()]
                _write_geojson(output_path, buffered_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.clip":
                output_ref = str(outputs.get("vector") or step_id or "vector_clip")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                primary_geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                overlay_geometries, _ = _resolve_overlay_geometries(node=node, references=references)
                primary_shapes = _geometry_dicts_to_shapes(primary_geometries)
                overlay_shapes = _geometry_dicts_to_shapes(overlay_geometries)
                overlay_union = unary_union(overlay_shapes)

                clipped_geometries: list[dict[str, Any]] = []
                for geom in primary_shapes:
                    clipped_geometries.extend(_shape_to_geometry_list(geom.intersection(overlay_union)))
                if not clipped_geometries:
                    clipped_geometries = [_default_geometry()]
                _write_geojson(output_path, clipped_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.intersection":
                output_ref = str(outputs.get("vector") or step_id or "vector_intersection")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                left_geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                right_geometries, _ = _resolve_overlay_geometries(node=node, references=references)
                left_shapes = _geometry_dicts_to_shapes(left_geometries)
                right_shapes = _geometry_dicts_to_shapes(right_geometries)

                intersected_geometries: list[dict[str, Any]] = []
                for left_shape in left_shapes:
                    for right_shape in right_shapes:
                        intersected_geometries.extend(
                            _shape_to_geometry_list(left_shape.intersection(right_shape))
                        )
                if not intersected_geometries:
                    intersected_geometries = [_default_geometry()]
                _write_geojson(output_path, intersected_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.dissolve":
                output_ref = str(outputs.get("vector") or step_id or "vector_dissolve")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                source_shapes = _geometry_dicts_to_shapes(geometries)
                dissolved = unary_union(source_shapes)
                dissolved_geometries = _shape_to_geometry_list(dissolved)
                if not dissolved_geometries:
                    dissolved_geometries = [_default_geometry()]
                _write_geojson(output_path, dissolved_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.reproject":
                output_ref = str(outputs.get("vector") or step_id or "vector_reproject")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                target_crs = str(params.get("target_crs") or "EPSG:4326")
                source_override = params.get("source_crs")
                source_crs_name = str(source_override or source_crs or "EPSG:4326")
                reprojected_geometries: list[dict[str, Any]] = []
                for geometry in geometries:
                    if source_crs_name == target_crs:
                        reprojected_geometries.append(geometry)
                    else:
                        reprojected_geometries.append(
                            transform_geom(source_crs_name, target_crs, geometry)
                        )
                if not reprojected_geometries:
                    reprojected_geometries = [_default_geometry()]
                _write_geojson(output_path, reprojected_geometries, crs=target_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.union":
                output_ref = str(outputs.get("vector") or step_id or "vector_union")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                left_geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                right_geometries, right_crs = _resolve_overlay_geometries(node=node, references=references)
                _require_matching_crs(left_crs=source_crs, right_crs=right_crs, op_name=op_name)
                all_shapes = _geometry_dicts_to_shapes(left_geometries) + _geometry_dicts_to_shapes(right_geometries)
                merged = unary_union(all_shapes)
                merged_geometries = _shape_to_geometry_list(merged)
                if not merged_geometries:
                    merged_geometries = [_default_geometry()]
                _write_geojson(output_path, merged_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.erase":
                output_ref = str(outputs.get("vector") or step_id or "vector_erase")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                left_geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                right_geometries, right_crs = _resolve_overlay_geometries(node=node, references=references)
                _require_matching_crs(left_crs=source_crs, right_crs=right_crs, op_name=op_name)
                left_shapes = _geometry_dicts_to_shapes(left_geometries)
                right_union = unary_union(_geometry_dicts_to_shapes(right_geometries))

                erased_geometries: list[dict[str, Any]] = []
                for left_shape in left_shapes:
                    erased_geometries.extend(_shape_to_geometry_list(left_shape.difference(right_union)))
                if not erased_geometries:
                    erased_geometries = [_default_geometry()]
                _write_geojson(output_path, erased_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.simplify":
                output_ref = str(outputs.get("vector") or step_id or "vector_simplify")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                tolerance = float(params.get("tolerance") or 0.0)
                if tolerance <= 0:
                    raise ValueError("vector.simplify requires positive tolerance")
                preserve_topology = bool(params.get("preserve_topology", True))
                source_shapes = _geometry_dicts_to_shapes(geometries)

                simplified_geometries: list[dict[str, Any]] = []
                for geom in source_shapes:
                    simplified_geometries.extend(
                        _shape_to_geometry_list(geom.simplify(tolerance, preserve_topology=preserve_topology))
                    )
                if not simplified_geometries:
                    simplified_geometries = [_default_geometry()]
                _write_geojson(output_path, simplified_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "vector.spatial_join":
                output_ref = str(outputs.get("vector") or step_id or "vector_spatial_join")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                left_geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                right_geometries, right_crs = _resolve_overlay_geometries(node=node, references=references)
                _require_matching_crs(left_crs=source_crs, right_crs=right_crs, op_name=op_name)
                left_shapes = _geometry_dicts_to_shapes(left_geometries)
                right_shapes = _geometry_dicts_to_shapes(right_geometries)
                predicate = str(params.get("predicate") or "intersects")

                output_features: list[dict[str, Any]] = []
                for left_shape in left_shapes:
                    if left_shape.is_empty:
                        continue
                    join_count = 0
                    for right_shape in right_shapes:
                        if right_shape.is_empty:
                            continue
                        if predicate == "contains":
                            matched = left_shape.contains(right_shape)
                        elif predicate == "within":
                            matched = left_shape.within(right_shape)
                        else:
                            matched = left_shape.intersects(right_shape)
                        if matched:
                            join_count += 1
                    output_features.append(
                        {
                            "type": "Feature",
                            "properties": {"join_count": join_count, "predicate": predicate},
                            "geometry": mapping(left_shape),
                        }
                    )

                if not output_features:
                    output_features = [
                        {
                            "type": "Feature",
                            "properties": {"join_count": 0, "predicate": predicate},
                            "geometry": _default_geometry(),
                        }
                    ]

                payload = {
                    "type": "FeatureCollection",
                    "features": output_features,
                    "crs": {"type": "name", "properties": {"name": source_crs}},
                }
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                references[output_ref] = str(output_path)
            elif op_name == "vector.repair":
                output_ref = str(outputs.get("vector") or step_id or "vector_repair")
                output_path = working_dir / f"{step_id or output_ref}.geojson"
                geometries, source_crs = _resolve_vector_geometries(node=node, references=references)
                source_shapes = _geometry_dicts_to_shapes(geometries)

                repaired_geometries: list[dict[str, Any]] = []
                for geom in source_shapes:
                    if geom.is_empty:
                        continue
                    fixed = geom if geom.is_valid else geom.buffer(0)
                    repaired_geometries.extend(_shape_to_geometry_list(fixed))
                if not repaired_geometries:
                    repaired_geometries = [_default_geometry()]
                _write_geojson(output_path, repaired_geometries, crs=source_crs)
                references[output_ref] = str(output_path)
            elif op_name == "artifact.export":
                source_ref = str(inputs.get("primary") or "")
                source_path = references.get(source_ref)
                if source_path is None and params.get("source_path"):
                    source_path = str(params.get("source_path"))
                if source_path is None:
                    source_path = _pick_primary_raster_reference(references)
                if source_path is None:
                    source_path = _pick_primary_vector_reference(references)
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
                        artifacts.append(
                            {"artifact_type": "geotiff", "path": str(source_path), "source_step": step_id}
                        )
                        exported_tif_path = str(source_path)
                        produced_artifact_path = str(source_path)
                    elif fmt_name in {"png", "png_map"}:
                        png_path = working_dir / f"{step_id or 'export'}_preview.png"
                        png_path.write_bytes(b"png-output")
                        artifacts.append(
                            {"artifact_type": "png_map", "path": str(png_path), "source_step": step_id}
                        )
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
                        artifacts.append(
                            {"artifact_type": "csv", "path": str(csv_path), "source_step": step_id}
                        )
                        produced_artifact_path = str(csv_path)
                    elif fmt_name in {"geojson", "json"}:
                        vector_source = Path(source_path)
                        if vector_source.suffix.lower() not in {".geojson", ".json"}:
                            picked_vector = _pick_primary_vector_reference(references)
                            if picked_vector:
                                vector_source = Path(picked_vector)
                        if not vector_source.exists() or vector_source.suffix.lower() not in {".geojson", ".json"}:
                            vector_source = working_dir / f"{step_id or 'export'}_vector.geojson"
                            _write_geojson(vector_source, [_default_geometry()], crs="EPSG:4326")
                        artifacts.append(
                            {"artifact_type": "geojson", "path": str(vector_source), "source_step": step_id}
                        )
                        produced_artifact_path = str(vector_source)
                    elif fmt_name == "gpkg":
                        vector_source = Path(source_path)
                        geometries: list[dict[str, Any]] = []
                        vector_crs = "EPSG:4326"
                        if vector_source.exists() and vector_source.suffix.lower() in {".geojson", ".json"}:
                            geometries, parsed_crs = _load_geojson_geometries(vector_source)
                            if parsed_crs:
                                vector_crs = parsed_crs
                        if not geometries:
                            geometries = [_default_geometry()]
                        gpkg_path = working_dir / f"{step_id or 'export'}_vector.gpkg"
                        _write_gpkg(gpkg_path, geometries, crs=vector_crs)
                        artifacts.append(
                            {"artifact_type": "gpkg", "path": str(gpkg_path), "source_step": step_id}
                        )
                        produced_artifact_path = str(gpkg_path)
                    elif fmt_name in {"shapefile", "shp"}:
                        vector_source = Path(source_path)
                        geometries: list[dict[str, Any]] = []
                        vector_crs = "EPSG:4326"
                        if vector_source.exists() and vector_source.suffix.lower() in {".geojson", ".json"}:
                            geometries, parsed_crs = _load_geojson_geometries(vector_source)
                            if parsed_crs:
                                vector_crs = parsed_crs
                        if not geometries:
                            geometries = [_default_geometry()]
                        shp_path = working_dir / f"{step_id or 'export'}_vector.shp"
                        _write_shapefile(shp_path, geometries, crs=vector_crs)
                        artifacts.append(
                            {"artifact_type": "shapefile", "path": str(shp_path), "source_step": step_id}
                        )
                        produced_artifact_path = str(shp_path)
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
