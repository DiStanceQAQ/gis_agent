from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.models import ArtifactRecord

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_id}")
def get_artifact_endpoint(artifact_id: str, db: Session = Depends(get_db)) -> FileResponse:
    artifact = db.get(ArtifactRecord, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return FileResponse(
        artifact.storage_key,
        media_type=artifact.mime_type,
        filename=artifact.storage_key.split("/")[-1],
    )

