from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import UploadFile

from packages.domain.config import Settings, get_settings


def ensure_storage_dirs(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    Path(cfg.storage_root).mkdir(parents=True, exist_ok=True)
    Path(cfg.uploads_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.artifacts_dir).mkdir(parents=True, exist_ok=True)


def detect_file_type(filename: str) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".geojson") or lower_name.endswith(".json"):
        return "geojson"
    if lower_name.endswith(".zip"):
        return "shp_zip"
    return "other"


def write_upload_file(session_id: str, upload: UploadFile, settings: Settings | None = None) -> tuple[str, int, str]:
    cfg = settings or get_settings()
    uploads_dir = Path(cfg.uploads_dir) / session_id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    target_path = uploads_dir / upload.filename
    content = upload.file.read()
    target_path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    return str(target_path), len(content), checksum


def build_artifact_path(task_id: str, filename: str, settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    artifact_dir = Path(cfg.artifacts_dir) / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return str(artifact_dir / filename)


def file_metadata(path: str) -> tuple[int, str]:
    content = Path(path).read_bytes()
    return os.path.getsize(path), hashlib.sha256(content).hexdigest()

