from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import create_uploaded_file
from packages.schemas.file import UploadedFileResponse

router = APIRouter(tags=["files"])


@router.post("/files", response_model=UploadedFileResponse)
def upload_file_endpoint(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> UploadedFileResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="File must have a name.")
    return create_uploaded_file(db=db, session_id=session_id, upload=file)

