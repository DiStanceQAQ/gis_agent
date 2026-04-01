from __future__ import annotations

import json
import math
import re
import tempfile
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoalchemy2.elements import WKTElement
from rasterio.crs import CRS
from rasterio.warp import transform_geom
from shapely.geometry import MultiPolygon, box, shape as shapely_shape
from shapely.ops import unary_union
from shapely.validation import make_valid

from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.models import AOIRecord, TaskRunRecord, UploadedFileRecord
from packages.domain.utils import make_id


COORDINATE_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
MAX_AOI_AREA_KM2 = 2_000_000.0
TARGET_CRS = "EPSG:4326"


@dataclass
class NormalizedAOI:
    source_type: str
    name: str | None
    geom_wkt: str
    bbox_wkt: str
    bbox_bounds: list[float]
    area_km2: float
    validation_message: str
    source_file_id: str | None = None


@dataclass
class NamedAOIResolution:
    status: str
    normalized_aoi: NormalizedAOI | None = None
    matched_names: list[str] | None = None


def _deg2_to_km2(area_deg2: float, latitude: float) -> float:
    km_per_degree_lat = 111.32
    km_per_degree_lon = 111.32 * max(0.1, abs(math.cos(math.radians(latitude))))
    return area_deg2 * km_per_degree_lat * km_per_degree_lon


def _import_shapefile_module() -> Any:
    try:
        import shapefile  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="Shapefile support is not available in the current runtime.",
            detail={"dependency": "pyshp"},
        ) from exc
    return shapefile


def _validate_bbox(bounds: list[float]) -> list[float]:
    if len(bounds) != 4:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_INVALID_BBOX,
            message="Bounding box must contain exactly four coordinates.",
            detail={"bbox": bounds},
        )
    xmin, ymin, xmax, ymax = [float(value) for value in bounds]
    if xmin >= xmax or ymin >= ymax:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_INVALID_BBOX,
            message="Bounding box coordinates are invalid.",
            detail={"bbox": bounds},
        )
    if xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_INVALID_BBOX,
            message="Bounding box is outside valid WGS84 bounds.",
            detail={"bbox": bounds},
        )
    return [xmin, ymin, xmax, ymax]


def _geometry_collection(geometries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "GeometryCollection", "geometries": geometries}


def _coerce_polygonal_geometry(geometry: dict[str, Any]) -> MultiPolygon:
    raw_geometry = shapely_shape(geometry)
    if raw_geometry.is_empty:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="AOI geometry is empty.",
        )

    polygonal = make_valid(raw_geometry)
    if polygonal.geom_type == "Polygon":
        multi = MultiPolygon([polygonal])
    elif polygonal.geom_type == "MultiPolygon":
        multi = polygonal
    elif hasattr(polygonal, "geoms"):
        polygon_parts = []
        for part in polygonal.geoms:
            if part.geom_type == "Polygon":
                polygon_parts.append(part)
            elif part.geom_type == "MultiPolygon":
                polygon_parts.extend(list(part.geoms))
        if not polygon_parts:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="Only polygon AOI geometries are supported.",
                detail={"geometry_type": polygonal.geom_type},
            )
        merged = unary_union(polygon_parts)
        if merged.geom_type == "Polygon":
            multi = MultiPolygon([merged])
        elif merged.geom_type == "MultiPolygon":
            multi = merged
        else:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="Failed to normalize AOI geometry into polygons.",
                detail={"geometry_type": merged.geom_type},
            )
    else:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="Only polygon AOI geometries are supported.",
            detail={"geometry_type": polygonal.geom_type},
        )

    if multi.is_empty:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="AOI geometry is empty after normalization.",
        )
    return multi


def _transform_to_wgs84(
    geometry: dict[str, Any],
    *,
    source_crs: CRS | str | None,
) -> dict[str, Any]:
    if source_crs is None:
        return geometry

    source = source_crs.to_string() if isinstance(source_crs, CRS) else str(source_crs)
    if source in {TARGET_CRS, "OGC:CRS84"}:
        return geometry

    try:
        return transform_geom(source, TARGET_CRS, geometry, precision=-1)
    except Exception as exc:  # pragma: no cover - defensive CRS handling
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="Failed to transform AOI geometry into WGS84.",
            detail={"source_crs": source},
        ) from exc


def _finalize_geometry(
    geometry: dict[str, Any],
    *,
    source_type: str,
    name: str | None,
    source_file_id: str | None = None,
    source_crs: CRS | str | None = TARGET_CRS,
    validation_message: str,
) -> NormalizedAOI:
    transformed = _transform_to_wgs84(geometry, source_crs=source_crs)
    polygonal = _coerce_polygonal_geometry(transformed)
    bbox_bounds = _validate_bbox(list(polygonal.bounds))
    latitude_center = (bbox_bounds[1] + bbox_bounds[3]) / 2
    area_km2 = _deg2_to_km2(float(polygonal.area), latitude_center)
    if area_km2 > MAX_AOI_AREA_KM2:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_AREA_TOO_LARGE,
            message="AOI area is too large for the MVP workflow.",
            detail={"area_km2": round(area_km2, 3), "max_area_km2": MAX_AOI_AREA_KM2},
        )

    bbox_polygon = box(*bbox_bounds)
    return NormalizedAOI(
        source_type=source_type,
        name=name,
        geom_wkt=polygonal.wkt,
        bbox_wkt=bbox_polygon.wkt,
        bbox_bounds=bbox_bounds,
        area_km2=round(area_km2, 3),
        validation_message=validation_message,
        source_file_id=source_file_id,
    )


def _extract_geojson_geometry(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    payload_type = payload.get("type")
    if payload_type in {"Polygon", "MultiPolygon"}:
        return None, payload
    if payload_type == "Feature":
        geometry = payload.get("geometry") or {}
        return (payload.get("properties") or {}).get("name"), geometry
    if payload_type == "FeatureCollection":
        features = payload.get("features") or []
        if not features:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="GeoJSON FeatureCollection is empty.",
            )
        geometries = []
        name = None
        for feature in features:
            geometry = feature.get("geometry")
            if geometry is None:
                continue
            geometries.append(geometry)
            if name is None:
                name = (feature.get("properties") or {}).get("name")
        if not geometries:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="GeoJSON FeatureCollection does not contain usable geometries.",
            )
        return name, _geometry_collection(geometries)
    raise AppError.bad_request(
        error_code=ErrorCode.AOI_PARSE_FAILED,
        message="Unsupported GeoJSON payload type.",
        detail={"type": payload_type},
    )


def _load_prj_crs(temp_path: Path, shp_path: Path) -> CRS | None:
    prj_candidates = [shp_path.with_suffix(".prj"), *temp_path.rglob("*.prj")]
    for prj_path in prj_candidates:
        if not prj_path.exists():
            continue
        try:
            return CRS.from_wkt(prj_path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def _default_aoi_registry_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "aoi_registry.json"


def _normalize_lookup_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().casefold()


def _load_aoi_registry() -> list[dict[str, Any]]:
    settings = get_settings()
    registry_path = (
        Path(settings.aoi_registry_path).expanduser()
        if settings.aoi_registry_path
        else _default_aoi_registry_path()
    )
    if not registry_path.exists():
        return []

    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive registry parsing
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="Failed to parse the configured AOI registry.",
            detail={"registry_path": str(registry_path)},
        ) from exc

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="Configured AOI registry must contain an entries list.",
            detail={"registry_path": str(registry_path)},
        )
    return entries


def _normalize_registry_entry(entry: dict[str, Any], *, source_type: str) -> NormalizedAOI:
    if not isinstance(entry.get("name"), str) or not entry["name"].strip():
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="AOI registry entry is missing a valid name.",
            detail={"entry": entry},
        )

    geometry = entry.get("geometry")
    bbox = entry.get("bbox")
    if geometry is None and bbox is None:
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="AOI registry entry must provide either geometry or bbox.",
            detail={"entry_name": entry["name"]},
        )

    if geometry is None:
        bbox_polygon = box(*_validate_bbox([float(value) for value in bbox]))
        geometry = {"type": "Polygon", "coordinates": [list(bbox_polygon.exterior.coords)]}

    return _finalize_geometry(
        geometry,
        source_type=source_type,
        name=entry["name"],
        validation_message=f"normalized from configured {source_type} registry entry",
    )


def resolve_named_aoi(name: str, *, preferred_source_type: str | None = None) -> NamedAOIResolution:
    lookup_key = _normalize_lookup_text(name)
    entries = _load_aoi_registry()
    matches: list[dict[str, Any]] = []
    for entry in entries:
        candidate_names = [entry.get("name"), *(entry.get("aliases") or [])]
        if any(isinstance(candidate, str) and _normalize_lookup_text(candidate) == lookup_key for candidate in candidate_names):
            matches.append(entry)

    if preferred_source_type:
        preferred_matches = [entry for entry in matches if entry.get("source_type") == preferred_source_type]
        if preferred_matches:
            matches = preferred_matches

    if not matches:
        return NamedAOIResolution(status="not_found", matched_names=[])
    if len(matches) > 1:
        return NamedAOIResolution(
            status="ambiguous",
            matched_names=[str(entry.get("name")) for entry in matches],
        )

    entry = matches[0]
    source_type = str(entry.get("source_type") or preferred_source_type or "place_alias")
    return NamedAOIResolution(
        status="resolved",
        normalized_aoi=_normalize_registry_entry(entry, source_type=source_type),
        matched_names=[str(entry.get("name"))],
    )


def build_named_aoi_clarification_message(
    name: str,
    *,
    resolution: NamedAOIResolution | None = None,
) -> str:
    if resolution and resolution.status == "ambiguous" and resolution.matched_names:
        candidates = " / ".join(resolution.matched_names)
        return (
            f"“{name}”在当前 AOI 注册表里命中了多个候选：{candidates}。"
            "请改用更完整的行政区名称、上传边界文件，或补充 bbox。"
        )
    return (
        f"当前版本没有在已配置 AOI 注册表里找到“{name}”的确定性边界。"
        "请上传 GeoJSON / shp.zip、改用 bbox，或先把该区域加入 AOI 注册表。"
    )


def parse_bbox_text(text: str) -> list[float] | None:
    numbers = [float(value) for value in COORDINATE_PATTERN.findall(text)]
    if len(numbers) < 4:
        return None
    if "bbox" in text.lower() or any(token in text for token in ["[", "]", "(", ")", "经度", "纬度"]):
        return _validate_bbox(numbers[:4])
    if re.fullmatch(
        r"\s*-?\d+(?:\.\d+)?(?:\s*[,，]\s*|\s+)-?\d+(?:\.\d+)?(?:\s*[,，]\s*|\s+)-?\d+(?:\.\d+)?(?:\s*[,，]\s*|\s+)-?\d+(?:\.\d+)?\s*",
        text,
    ):
        return _validate_bbox(numbers[:4])
    return None


def normalize_bbox_aoi(bounds: list[float], *, name: str | None = None) -> NormalizedAOI:
    validated_bounds = _validate_bbox(bounds)
    bbox_polygon = box(*validated_bounds)
    return _finalize_geometry(
        {
            "type": "Polygon",
            "coordinates": [list(bbox_polygon.exterior.coords)],
        },
        source_type="bbox",
        name=name or "bbox_aoi",
        validation_message="normalized from bbox input",
    )


def normalize_bbox_text(text: str) -> NormalizedAOI | None:
    bbox = parse_bbox_text(text)
    if bbox is None:
        return None
    return normalize_bbox_aoi(bbox)


def normalize_geojson_file(uploaded_file: UploadedFileRecord) -> NormalizedAOI:
    if uploaded_file.file_type != "geojson":
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_UNSUPPORTED_FILE_TYPE,
            message="Uploaded AOI file type is not supported yet.",
            detail={"file_id": uploaded_file.id, "file_type": uploaded_file.file_type},
        )

    try:
        payload = json.loads(Path(uploaded_file.storage_key).read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive file parsing
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_PARSE_FAILED,
            message="Failed to parse uploaded GeoJSON file.",
            detail={"file_id": uploaded_file.id},
        ) from exc

    name, geometry = _extract_geojson_geometry(payload)
    return _finalize_geometry(
        geometry,
        source_type="file_upload",
        name=name or uploaded_file.original_name,
        validation_message="normalized from uploaded geojson",
        source_file_id=uploaded_file.id,
    )


def normalize_shp_zip_file(uploaded_file: UploadedFileRecord) -> NormalizedAOI:
    shapefile = _import_shapefile_module()
    if uploaded_file.file_type != "shp_zip":
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_UNSUPPORTED_FILE_TYPE,
            message="Uploaded AOI file type is not supported yet.",
            detail={"file_id": uploaded_file.id, "file_type": uploaded_file.file_type},
        )

    with tempfile.TemporaryDirectory(prefix="gis-agent-shp-") as temp_dir:
        temp_path = Path(temp_dir)
        try:
            with zipfile.ZipFile(uploaded_file.storage_key) as archive:
                archive.extractall(temp_path)
        except zipfile.BadZipFile as exc:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="Uploaded shapefile archive is invalid.",
                detail={"file_id": uploaded_file.id},
            ) from exc

        shp_files = list(temp_path.rglob("*.shp"))
        if not shp_files:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="Shapefile archive does not contain a .shp file.",
                detail={"file_id": uploaded_file.id},
            )

        shp_path = shp_files[0]
        reader = shapefile.Reader(str(shp_path))
        shapes = reader.shapes()
        if not shapes:
            raise AppError.bad_request(
                error_code=ErrorCode.AOI_PARSE_FAILED,
                message="Shapefile archive contains no shapes.",
                detail={"file_id": uploaded_file.id},
            )

        geometries: list[dict[str, Any]] = []
        for shape_record in shapes:
            if shape_record.shapeType not in {
                shapefile.POLYGON,
                shapefile.POLYGONM,
                shapefile.POLYGONZ,
            }:
                raise AppError.bad_request(
                    error_code=ErrorCode.AOI_PARSE_FAILED,
                    message="Only polygon shapefiles are supported.",
                    detail={"shape_type": shape_record.shapeType},
                )
            geometries.append(shape_record.__geo_interface__)

        source_crs = _load_prj_crs(temp_path, shp_path)
        validation_message = "normalized from uploaded shapefile zip"
        if source_crs is None:
            validation_message += "; assumed EPSG:4326"

        return _finalize_geometry(
            _geometry_collection(geometries),
            source_type="file_upload",
            name=uploaded_file.original_name,
            source_file_id=uploaded_file.id,
            source_crs=source_crs or TARGET_CRS,
            validation_message=validation_message,
        )


def normalize_task_aoi(
    *,
    task: TaskRunRecord,
    uploaded_files: list[UploadedFileRecord] | None = None,
) -> NormalizedAOI | None:
    uploaded_files = uploaded_files or []
    if task.task_spec is None:
        return None

    if task.task_spec.aoi_source_type == "file_upload":
        if not uploaded_files:
            return None
        uploaded_file = uploaded_files[0]
        if uploaded_file.file_type == "geojson":
            return normalize_geojson_file(uploaded_file)
        if uploaded_file.file_type == "shp_zip":
            return normalize_shp_zip_file(uploaded_file)
        raise AppError.bad_request(
            error_code=ErrorCode.AOI_UNSUPPORTED_FILE_TYPE,
            message="Uploaded AOI file type is not supported yet.",
            detail={"file_id": uploaded_file.id, "file_type": uploaded_file.file_type},
        )

    if task.task_spec.aoi_input:
        bbox_aoi = normalize_bbox_text(task.task_spec.aoi_input)
        if bbox_aoi is not None:
            return bbox_aoi
        if task.task_spec.aoi_source_type in {"admin_name", "place_alias"}:
            resolution = resolve_named_aoi(
                task.task_spec.aoi_input,
                preferred_source_type=task.task_spec.aoi_source_type,
            )
            return resolution.normalized_aoi

    return None


def upsert_task_aoi(
    *,
    task: TaskRunRecord,
    normalized_aoi: NormalizedAOI,
) -> AOIRecord:
    if task.aoi is None:
        task.aoi = AOIRecord(id=make_id("aoi"), task_id=task.id)

    task.aoi.source_file_id = normalized_aoi.source_file_id
    task.aoi.name = normalized_aoi.name
    task.aoi.geom = WKTElement(normalized_aoi.geom_wkt, srid=4326)
    task.aoi.bbox = WKTElement(normalized_aoi.bbox_wkt, srid=4326)
    task.aoi.bbox_bounds_json = normalized_aoi.bbox_bounds
    task.aoi.area_km2 = normalized_aoi.area_km2
    task.aoi.is_valid = True
    task.aoi.validation_message = normalized_aoi.validation_message
    return task.aoi


def clone_task_aoi(
    *,
    task: TaskRunRecord,
    original_aoi: AOIRecord,
) -> AOIRecord:
    if task.aoi is None:
        task.aoi = AOIRecord(id=make_id("aoi"), task_id=task.id)

    task.aoi.source_file_id = original_aoi.source_file_id
    task.aoi.name = original_aoi.name
    task.aoi.geom = deepcopy(original_aoi.geom)
    task.aoi.bbox = deepcopy(original_aoi.bbox)
    task.aoi.bbox_bounds_json = deepcopy(original_aoi.bbox_bounds_json)
    task.aoi.area_km2 = original_aoi.area_km2
    task.aoi.is_valid = original_aoi.is_valid
    task.aoi.validation_message = original_aoi.validation_message
    return task.aoi
