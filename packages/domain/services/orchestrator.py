from __future__ import annotations

from copy import deepcopy

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session, selectinload

from apps.worker.tasks import run_mock_task_async
from packages.domain.config import get_settings
from packages.domain.models import (
    ArtifactRecord,
    MessageRecord,
    SessionRecord,
    TaskRunRecord,
    TaskSpecRecord,
    UploadedFileRecord,
)
from packages.domain.services.mock_pipeline import run_mock_task
from packages.domain.services.parser import parse_task_message
from packages.domain.services.storage import detect_file_type, write_upload_file
from packages.domain.utils import make_id, merge_dicts
from packages.schemas.file import UploadedFileResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse
from packages.schemas.session import SessionResponse
from packages.schemas.task import (
    ArtifactResponse,
    CandidateResponse,
    ParsedTaskSpec,
    RecommendationResponse,
    TaskDetailResponse,
    TaskStepResponse,
)


def create_session(db: Session) -> SessionResponse:
    record = SessionRecord(id=make_id("ses"), status="active")
    db.add(record)
    db.commit()
    return SessionResponse(session_id=record.id, status=record.status)


def create_uploaded_file(db: Session, session_id: str, upload: UploadFile) -> UploadedFileResponse:
    session = db.get(SessionRecord, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    path, size_bytes, checksum = write_upload_file(session_id=session_id, upload=upload)
    file_type = detect_file_type(upload.filename or "")
    record = UploadedFileRecord(
        id=make_id("file"),
        session_id=session_id,
        original_name=upload.filename or "uploaded_file",
        file_type=file_type,
        storage_key=path,
        size_bytes=size_bytes,
        checksum=checksum,
    )
    db.add(record)
    db.commit()
    return UploadedFileResponse(
        file_id=record.id,
        file_type=record.file_type,
        storage_key=record.storage_key,
        original_name=record.original_name,
    )


def _queue_or_run(task_id: str) -> None:
    settings = get_settings()
    if settings.execution_mode == "celery_mock":
        run_mock_task_async.delay(task_id)
        return
    run_mock_task(task_id)


def _build_task(
    db: Session,
    session_id: str,
    user_message_id: str,
    parsed: ParsedTaskSpec,
    parent_task_id: str | None = None,
) -> TaskRunRecord:
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session_id,
        parent_task_id=parent_task_id,
        user_message_id=user_message_id,
        status="waiting_clarification" if parsed.need_confirmation else "queued",
        current_step="parse_task",
        analysis_type=parsed.analysis_type,
        requested_time_range=parsed.time_range,
    )
    db.add(task)
    db.flush()

    task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input=parsed.aoi_input,
        aoi_source_type=parsed.aoi_source_type,
        preferred_output=parsed.preferred_output,
        user_priority=parsed.user_priority,
        need_confirmation=parsed.need_confirmation,
        raw_spec_json=parsed.model_dump(),
    )
    db.add(task_spec)
    db.commit()
    db.refresh(task)
    return task


def create_message_and_task(db: Session, payload: MessageCreateRequest) -> MessageCreateResponse:
    session = db.get(SessionRecord, payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.title is None:
        session.title = payload.content[:60]

    files = []
    if payload.file_ids:
        files = (
            db.query(UploadedFileRecord)
            .filter(UploadedFileRecord.id.in_(payload.file_ids))
            .all()
        )

    message = MessageRecord(
        id=make_id("msg"),
        session_id=payload.session_id,
        role="user",
        content=payload.content,
    )
    db.add(message)
    db.commit()

    parsed = parse_task_message(payload.content, has_upload=bool(files))
    task = _build_task(
        db=db,
        session_id=payload.session_id,
        user_message_id=message.id,
        parsed=parsed,
    )

    message.linked_task_id = task.id
    db.commit()

    if not parsed.need_confirmation:
        _queue_or_run(task.id)

    return MessageCreateResponse(
        message_id=message.id,
        task_id=task.id,
        task_status=task.status,
        need_clarification=parsed.need_confirmation,
        missing_fields=parsed.missing_fields,
    )


def _load_task(db: Session, task_id: str) -> TaskRunRecord:
    task = (
        db.query(TaskRunRecord)
        .options(
            selectinload(TaskRunRecord.task_spec),
            selectinload(TaskRunRecord.aoi),
            selectinload(TaskRunRecord.candidates),
            selectinload(TaskRunRecord.steps),
            selectinload(TaskRunRecord.artifacts),
        )
        .filter(TaskRunRecord.id == task_id)
        .first()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def get_task_detail(db: Session, task_id: str) -> TaskDetailResponse:
    settings = get_settings()
    task = _load_task(db, task_id)

    recommendation = None
    if task.recommendation_json:
        recommendation = RecommendationResponse(**task.recommendation_json)

    task_spec = task.task_spec.raw_spec_json if task.task_spec else None

    artifacts = [
        ArtifactResponse(
            artifact_id=artifact.id,
            artifact_type=artifact.artifact_type,
            mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes,
            download_url=f"{settings.api_prefix}/artifacts/{artifact.id}",
        )
        for artifact in sorted(task.artifacts, key=lambda item: item.created_at)
    ]

    png_preview_url = next(
        (artifact.download_url for artifact in artifacts if artifact.artifact_type == "png_map"),
        None,
    )

    return TaskDetailResponse(
        task_id=task.id,
        status=task.status,
        current_step=task.current_step,
        analysis_type=task.analysis_type,
        task_spec=task_spec,
        recommendation=recommendation,
        candidates=[
            CandidateResponse(
                dataset_name=c.dataset_name,
                scene_count=c.scene_count,
                coverage_ratio=c.coverage_ratio,
                effective_pixel_ratio_estimate=c.effective_pixel_ratio_estimate,
                spatial_resolution=c.spatial_resolution,
                temporal_density_note=c.temporal_density_note,
                suitability_score=c.suitability_score,
                recommendation_rank=c.recommendation_rank,
            )
            for c in sorted(task.candidates, key=lambda item: item.recommendation_rank)
        ],
        steps=[
            TaskStepResponse(
                step_name=step.step_name,
                status=step.status,
                started_at=step.started_at,
                ended_at=step.ended_at,
                detail=step.detail_json,
            )
            for step in task.steps
        ],
        artifacts=artifacts,
        summary_text=task.result_summary_text,
        methods_text=task.methods_text,
        png_preview_url=png_preview_url,
        requested_time_range=task.requested_time_range,
        actual_time_range=task.actual_time_range,
        error_message=task.error_message,
    )


def rerun_task(db: Session, task_id: str, override: dict | None) -> TaskDetailResponse:
    original_task = _load_task(db, task_id)
    if original_task.task_spec is None:
        raise HTTPException(status_code=400, detail="Original task has no task spec.")

    original_spec = deepcopy(original_task.task_spec.raw_spec_json)
    merged = merge_dicts(original_spec, override or {})
    parsed = ParsedTaskSpec(**merged)

    message = MessageRecord(
        id=make_id("msg"),
        session_id=original_task.session_id,
        role="user",
        content="rerun task",
        linked_task_id=None,
    )
    db.add(message)
    db.commit()

    task = _build_task(
        db=db,
        session_id=original_task.session_id,
        user_message_id=message.id,
        parsed=parsed,
        parent_task_id=original_task.id,
    )
    message.linked_task_id = task.id
    db.commit()

    if not parsed.need_confirmation:
        _queue_or_run(task.id)
    return get_task_detail(db=db, task_id=task.id)

