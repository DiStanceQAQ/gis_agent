from fastapi import APIRouter, Depends, File, Form, Path, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.models import UploadedFileRecord
from packages.domain.services.file_preview import (
    build_uploaded_file_preview,
    read_uploaded_file_preview_image,
)
from packages.domain.services.orchestrator import create_uploaded_file
from packages.schemas.common import ErrorResponse
from packages.schemas.file import UploadedFilePreviewResponse, UploadedFileResponse

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


@router.get(
    "/files/{file_id}/preview",
    response_model=UploadedFilePreviewResponse,
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
def get_uploaded_file_preview_endpoint(
    file_id: str = Path(..., description="Uploaded file id"),
    db: Session = Depends(get_db),
) -> UploadedFilePreviewResponse:
    uploaded_file = db.get(UploadedFileRecord, file_id)
    if uploaded_file is None:
        raise AppError.not_found(
            error_code=ErrorCode.FILE_NOT_FOUND,
            message="Uploaded file not found.",
            detail={"file_id": file_id},
        )

    preview = build_uploaded_file_preview(uploaded_file)
    image_url = preview.image_url
    if preview.preview_type == "raster_image" and not image_url:
        settings = get_settings()
        image_url = f"{settings.api_prefix}/files/{file_id}/preview-image"

    return UploadedFilePreviewResponse(
        file_id=uploaded_file.id,
        file_type=uploaded_file.file_type,
        preview_type=preview.preview_type,
        bbox_bounds=preview.bbox_bounds,
        feature_count=preview.feature_count,
        geojson=preview.geojson,
        image_url=image_url,
        message=preview.message,
    )


@router.get(
    "/files/{file_id}/preview-image",
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
def get_uploaded_file_preview_image_endpoint(
    file_id: str = Path(..., description="Uploaded file id"),
    db: Session = Depends(get_db),
) -> Response:
    uploaded_file = db.get(UploadedFileRecord, file_id)
    if uploaded_file is None:
        raise AppError.not_found(
            error_code=ErrorCode.FILE_NOT_FOUND,
            message="Uploaded file not found.",
            detail={"file_id": file_id},
        )

    return Response(
        content=read_uploaded_file_preview_image(uploaded_file),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )
