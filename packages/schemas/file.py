from datetime import datetime
from typing import Any

from packages.schemas.common import ORMModel


class UploadedFileResponse(ORMModel):
    file_id: str
    file_type: str
    storage_key: str
    original_name: str
    size_bytes: int


class UploadedFilePreviewResponse(ORMModel):
    file_id: str
    file_type: str
    preview_type: str
    bbox_bounds: list[float] | None = None
    feature_count: int | None = None
    geojson: dict[str, Any] | None = None
    image_url: str | None = None
    message: str | None = None


class ArtifactMetadataResponse(ORMModel):
    artifact_id: str
    artifact_type: str
    mime_type: str
    size_bytes: int
    checksum: str
    created_at: datetime
    metadata: dict[str, Any] | None = None
    download_url: str
