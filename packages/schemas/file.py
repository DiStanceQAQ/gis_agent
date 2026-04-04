from datetime import datetime
from typing import Any

from packages.schemas.common import ORMModel


class UploadedFileResponse(ORMModel):
    file_id: str
    file_type: str
    storage_key: str
    original_name: str


class ArtifactMetadataResponse(ORMModel):
    artifact_id: str
    artifact_type: str
    mime_type: str
    size_bytes: int
    checksum: str
    created_at: datetime
    metadata: dict[str, Any] | None = None
    download_url: str
