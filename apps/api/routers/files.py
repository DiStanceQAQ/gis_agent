from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.orchestrator import create_uploaded_file
from packages.schemas.common import ErrorResponse
from packages.schemas.file import UploadedFileResponse

router = APIRouter(tags=["files"])


@router.post(
    "/files",
    response_model=UploadedFileResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def upload_file_endpoint(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> UploadedFileResponse:
    if not file.filename:
        raise AppError.bad_request(
            error_code=ErrorCode.FILE_NAME_REQUIRED,
            message="File must have a name.",
        )
    return create_uploaded_file(db=db, session_id=session_id, upload=file)
