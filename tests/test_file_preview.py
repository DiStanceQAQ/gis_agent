from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest
import rasterio
import shapefile
from rasterio.transform import from_origin
from shapely.geometry import Polygon
from shapely.wkb import dumps as dump_wkb

from packages.domain.config import get_settings
from packages.domain.models import UploadedFileRecord
from packages.domain.services.file_preview import (
    build_uploaded_file_preview,
    read_uploaded_file_preview_image,
)
from packages.domain.services.storage import _get_storage_backend_cached


@pytest.fixture(autouse=True)
def _force_local_storage_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    monkeypatch.setenv("GIS_AGENT_STORAGE_BACKEND", "local")
    monkeypatch.setenv("GIS_AGENT_STORAGE_ROOT", str(tmp_path / ".data"))
    get_settings.cache_clear()
    _get_storage_backend_cached.cache_clear()
    yield
    get_settings.cache_clear()
    _get_storage_backend_cached.cache_clear()


def _make_uploaded_file(
    *,
    file_id: str,
    file_type: str,
    path: Path,
) -> UploadedFileRecord:
    return UploadedFileRecord(
        id=file_id,
        session_id="ses_preview",
        original_name=path.name,
        file_type=file_type,
        storage_key=str(path),
        size_bytes=path.stat().st_size,
        checksum="sha256-preview",
    )


def _write_test_shp_zip(path: Path) -> None:
    shape_dir = path.parent / "shape"
    shape_dir.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(shape_dir / "boundary"))
    writer.field("name", "C")
    writer.poly([[[116.0, 39.8], [116.4, 39.8], [116.4, 40.0], [116.0, 40.0], [116.0, 39.8]]])
    writer.record("boundary")
    writer.close()

    with zipfile.ZipFile(path, "w") as archive:
        for child in shape_dir.iterdir():
            archive.write(child, arcname=child.name)


def _write_test_shp_only(path: Path) -> None:
    writer = shapefile.Writer(str(path.with_suffix("")))
    writer.field("name", "C")
    writer.poly([[[116.0, 39.8], [116.4, 39.8], [116.4, 40.0], [116.0, 40.0], [116.0, 39.8]]])
    writer.record("single")
    writer.close()

    for suffix in (".shx", ".dbf", ".prj", ".cpg"):
        sidecar = path.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()


def _write_test_gpkg(path: Path) -> None:
    polygon = Polygon([(116.0, 39.8), (116.4, 39.8), (116.4, 40.0), (116.0, 40.0), (116.0, 39.8)])
    gpkg_blob = (
        b"GP"
        + bytes([0, 1])
        + (4326).to_bytes(4, "little", signed=True)
        + dump_wkb(polygon, hex=False)
    )

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
                srs_id INTEGER
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
                PRIMARY KEY (table_name, column_name)
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
                "EPSG:4326",
                "WGS84",
            ),
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
            ("features", "features", "features", "", 4326),
        )
        cursor.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, ?, ?, ?, ?, ?)",
            (
                "features",
                "geom",
                "POLYGON",
                4326,
                0,
                0,
            ),
        )
        cursor.execute("INSERT INTO features (geom, properties) VALUES (?, ?)", (gpkg_blob, "{}"))
        conn.commit()


def _write_test_kml(path: Path) -> None:
    path.write_text(
        """
<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>test-area</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>116.0,39.8 116.4,39.8 116.4,40.0 116.0,40.0 116.0,39.8</coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
        """.strip(),
        encoding="utf-8",
    )


def test_build_uploaded_file_preview_for_shp_zip(tmp_path: Path) -> None:
    zip_path = tmp_path / "boundary.zip"
    _write_test_shp_zip(zip_path)
    preview = build_uploaded_file_preview(
        _make_uploaded_file(file_id="file_shp_zip", file_type="shp_zip", path=zip_path)
    )

    assert preview.preview_type == "vector_geojson"
    assert preview.feature_count == 1
    assert preview.bbox_bounds is not None
    assert preview.geojson is not None
    assert preview.geojson.get("type") == "FeatureCollection"


def test_build_uploaded_file_preview_for_shp_without_sidecars(tmp_path: Path) -> None:
    shp_path = tmp_path / "boundary.shp"
    _write_test_shp_only(shp_path)
    preview = build_uploaded_file_preview(
        _make_uploaded_file(file_id="file_shp_only", file_type="shp", path=shp_path)
    )

    assert preview.preview_type == "vector_geojson"
    assert preview.feature_count == 1
    assert preview.bbox_bounds is not None


def test_build_uploaded_file_preview_for_gpkg(tmp_path: Path) -> None:
    gpkg_path = tmp_path / "boundary.gpkg"
    _write_test_gpkg(gpkg_path)
    preview = build_uploaded_file_preview(
        _make_uploaded_file(file_id="file_gpkg", file_type="vector_gpkg", path=gpkg_path)
    )

    assert preview.preview_type == "vector_geojson"
    assert preview.feature_count == 1
    assert preview.bbox_bounds is not None


def test_build_uploaded_file_preview_for_kml_and_kmz(tmp_path: Path) -> None:
    kml_path = tmp_path / "boundary.kml"
    _write_test_kml(kml_path)
    kmz_path = tmp_path / "boundary.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.write(kml_path, arcname="doc.kml")

    kml_preview = build_uploaded_file_preview(
        _make_uploaded_file(file_id="file_kml", file_type="vector_kml", path=kml_path)
    )
    kmz_preview = build_uploaded_file_preview(
        _make_uploaded_file(file_id="file_kmz", file_type="vector_kmz", path=kmz_path)
    )

    assert kml_preview.preview_type == "vector_geojson"
    assert kmz_preview.preview_type == "vector_geojson"
    assert kml_preview.feature_count == 1
    assert kmz_preview.feature_count == 1


def test_build_uploaded_file_preview_for_raster_image_and_read_png(tmp_path: Path) -> None:
    raster_path = tmp_path / "xinjiang_like.tif"
    data = np.linspace(0, 1000, 400 * 300, dtype="float32").reshape((300, 400))
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=400,
        height=300,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(116.0, 40.0, 0.01, 0.01),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(data, 1)

    uploaded = _make_uploaded_file(
        file_id="file_raster",
        file_type="raster_tiff",
        path=raster_path,
    )
    preview = build_uploaded_file_preview(uploaded)

    assert preview.preview_type == "raster_image"
    assert preview.bbox_bounds is not None
    assert preview.image_url is not None

    png_bytes = read_uploaded_file_preview_image(uploaded)
    assert png_bytes.startswith(b"\x89PNG")
