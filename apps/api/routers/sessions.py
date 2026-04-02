from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import create_session, list_session_tasks
from packages.schemas.common import ErrorResponse
from packages.schemas.session import SessionResponse, SessionTasksResponse

router = APIRouter(tags=["sessions"])


@router.post("/sessions", response_model=SessionResponse)
def create_session_endpoint(db: Session = Depends(get_db)) -> SessionResponse:
    return create_session(db)


@router.get(
    "/sessions/{session_id}/tasks",
    response_model=SessionTasksResponse,
    responses={404: {"model": ErrorResponse}},
)
def list_session_tasks_endpoint(
    session_id: str,
    limit: int = Query(default=8, ge=1, le=20),
    db: Session = Depends(get_db),
) -> SessionTasksResponse:
    return list_session_tasks(db=db, session_id=session_id, limit=limit)
