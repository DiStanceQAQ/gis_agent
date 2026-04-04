from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import (
    approve_task_plan,
    get_task_detail,
    get_task_events,
    reject_task_plan,
    rerun_task,
    update_task_plan_draft,
)
from packages.schemas.common import ErrorResponse
from packages.schemas.task import (
    RerunTaskRequest,
    TaskDetailResponse,
    TaskEventsResponse,
    TaskPlanApproveRequest,
    TaskPlanPatchRequest,
    TaskPlanRejectRequest,
)

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
    response_model=TaskEventsResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_task_events_endpoint(
    task_id: str,
    since_id: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TaskEventsResponse:
    return get_task_events(db=db, task_id=task_id, since_id=since_id)


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


@router.patch(
    "/tasks/{task_id}/plan",
    response_model=TaskDetailResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def patch_task_plan_endpoint(
    task_id: str,
    payload: TaskPlanPatchRequest,
    db: Session = Depends(get_db),
) -> TaskDetailResponse:
    return update_task_plan_draft(db=db, task_id=task_id, plan=payload.operation_plan)


@router.post(
    "/tasks/{task_id}/approve",
    response_model=TaskDetailResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def approve_task_plan_endpoint(
    task_id: str,
    payload: TaskPlanApproveRequest,
    db: Session = Depends(get_db),
) -> TaskDetailResponse:
    return approve_task_plan(db=db, task_id=task_id, approved_version=payload.approved_version)


@router.post(
    "/tasks/{task_id}/reject",
    response_model=TaskDetailResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def reject_task_plan_endpoint(
    task_id: str,
    payload: TaskPlanRejectRequest,
    db: Session = Depends(get_db),
) -> TaskDetailResponse:
    return reject_task_plan(db=db, task_id=task_id, reason=payload.reason)
