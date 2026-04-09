from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import rasterio
from shapely.geometry import box, shape

from packages.domain.config import get_settings
from packages.domain.models import UploadedFileRecord
from packages.domain.services.aoi import normalize_geojson_file, normalize_shp_file
from packages.domain.services.processing_pipeline import run_processing_pipeline
from packages.domain.services.storage import _get_storage_backend_cached


REAL_DATA_ROOT = Path(os.environ.get("GIS_AGENT_REAL_DATA_ROOT", "/Users/ljn/gis_data"))
REAL_DATA_ENABLED = os.environ.get("GIS_AGENT_ENABLE_REAL_DATA_TESTS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}


@pytest.fixture(autouse=True)
def _force_local_storage_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_STORAGE_BACKEND", "local")
    get_settings.cache_clear()
    _get_storage_backend_cached.cache_clear()
    yield
    get_settings.cache_clear()
    _get_storage_backend_cached.cache_clear()


@pytest.fixture(scope="module")
def real_data_paths() -> dict[str, Path]:
    if not REAL_DATA_ENABLED:
        pytest.skip("Set GIS_AGENT_ENABLE_REAL_DATA_TESTS=1 to run local gis_data integration tests.")

    paths = {
        "raster": REAL_DATA_ROOT / "CACD-2020.tif",
        "xinjiang_geojson": REAL_DATA_ROOT / "区划" / "xinjiang.geojson",
        "province_shp": REAL_DATA_ROOT / "区划" / "省.shp",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        pytest.skip(f"Missing required gis_data files: {missing}")
    return paths


def _write_small_clip_geojson(*, source_geojson: Path, output_geojson: Path) -> None:
    payload = json.loads(source_geojson.read_text(encoding="utf-8"))
    features = payload.get("features") or []
    if not features:
        raise ValueError(f"{source_geojson} has no features")

    geom = shape(features[0]["geometry"])
    min_x, min_y, max_x, max_y = geom.bounds
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    half_size = min((max_x - min_x) / 12.0, (max_y - min_y) / 12.0, 0.2)
    clip_geom = box(center_x - half_size, center_y - half_size, center_x + half_size, center_y + half_size)

    clip_payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "real-data-small-clip"},
                "geometry": clip_geom.__geo_interface__,
            }
        ],
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
    }
    output_geojson.write_text(json.dumps(clip_payload, ensure_ascii=False), encoding="utf-8")


def test_real_data_raster_clip_and_geotiff_export(tmp_path: Path, real_data_paths: dict[str, Path]) -> None:
    source_raster = real_data_paths["raster"]
    clip_geojson = tmp_path / "clip_small.geojson"
    _write_small_clip_geojson(source_geojson=real_data_paths["xinjiang_geojson"], output_geojson=clip_geojson)
    output_tif = tmp_path / "xinjiang_small_from_real_data.tif"

    plan_nodes = [
        {
            "step_id": "clip_real",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {
                "source_path": str(source_raster),
                "clip_path": str(clip_geojson),
                "crop": True,
            },
            "outputs": {"raster": "r_clip"},
        },
        {
            "step_id": "export_real",
            "op_name": "artifact.export",
            "depends_on": ["clip_real"],
            "inputs": {"primary": "r_clip"},
            "params": {"formats": ["geotiff"], "output_path": str(output_tif)},
            "outputs": {"artifact": "a_out"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_real_clip", plan_nodes=plan_nodes, working_dir=tmp_path)

    assert output_tif.exists()
    geotiff_artifacts = [item for item in outputs["artifacts"] if item["artifact_type"] == "geotiff"]
    assert geotiff_artifacts
    assert geotiff_artifacts[0]["path"] == str(output_tif)

    with rasterio.open(source_raster) as src, rasterio.open(output_tif) as dst:
        assert dst.width < src.width
        assert dst.height < src.height
        assert dst.count >= 1


def test_real_data_vector_buffer_and_geojson_export(tmp_path: Path, real_data_paths: dict[str, Path]) -> None:
    source_vector = real_data_paths["xinjiang_geojson"]

    plan_nodes = [
        {
            "step_id": "upload_vector_real",
            "op_name": "input.upload_vector",
            "depends_on": [],
            "inputs": {"upload_id": "real_vector_upload"},
            "params": {"source_path": str(source_vector)},
            "outputs": {"vector": "v_raw"},
        },
        {
            "step_id": "buffer_real",
            "op_name": "vector.buffer",
            "depends_on": ["upload_vector_real"],
            "inputs": {"vector": "v_raw"},
            "params": {"distance_m": 2000},
            "outputs": {"vector": "v_buf"},
        },
        {
            "step_id": "export_geojson_real",
            "op_name": "artifact.export",
            "depends_on": ["buffer_real"],
            "inputs": {"primary": "v_buf"},
            "params": {"formats": ["geojson"]},
            "outputs": {"artifact": "a_geojson"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_real_vector", plan_nodes=plan_nodes, working_dir=tmp_path)
    geojson_artifacts = [item for item in outputs["artifacts"] if item["artifact_type"] == "geojson"]
    assert geojson_artifacts

    geojson_path = Path(geojson_artifacts[0]["path"])
    assert geojson_path.exists()
    payload = json.loads(geojson_path.read_text(encoding="utf-8"))
    assert payload.get("type") == "FeatureCollection"
    assert len(payload.get("features") or []) >= 1


def test_real_data_vector_clip_with_real_overlay(tmp_path: Path, real_data_paths: dict[str, Path]) -> None:
    source_vector = real_data_paths["province_shp"]
    overlay_vector = real_data_paths["xinjiang_geojson"]

    plan_nodes = [
        {
            "step_id": "upload_province_real",
            "op_name": "input.upload_vector",
            "depends_on": [],
            "inputs": {"upload_id": "real_province_upload"},
            "params": {"source_path": str(source_vector)},
            "outputs": {"vector": "v_province"},
        },
        {
            "step_id": "clip_province_real",
            "op_name": "vector.clip",
            "depends_on": ["upload_province_real"],
            "inputs": {"vector": "v_province"},
            "params": {"overlay_path": str(overlay_vector)},
            "outputs": {"vector": "v_clipped"},
        },
        {
            "step_id": "export_clip_geojson_real",
            "op_name": "artifact.export",
            "depends_on": ["clip_province_real"],
            "inputs": {"primary": "v_clipped"},
            "params": {"formats": ["geojson"]},
            "outputs": {"artifact": "a_clip_geojson"},
        },
    ]

    outputs = run_processing_pipeline(
        task_id="task_real_vector_clip",
        plan_nodes=plan_nodes,
        working_dir=tmp_path,
    )
    geojson_artifacts = [item for item in outputs["artifacts"] if item["artifact_type"] == "geojson"]
    assert geojson_artifacts

    geojson_path = Path(geojson_artifacts[0]["path"])
    assert geojson_path.exists()
    payload = json.loads(geojson_path.read_text(encoding="utf-8"))
    assert payload.get("type") == "FeatureCollection"
    assert len(payload.get("features") or []) >= 1


def test_real_data_export_shapefile_has_sidecars(tmp_path: Path, real_data_paths: dict[str, Path]) -> None:
    source_vector = real_data_paths["xinjiang_geojson"]

    plan_nodes = [
        {
            "step_id": "upload_vector_shp_real",
            "op_name": "input.upload_vector",
            "depends_on": [],
            "inputs": {"upload_id": "real_shp_upload"},
            "params": {"source_path": str(source_vector)},
            "outputs": {"vector": "v_src"},
        },
        {
            "step_id": "export_shp_real",
            "op_name": "artifact.export",
            "depends_on": ["upload_vector_shp_real"],
            "inputs": {"primary": "v_src"},
            "params": {"formats": ["shapefile"]},
            "outputs": {"artifact": "a_shp"},
        },
    ]

    outputs = run_processing_pipeline(
        task_id="task_real_shapefile_export",
        plan_nodes=plan_nodes,
        working_dir=tmp_path,
    )
    shapefile_artifacts = [item for item in outputs["artifacts"] if item["artifact_type"] == "shapefile"]
    assert shapefile_artifacts

    shp_path = Path(shapefile_artifacts[0]["path"])
    assert shp_path.exists()
    assert shp_path.suffix.lower() == ".shp"
    for suffix in (".dbf", ".shx", ".prj"):
        assert shp_path.with_suffix(suffix).exists()


def test_real_data_normalize_geojson_aoi(real_data_paths: dict[str, Path]) -> None:
    geojson_path = real_data_paths["xinjiang_geojson"]
    uploaded_file = UploadedFileRecord(
        id="file_real_geojson",
        session_id="ses_real_data",
        original_name=geojson_path.name,
        file_type="geojson",
        storage_key=str(geojson_path),
        size_bytes=geojson_path.stat().st_size,
        checksum="sha256-real-geojson",
    )

    normalized = normalize_geojson_file(uploaded_file)

    assert normalized.source_type == "file_upload"
    assert normalized.source_file_id == "file_real_geojson"
    assert normalized.area_km2 > 0
    assert len(normalized.bbox_bounds) == 4


def test_real_data_normalize_shp_allows_large_aoi(real_data_paths: dict[str, Path]) -> None:
    shp_path = real_data_paths["province_shp"]
    uploaded_file = UploadedFileRecord(
        id="file_real_shp",
        session_id="ses_real_data",
        original_name=shp_path.name,
        file_type="shp",
        storage_key=str(shp_path),
        size_bytes=shp_path.stat().st_size,
        checksum="sha256-real-shp",
    )

    normalized = normalize_shp_file(uploaded_file)

    assert normalized.source_type == "file_upload"
    assert normalized.source_file_id == "file_real_shp"
    assert normalized.area_km2 > 0
    assert len(normalized.bbox_bounds) == 4
