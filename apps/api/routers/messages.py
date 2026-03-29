from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import create_message_and_task
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse

router = APIRouter(tags=["messages"])


@router.post("/messages", response_model=MessageCreateResponse)
def create_message_endpoint(
    payload: MessageCreateRequest,
    db: Session = Depends(get_db),
) -> MessageCreateResponse:
    return create_message_and_task(db=db, payload=payload)

