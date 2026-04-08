from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from fastapi import UploadFile

from packages.domain.config import Settings, get_settings
from packages.domain.errors import AppError, ErrorCode


@dataclass(frozen=True)
class StoredObject:
    storage_key: str
    size_bytes: int
    checksum: str


def _checksum_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_name(filename: str) -> str:
    return Path(filename or "unnamed").name or "unnamed"


def build_upload_storage_key(session_id: str, filename: str) -> str:
    return f"uploads/{session_id}/{_safe_name(filename)}"


def build_artifact_storage_key(task_id: str, filename: str) -> str:
    return f"artifacts/{task_id}/{_safe_name(filename)}"


ARTIFACT_MIME_BY_TYPE: dict[str, str] = {
    "png_map": "image/png",
    "geotiff": "image/tiff",
    "methods_md": "text/markdown",
    "summary_md": "text/markdown",
    "csv": "text/csv",
    "geojson": "application/geo+json",
    "gpkg": "application/geopackage+sqlite3",
    "shapefile": "application/x-shapefile",
}


def infer_artifact_mime_type(path: str, *, artifact_type: str | None = None) -> str:
    if artifact_type and artifact_type in ARTIFACT_MIME_BY_TYPE:
        return ARTIFACT_MIME_BY_TYPE[artifact_type]

    guessed = mimetypes.guess_type(path)[0]
    if guessed:
        return guessed
    suffix = Path(path).suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    if suffix in {".geojson", ".json"}:
        return "application/geo+json"
    if suffix == ".gpkg":
        return "application/geopackage+sqlite3"
    if suffix == ".shp":
        return "application/x-shapefile"
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".csv":
        return "text/csv"
    return "application/octet-stream"


def _collect_raster_metadata(path: Path) -> dict[str, object]:
    import rasterio

    with rasterio.open(path) as dataset:
        resolution = None
        if dataset.res:
            resolution = [float(dataset.res[0]), float(dataset.res[1])]
        return {
            "projection": str(dataset.crs) if dataset.crs else None,
            "dimensions": {"width": int(dataset.width), "height": int(dataset.height)},
            "band_count": int(dataset.count),
            "dtype": str(dataset.dtypes[0]) if dataset.dtypes else None,
            "nodata": dataset.nodata,
            "resolution": resolution,
            "bounds": list(dataset.bounds) if dataset.bounds else None,
        }


def _collect_geojson_metadata(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        features = []
    crs = None
    crs_payload = payload.get("crs") if isinstance(payload, dict) else None
    if isinstance(crs_payload, dict):
        props = crs_payload.get("properties")
        if isinstance(props, dict):
            crs = props.get("name")
    geometry_type = None
    if features:
        geometry = features[0].get("geometry")
        if isinstance(geometry, dict):
            geometry_type = geometry.get("type")
    return {
        "projection": crs,
        "feature_count": len(features),
        "geometry_type": geometry_type,
    }


def _collect_gpkg_metadata(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        feature_tables = [
            row[0]
            for row in cursor.execute(
                "SELECT table_name FROM gpkg_contents WHERE data_type='features'"
            ).fetchall()
        ]
        feature_count = 0
        for table_name in feature_tables:
            query = f'SELECT COUNT(*) FROM "{table_name}"'
            feature_count += int(cursor.execute(query).fetchone()[0])

        srs_info = cursor.execute(
            "SELECT srs_id, organization, organization_coordsys_id FROM gpkg_spatial_ref_sys ORDER BY srs_id LIMIT 1"
        ).fetchone()
        projection = None
        if srs_info:
            projection = (
                f"{srs_info[1]}:{srs_info[2]}" if srs_info[1] and srs_info[2] else srs_info[0]
            )

    return {
        "projection": projection,
        "feature_count": feature_count,
        "layer_count": len(feature_tables),
    }


def _collect_shapefile_metadata(path: Path) -> dict[str, object]:
    import shapefile

    reader = shapefile.Reader(str(path))
    feature_count = int(reader.numRecords)
    bbox = list(reader.bbox) if reader.bbox else None
    shape_type = reader.shapeTypeName
    projection = None
    prj_path = path.with_suffix(".prj")
    if prj_path.exists():
        projection = prj_path.read_text(encoding="utf-8").strip() or None
    return {
        "projection": projection,
        "feature_count": feature_count,
        "shape_type": shape_type,
        "bbox": bbox,
    }


def collect_artifact_metadata(
    source_path: str,
    *,
    artifact_type: str,
    source_step: str | None = None,
) -> dict[str, object]:
    path = Path(source_path)
    metadata: dict[str, object] = {
        "artifact_type": artifact_type,
        "filename": path.name,
        "source_step": source_step,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if path.suffix.lower() in {".tif", ".tiff"}:
        metadata.update(_collect_raster_metadata(path))
    elif path.suffix.lower() in {".geojson", ".json"}:
        metadata.update(_collect_geojson_metadata(path))
    elif path.suffix.lower() == ".gpkg":
        metadata.update(_collect_gpkg_metadata(path))
    elif path.suffix.lower() == ".shp":
        metadata.update(_collect_shapefile_metadata(path))
    elif path.suffix.lower() == ".csv":
        line_count = 0
        with path.open("r", encoding="utf-8") as handle:
            for _ in handle:
                line_count += 1
        metadata["line_count"] = line_count
    elif path.suffix.lower() == ".md":
        metadata["line_count"] = len(path.read_text(encoding="utf-8").splitlines())

    return metadata


def _build_s3_client(
    *,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    region_name: str,
    addressing_style: str,
):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region_name,
        config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style}),
    )


class LocalStorageBackend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ensure_ready(self) -> None:
        Path(self.settings.storage_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.uploads_dir).mkdir(parents=True, exist_ok=True)
        Path(self.settings.artifacts_dir).mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, storage_key: str) -> Path:
        candidate = Path(storage_key)
        if candidate.is_absolute() or candidate.exists():
            return candidate
        return Path(self.settings.storage_root) / storage_key

    def save_bytes(
        self, storage_key: str, content: bytes, *, content_type: str | None = None
    ) -> StoredObject:
        del content_type
        path = self._path_for_key(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return StoredObject(
            storage_key=storage_key,
            size_bytes=len(content),
            checksum=_checksum_bytes(content),
        )

    def save_file(
        self, storage_key: str, source_path: str, *, content_type: str | None = None
    ) -> StoredObject:
        del content_type
        source = Path(source_path)
        destination = self._path_for_key(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.absolute() != destination.absolute():
            shutil.copy2(source, destination)
        return StoredObject(
            storage_key=storage_key,
            size_bytes=os.path.getsize(destination),
            checksum=hashlib.sha256(destination.read_bytes()).hexdigest(),
        )

    def read_bytes(self, storage_key: str) -> bytes:
        return self._path_for_key(storage_key).read_bytes()

    def exists(self, storage_key: str) -> bool:
        return self._path_for_key(storage_key).exists()

    @contextmanager
    def materialize(self, storage_key: str, *, suffix: str | None = None) -> Iterator[Path]:
        del suffix
        yield self._path_for_key(storage_key)


class S3StorageBackend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.s3_endpoint_url:
            raise AppError.bad_request(
                error_code=ErrorCode.BAD_REQUEST,
                message="S3 storage backend requires GIS_AGENT_S3_ENDPOINT_URL.",
            )
        if not settings.s3_access_key_id or not settings.s3_secret_access_key:
            raise AppError.bad_request(
                error_code=ErrorCode.BAD_REQUEST,
                message="S3 storage backend requires GIS_AGENT_S3_ACCESS_KEY_ID and GIS_AGENT_S3_SECRET_ACCESS_KEY.",
            )
        self.client = _build_s3_client(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            addressing_style=settings.s3_addressing_style,
        )
        self.bucket = settings.s3_bucket
        self._ready = False

    def ensure_ready(self) -> None:
        if self._ready:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)
        self._ready = True

    def save_bytes(
        self, storage_key: str, content: bytes, *, content_type: str | None = None
    ) -> StoredObject:
        self.ensure_ready()
        extra: dict[str, object] = {}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=storage_key, Body=content, **extra)
        return StoredObject(
            storage_key=storage_key,
            size_bytes=len(content),
            checksum=_checksum_bytes(content),
        )

    def save_file(
        self, storage_key: str, source_path: str, *, content_type: str | None = None
    ) -> StoredObject:
        content = Path(source_path).read_bytes()
        guessed_type = content_type or mimetypes.guess_type(source_path)[0]
        return self.save_bytes(storage_key, content, content_type=guessed_type)

    def read_bytes(self, storage_key: str) -> bytes:
        self.ensure_ready()
        response = self.client.get_object(Bucket=self.bucket, Key=storage_key)
        return response["Body"].read()

    def exists(self, storage_key: str) -> bool:
        self.ensure_ready()
        try:
            self.client.head_object(Bucket=self.bucket, Key=storage_key)
        except Exception:
            return False
        return True

    @contextmanager
    def materialize(self, storage_key: str, *, suffix: str | None = None) -> Iterator[Path]:
        content = self.read_bytes(storage_key)
        with tempfile.NamedTemporaryFile(
            prefix="gis-agent-storage-",
            suffix=suffix or Path(storage_key).suffix,
            delete=False,
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        try:
            yield temp_path
        finally:
            temp_path.unlink(missing_ok=True)


@lru_cache
def _get_storage_backend_cached(
    storage_backend: str,
    storage_root: str,
    s3_endpoint_url: str | None,
    s3_bucket: str,
    s3_access_key_id: str | None,
    s3_secret_access_key: str | None,
    s3_region: str,
    s3_addressing_style: str,
):
    settings = Settings(
        storage_backend=storage_backend,
        storage_root=storage_root,
        s3_endpoint_url=s3_endpoint_url,
        s3_bucket=s3_bucket,
        s3_access_key_id=s3_access_key_id,
        s3_secret_access_key=s3_secret_access_key,
        s3_region=s3_region,
        s3_addressing_style=s3_addressing_style,
    )
    if settings.is_s3_storage:
        return S3StorageBackend(settings)
    return LocalStorageBackend(settings)


def get_storage_backend(settings: Settings | None = None):
    cfg = settings or get_settings()
    return _get_storage_backend_cached(
        cfg.storage_backend,
        cfg.storage_root,
        cfg.s3_endpoint_url,
        cfg.s3_bucket,
        cfg.s3_access_key_id,
        cfg.s3_secret_access_key,
        cfg.s3_region,
        cfg.s3_addressing_style,
    )


def ensure_storage_dirs(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    Path(cfg.storage_root).mkdir(parents=True, exist_ok=True)
    Path(cfg.uploads_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.artifacts_dir).mkdir(parents=True, exist_ok=True)
    get_storage_backend(cfg).ensure_ready()


def detect_file_type(filename: str) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".geojson") or lower_name.endswith(".json"):
        return "geojson"
    if lower_name.endswith(".shp"):
        return "shp"
    if lower_name.endswith(".kml"):
        return "vector_kml"
    if lower_name.endswith(".kmz"):
        return "vector_kmz"
    if lower_name.endswith(".zip"):
        return "shp_zip"
    if lower_name.endswith(".tif") or lower_name.endswith(".tiff"):
        return "raster_tiff"
    if lower_name.endswith(".gpkg"):
        return "vector_gpkg"
    return "other"


def write_upload_file(
    session_id: str, upload: UploadFile, settings: Settings | None = None
) -> tuple[str, int, str]:
    content = upload.file.read()
    storage_key = build_upload_storage_key(session_id, upload.filename or "uploaded_file")
    stored = get_storage_backend(settings).save_bytes(
        storage_key,
        content,
        content_type=upload.content_type,
    )
    return stored.storage_key, stored.size_bytes, stored.checksum


def build_artifact_path(task_id: str, filename: str, settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    artifact_dir = Path(cfg.artifacts_dir) / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return str(artifact_dir / filename)


def persist_artifact_file(
    task_id: str,
    filename: str,
    source_path: str,
    *,
    content_type: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, int, str]:
    storage_key = build_artifact_storage_key(task_id, filename)
    stored = get_storage_backend(settings).save_file(
        storage_key,
        source_path,
        content_type=content_type,
    )
    return stored.storage_key, stored.size_bytes, stored.checksum


def read_storage_bytes(storage_key: str, settings: Settings | None = None) -> bytes:
    return get_storage_backend(settings).read_bytes(storage_key)


def read_storage_text(
    storage_key: str, *, encoding: str = "utf-8", settings: Settings | None = None
) -> str:
    return read_storage_bytes(storage_key, settings=settings).decode(encoding)


def storage_exists(storage_key: str, settings: Settings | None = None) -> bool:
    return get_storage_backend(settings).exists(storage_key)


@contextmanager
def materialize_storage_path(
    storage_key: str,
    *,
    suffix: str | None = None,
    settings: Settings | None = None,
) -> Iterator[Path]:
    with get_storage_backend(settings).materialize(storage_key, suffix=suffix) as path:
        yield path


def file_metadata(path: str) -> tuple[int, str]:
    content = Path(path).read_bytes()
    return os.path.getsize(path), hashlib.sha256(content).hexdigest()
