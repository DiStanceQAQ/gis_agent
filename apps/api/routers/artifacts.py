from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.errors import AppError, ErrorCode
from packages.domain.models import ArtifactRecord
from packages.domain.services.storage import read_storage_bytes, storage_exists
from packages.schemas.common import ErrorResponse

router = APIRouter(tags=["artifacts"])


@router.get(
    "/artifacts/{artifact_id}",
    responses={404: {"model": ErrorResponse}},
)
def get_artifact_endpoint(artifact_id: str, db: Session = Depends(get_db)) -> Response:
    artifact = db.get(ArtifactRecord, artifact_id)
    if artifact is None:
        raise AppError.not_found(
            error_code=ErrorCode.ARTIFACT_NOT_FOUND,
            message="Artifact not found.",
            detail={"artifact_id": artifact_id},
        )
    if not storage_exists(artifact.storage_key):
        raise AppError.not_found(
            error_code=ErrorCode.ARTIFACT_STORAGE_MISSING,
            message="Artifact file is missing from storage.",
            detail={"artifact_id": artifact_id, "storage_key": artifact.storage_key},
        )
    filename = artifact.storage_key.split("/")[-1]
    disposition = "inline" if artifact.mime_type.startswith("image/") else "attachment"
    return Response(
        content=read_storage_bytes(artifact.storage_key),
        media_type=artifact.mime_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )
