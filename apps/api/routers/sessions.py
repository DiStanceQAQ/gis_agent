from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import create_session
from packages.schemas.session import SessionResponse

router = APIRouter(tags=["sessions"])


@router.post("/sessions", response_model=SessionResponse)
def create_session_endpoint(db: Session = Depends(get_db)) -> SessionResponse:
    return create_session(db)
