from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import create_message
from packages.schemas.common import ErrorResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse

router = APIRouter(tags=["messages"])


@router.post(
    "/messages",
    response_model=MessageCreateResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def create_message_endpoint(
    payload: MessageCreateRequest,
    db: Session = Depends(get_db),
) -> MessageCreateResponse:
    return create_message(db=db, payload=payload)
