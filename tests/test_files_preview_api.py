from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from apps.api.main import app
from packages.domain.config import get_settings
from packages.domain.database import SessionLocal
from packages.domain.models import SessionRecord, UploadedFileRecord
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


def _seed_uploaded_file(*, file_id: str, file_type: str, storage_path: Path) -> None:
    with SessionLocal() as db:
        db.query(UploadedFileRecord).filter(UploadedFileRecord.id == file_id).delete(
            synchronize_session=False
        )
        session = db.get(SessionRecord, "ses_file_preview")
        if session is None:
            session = SessionRecord(id="ses_file_preview", title="preview", status="active")
            db.add(session)
        db.add(
            UploadedFileRecord(
                id=file_id,
                session_id=session.id,
                original_name=storage_path.name,
                file_type=file_type,
                storage_key=str(storage_path),
                size_bytes=storage_path.stat().st_size,
                checksum="sha256-preview",
            )
        )
        db.commit()


def _cleanup_uploaded_file(file_id: str) -> None:
    with SessionLocal() as db:
        db.query(UploadedFileRecord).filter(UploadedFileRecord.id == file_id).delete(
            synchronize_session=False
        )
        remaining = (
            db.query(UploadedFileRecord)
            .filter(UploadedFileRecord.session_id == "ses_file_preview")
            .count()
        )
        session = db.get(SessionRecord, "ses_file_preview")
        if session is not None and remaining == 0:
            db.delete(session)
        db.commit()


def test_files_preview_endpoint_returns_vector_geojson(tmp_path: Path) -> None:
    kml_path = tmp_path / "boundary.kml"
    kml_path.write_text(
        """
<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Placemark>
    <name>demo</name>
    <Polygon>
      <outerBoundaryIs>
        <LinearRing>
          <coordinates>116.0,39.8 116.4,39.8 116.4,40.0 116.0,40.0 116.0,39.8</coordinates>
        </LinearRing>
      </outerBoundaryIs>
    </Polygon>
  </Placemark>
</kml>
        """.strip(),
        encoding="utf-8",
    )
    _seed_uploaded_file(file_id="file_preview_kml", file_type="vector_kml", storage_path=kml_path)
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/files/file_preview_kml/preview")
        assert response.status_code == 200
        payload = response.json()
        assert payload["preview_type"] == "vector_geojson"
        assert payload["geojson"]["type"] == "FeatureCollection"
        assert payload["feature_count"] == 1
        assert len(payload["bbox_bounds"]) == 4
    finally:
        _cleanup_uploaded_file("file_preview_kml")


def test_files_preview_image_endpoint_returns_png(tmp_path: Path) -> None:
    raster_path = tmp_path / "raster.tif"
    values = np.arange(200 * 160, dtype="float32").reshape((160, 200))
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=200,
        height=160,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(116.0, 40.0, 0.01, 0.01),
    ) as dataset:
        dataset.write(values, 1)

    _seed_uploaded_file(
        file_id="file_preview_raster", file_type="raster_tiff", storage_path=raster_path
    )
    try:
        with TestClient(app) as client:
            preview_response = client.get("/api/v1/files/file_preview_raster/preview")
            image_response = client.get("/api/v1/files/file_preview_raster/preview-image")
        assert preview_response.status_code == 200
        assert preview_response.json()["preview_type"] == "raster_image"
        assert image_response.status_code == 200
        assert image_response.headers["content-type"].startswith("image/png")
        assert image_response.content.startswith(b"\x89PNG")
    finally:
        _cleanup_uploaded_file("file_preview_raster")
