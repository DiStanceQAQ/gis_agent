from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

from fastapi import UploadFile
import numpy as np
import rasterio
from rasterio.transform import from_origin

from packages.domain.config import Settings
from packages.domain.services import storage


class FakeS3Client:
    def __init__(self) -> None:
        self.bucket_exists = False
        self.objects: dict[tuple[str, str], bytes] = {}

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_exists:
            raise RuntimeError(f"missing bucket {Bucket}")

    def create_bucket(self, *, Bucket: str) -> None:
        self.bucket_exists = True

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs) -> None:
        del kwargs
        self.bucket_exists = True
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise RuntimeError(f"missing object {Bucket}/{Key}")


def _clear_storage_backend_cache() -> None:
    storage._get_storage_backend_cached.cache_clear()


def test_local_storage_backend_persists_upload_and_artifact(tmp_path: Path) -> None:
    settings = Settings(storage_root=str(tmp_path), storage_backend="local")
    _clear_storage_backend_cache()
    storage.ensure_storage_dirs(settings)

    upload = UploadFile(filename="aoi.geojson", file=BytesIO(b'{"type":"FeatureCollection","features":[]}'))
    storage_key, size_bytes, checksum = storage.write_upload_file("ses_test", upload, settings=settings)

    assert storage_key == "uploads/ses_test/aoi.geojson"
    assert size_bytes > 0
    assert checksum
    assert storage.storage_exists(storage_key, settings=settings)
    assert storage.read_storage_text(storage_key, settings=settings) == '{"type":"FeatureCollection","features":[]}'

    artifact_source = tmp_path / "artifact.txt"
    artifact_source.write_text("artifact-body", encoding="utf-8")
    artifact_key, artifact_size, artifact_checksum = storage.persist_artifact_file(
        "task_test",
        "summary.md",
        str(artifact_source),
        content_type="text/markdown",
        settings=settings,
    )

    assert artifact_key == "artifacts/task_test/summary.md"
    assert artifact_size == len("artifact-body")
    assert artifact_checksum
    assert storage.read_storage_text(artifact_key, settings=settings) == "artifact-body"


def test_s3_storage_backend_uses_object_keys_and_materializes_downloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_client = FakeS3Client()
    settings = Settings(
        storage_root=str(tmp_path),
        storage_backend="s3",
        s3_endpoint_url="http://minio:9000",
        s3_bucket="gis-agent",
        s3_access_key_id="minioadmin",
        s3_secret_access_key="minioadmin",
    )

    _clear_storage_backend_cache()
    monkeypatch.setattr(storage, "_build_s3_client", lambda **kwargs: fake_client)
    storage.ensure_storage_dirs(settings)

    upload = UploadFile(filename="aoi.geojson", file=BytesIO(b'{"hello":"world"}'))
    storage_key, size_bytes, checksum = storage.write_upload_file("ses_s3", upload, settings=settings)

    assert storage_key == "uploads/ses_s3/aoi.geojson"
    assert size_bytes == len(b'{"hello":"world"}')
    assert checksum
    assert fake_client.objects[(settings.s3_bucket, storage_key)] == b'{"hello":"world"}'

    artifact_source = tmp_path / "ndvi.tif"
    artifact_source.write_bytes(b"pretend-geotiff")
    artifact_key, artifact_size, artifact_checksum = storage.persist_artifact_file(
        "task_s3",
        "ndvi_real.tif",
        str(artifact_source),
        content_type="image/tiff",
        settings=settings,
    )

    assert artifact_key == "artifacts/task_s3/ndvi_real.tif"
    assert artifact_size == len(b"pretend-geotiff")
    assert artifact_checksum
    assert storage.storage_exists(artifact_key, settings=settings)
    assert storage.read_storage_bytes(artifact_key, settings=settings) == b"pretend-geotiff"

    with storage.materialize_storage_path(artifact_key, settings=settings) as local_path:
        assert local_path.exists()
        assert local_path.read_bytes() == b"pretend-geotiff"

    assert not any(path.name.startswith("gis-agent-storage-") for path in tmp_path.iterdir())


def test_detect_file_type_supports_upload_first_raster_vector() -> None:
    assert storage.detect_file_type("a.tif") == "raster_tiff"
    assert storage.detect_file_type("a.tiff") == "raster_tiff"
    assert storage.detect_file_type("zones.geojson") == "geojson"
    assert storage.detect_file_type("zones.shp") == "shp"
    assert storage.detect_file_type("zones.shp.zip") == "shp_zip"


def test_infer_artifact_mime_type_uses_artifact_type_mapping() -> None:
    assert storage.infer_artifact_mime_type("result.unknown", artifact_type="gpkg") == "application/geopackage+sqlite3"
    assert storage.infer_artifact_mime_type("map.png", artifact_type="png_map") == "image/png"


def test_collect_artifact_metadata_for_geojson_and_raster(tmp_path: Path) -> None:
    geojson_path = tmp_path / "zones.geojson"
    geojson_payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[116.0, 39.9], [116.1, 39.9], [116.1, 40.0], [116.0, 40.0], [116.0, 39.9]]
                    ],
                },
            }
        ],
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
    }
    geojson_path.write_text(json.dumps(geojson_payload), encoding="utf-8")

    raster_path = tmp_path / "ndvi.tif"
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(116.0, 40.0, 0.01, 0.01),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(np.ones((8, 8), dtype="float32"), 1)

    geo_meta = storage.collect_artifact_metadata(
        str(geojson_path),
        artifact_type="geojson",
        source_step="vector_export",
    )
    raster_meta = storage.collect_artifact_metadata(
        str(raster_path),
        artifact_type="geotiff",
        source_step="raster_export",
    )

    assert geo_meta["feature_count"] == 1
    assert geo_meta["projection"] == "EPSG:4326"
    assert geo_meta["source_step"] == "vector_export"

    assert raster_meta["projection"] == "EPSG:4326"
    assert raster_meta["dimensions"] == {"width": 8, "height": 8}
    assert raster_meta["source_step"] == "raster_export"
