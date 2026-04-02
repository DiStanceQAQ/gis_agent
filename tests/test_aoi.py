from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from rasterio.crs import CRS
from rasterio.warp import transform
import shapefile
from shapely.wkt import loads as load_wkt

from packages.domain.config import get_settings
from packages.domain.models import TaskRunRecord, TaskSpecRecord, UploadedFileRecord
from packages.domain.services.aoi import (
    normalize_bbox_text,
    normalize_geojson_file,
    normalize_task_aoi,
    parse_bbox_text,
    resolve_named_aoi,
)
from packages.domain.services.storage import _get_storage_backend_cached


@pytest.fixture(autouse=True)
def _force_local_storage_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_STORAGE_BACKEND", "local")
    get_settings.cache_clear()
    _get_storage_backend_cached.cache_clear()
    yield
    get_settings.cache_clear()
    _get_storage_backend_cached.cache_clear()


def test_parse_bbox_text_from_literal() -> None:
    bbox = parse_bbox_text("bbox(116.1, 39.8, 116.5, 40.1)")

    assert bbox == [116.1, 39.8, 116.5, 40.1]


def test_normalize_bbox_text() -> None:
    normalized = normalize_bbox_text("[116.1,39.8,116.5,40.1]")

    assert normalized is not None
    assert normalized.source_type == "bbox"
    assert normalized.bbox_bounds == [116.1, 39.8, 116.5, 40.1]
    assert normalized.geom_wkt.startswith("MULTIPOLYGON")


def test_resolve_named_aoi_alias_from_registry() -> None:
    resolution = resolve_named_aoi("北京西山", preferred_source_type="place_alias")

    assert resolution.status == "resolved"
    assert resolution.normalized_aoi is not None
    assert resolution.normalized_aoi.name == "北京西山"
    assert resolution.normalized_aoi.source_type == "place_alias"
    assert resolution.normalized_aoi.bbox_bounds == [115.72, 39.86, 116.18, 40.06]


def test_resolve_named_aoi_admin_name_from_registry() -> None:
    resolution = resolve_named_aoi("海淀区", preferred_source_type="admin_name")

    assert resolution.status == "resolved"
    assert resolution.normalized_aoi is not None
    assert resolution.normalized_aoi.name == "北京市海淀区"
    assert resolution.normalized_aoi.source_type == "admin_name"


def test_resolve_named_aoi_returns_not_found_for_unknown_name() -> None:
    resolution = resolve_named_aoi("不存在的研究区", preferred_source_type="place_alias")

    assert resolution.status == "not_found"
    assert resolution.normalized_aoi is None


def test_normalize_task_aoi_uses_named_registry() -> None:
    task = TaskRunRecord(id="task_test", session_id="ses_test", user_message_id="msg_test")
    task.task_spec = TaskSpecRecord(
        task_id="task_test",
        aoi_input="海淀区",
        aoi_source_type="admin_name",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={},
    )

    normalized = normalize_task_aoi(task=task)

    assert normalized is not None
    assert normalized.name == "北京市海淀区"


def test_normalize_geojson_file(tmp_path: Path) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "test-aoi"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [116.1, 39.8],
                            [116.5, 39.8],
                            [116.5, 40.1],
                            [116.1, 40.1],
                            [116.1, 39.8],
                        ]
                    ],
                },
            }
        ],
    }
    geojson_path = tmp_path / "aoi.geojson"
    geojson_path.write_text(json.dumps(payload), encoding="utf-8")

    uploaded_file = UploadedFileRecord(
        id="file_test",
        session_id="ses_test",
        original_name="aoi.geojson",
        file_type="geojson",
        storage_key=str(geojson_path),
        size_bytes=geojson_path.stat().st_size,
        checksum="sha256",
    )

    normalized = normalize_geojson_file(uploaded_file)

    assert normalized.source_type == "file_upload"
    assert normalized.name == "test-aoi"
    assert normalized.bbox_bounds == [116.1, 39.8, 116.5, 40.1]
    assert normalized.source_file_id == "file_test"


def test_normalize_geojson_file_preserves_holes_and_all_features(tmp_path: Path) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "complex-aoi"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [116.1, 39.8],
                            [116.6, 39.8],
                            [116.6, 40.2],
                            [116.1, 40.2],
                            [116.1, 39.8],
                        ],
                        [
                            [116.25, 39.9],
                            [116.45, 39.9],
                            [116.45, 40.0],
                            [116.25, 40.0],
                            [116.25, 39.9],
                        ],
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [117.0, 39.7],
                            [117.2, 39.7],
                            [117.2, 39.9],
                            [117.0, 39.9],
                            [117.0, 39.7],
                        ]
                    ],
                },
            },
        ],
    }
    geojson_path = tmp_path / "complex.geojson"
    geojson_path.write_text(json.dumps(payload), encoding="utf-8")

    uploaded_file = UploadedFileRecord(
        id="file_complex",
        session_id="ses_test",
        original_name="complex.geojson",
        file_type="geojson",
        storage_key=str(geojson_path),
        size_bytes=geojson_path.stat().st_size,
        checksum="sha256",
    )

    normalized = normalize_geojson_file(uploaded_file)
    geometry = load_wkt(normalized.geom_wkt)

    assert normalized.name == "complex-aoi"
    assert normalized.bbox_bounds == [116.1, 39.7, 117.2, 40.2]
    assert len(geometry.geoms) == 2
    assert any(len(polygon.interiors) == 1 for polygon in geometry.geoms)


def test_normalize_shp_zip_file(tmp_path: Path) -> None:
    from packages.domain.services.aoi import normalize_shp_zip_file

    shp_dir = tmp_path / "shape"
    shp_dir.mkdir()
    writer = shapefile.Writer(str(shp_dir / "aoi"))
    writer.field("name", "C")
    writer.poly(
        [
            [
                [116.1, 39.8],
                [116.5, 39.8],
                [116.5, 40.1],
                [116.1, 40.1],
                [116.1, 39.8],
            ]
        ]
    )
    writer.record("test-shp")
    writer.close()

    zip_path = tmp_path / "aoi.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for file_path in shp_dir.iterdir():
            archive.write(file_path, arcname=file_path.name)

    uploaded_file = UploadedFileRecord(
        id="file_zip",
        session_id="ses_test",
        original_name="aoi.zip",
        file_type="shp_zip",
        storage_key=str(zip_path),
        size_bytes=zip_path.stat().st_size,
        checksum="sha256",
    )

    normalized = normalize_shp_zip_file(uploaded_file)

    assert normalized.source_type == "file_upload"
    assert normalized.bbox_bounds == [116.1, 39.8, 116.5, 40.1]
    assert normalized.source_file_id == "file_zip"


def test_normalize_shp_zip_file_reprojects_prj(tmp_path: Path) -> None:
    from packages.domain.services.aoi import normalize_shp_zip_file

    lon_coords = [116.1, 116.5, 116.5, 116.1, 116.1]
    lat_coords = [39.8, 39.8, 40.1, 40.1, 39.8]
    x_coords, y_coords = transform("EPSG:4326", "EPSG:3857", lon_coords, lat_coords)
    polygon = [[x, y] for x, y in zip(x_coords, y_coords)]

    shp_dir = tmp_path / "shape_3857"
    shp_dir.mkdir()
    writer = shapefile.Writer(str(shp_dir / "aoi"))
    writer.field("name", "C")
    writer.poly([polygon])
    writer.record("test-3857")
    writer.close()
    (shp_dir / "aoi.prj").write_text(CRS.from_epsg(3857).to_wkt(), encoding="utf-8")

    zip_path = tmp_path / "aoi_3857.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for file_path in shp_dir.iterdir():
            archive.write(file_path, arcname=file_path.name)

    uploaded_file = UploadedFileRecord(
        id="file_zip_3857",
        session_id="ses_test",
        original_name="aoi_3857.zip",
        file_type="shp_zip",
        storage_key=str(zip_path),
        size_bytes=zip_path.stat().st_size,
        checksum="sha256",
    )

    normalized = normalize_shp_zip_file(uploaded_file)

    assert normalized.bbox_bounds == pytest.approx([116.1, 39.8, 116.5, 40.1], abs=1e-4)
    assert "assumed EPSG:4326" not in normalized.validation_message


def test_normalize_bbox_text_rejects_invalid_bbox() -> None:
    from packages.domain.errors import AppError, ErrorCode

    with pytest.raises(AppError) as exc_info:
        normalize_bbox_text("[116.5,39.8,116.1,40.1]")

    assert exc_info.value.error_code == ErrorCode.AOI_INVALID_BBOX
