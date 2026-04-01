from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import get_task_detail, rerun_task
from packages.schemas.common import ErrorResponse
from packages.schemas.task import RerunTaskRequest, TaskDetailResponse

router = APIRouter(tags=["tasks"])


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_task_endpoint(task_id: str, db: Session = Depends(get_db)) -> TaskDetailResponse:
    return get_task_detail(db=db, task_id=task_id)


@router.get(
    "/tasks/{task_id}/events",
    responses={404: {"model": ErrorResponse}},
)
def get_task_events_endpoint(task_id: str, db: Session = Depends(get_db)) -> dict:
    detail = get_task_detail(db=db, task_id=task_id)
    return {
        "task_id": detail.task_id,
        "status": detail.status,
        "events": [step.model_dump() for step in detail.steps],
    }


@router.post(
    "/tasks/{task_id}/rerun",
    response_model=TaskDetailResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def rerun_task_endpoint(
    task_id: str,
    payload: RerunTaskRequest,
    db: Session = Depends(get_db),
) -> TaskDetailResponse:
    return rerun_task(db=db, task_id=task_id, override=payload.override)
