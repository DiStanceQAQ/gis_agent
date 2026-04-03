from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import ArtifactRecord, TaskRunRecord
from packages.domain.services.catalog import persist_candidates, search_candidates
from packages.domain.services.explanation import ExplanationContext, build_methods_text, build_summary_text
from packages.domain.services.processing_pipeline import run_processing_pipeline
from packages.domain.services.planner import (
    ensure_task_plan,
    set_task_plan_step_status,
)
from packages.domain.services.qc import evaluate_candidate_qc
from packages.domain.services.recommendation import build_recommendation
from packages.domain.services.storage import build_artifact_path, persist_artifact_file
from packages.domain.services.task_events import append_task_event
from packages.domain.services.task_state import (
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SUCCESS,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_step_status,
    set_task_status,
)
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail or {}


@dataclass
class PipelineExecutionContext:
    parsed_spec: ParsedTaskSpec
    bbox: list[float] | None = None
    candidates: list[object] = field(default_factory=list)
    candidate_qc: object | None = None
    recommendation: dict[str, object] | None = None
    pipeline_outputs: dict[str, object] | None = None


def _require_task_bbox(task: TaskRunRecord) -> list[float]:
    if task.aoi and task.aoi.bbox_bounds_json:
        return [float(value) for value in task.aoi.bbox_bounds_json]
    raise ValueError("Task AOI was not normalized; refusing to run pipeline without a normalized AOI bbox.")


def _tool_plan_task(db: Session, task: TaskRunRecord, context: PipelineExecutionContext) -> dict[str, object]:
    task.plan_json = ensure_task_plan(task.plan_json, context.parsed_spec, task_id=task.id).model_dump()
    db.flush()
    return {
        "plan_status": (task.plan_json or {}).get("status"),
        "objective": (task.plan_json or {}).get("objective"),
        "tool_count": len((task.plan_json or {}).get("steps", [])),
    }


def _tool_normalize_aoi(db: Session, task: TaskRunRecord, context: PipelineExecutionContext) -> dict[str, object]:
    del db
    bbox = _require_task_bbox(task)
    context.bbox = bbox
    return {
        "bbox": bbox,
        "aoi_name": task.aoi.name if task.aoi else None,
        "aoi_area_km2": task.aoi.area_km2 if task.aoi else None,
    }


def _tool_search_candidates(db: Session, task: TaskRunRecord, context: PipelineExecutionContext) -> dict[str, object]:
    candidates = search_candidates(context.parsed_spec, task.aoi)
    candidate_qc = evaluate_candidate_qc(candidates)
    persist_candidates(db, task=task, candidates=candidates)
    context.candidates = candidates
    context.candidate_qc = candidate_qc

    detail = {
        "candidate_count": len(candidates),
        "collections": [candidate.collection_id for candidate in candidates],
        "qc": candidate_qc.to_dict(),
    }
    if candidate_qc.status == "warn":
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_qc_warning",
            step_name="search_candidates",
            status=task.status,
            detail=candidate_qc.to_dict(),
        )
    return detail


def _tool_recommend_dataset(db: Session, task: TaskRunRecord, context: PipelineExecutionContext) -> dict[str, object]:
    recommendation = build_recommendation(
        context.candidates,
        user_priority=context.parsed_spec.user_priority,
        requested_dataset=context.parsed_spec.requested_dataset,
        task_id=task.id,
    )
    context.recommendation = recommendation
    if recommendation.get("error_code"):
        task.recommendation_json = recommendation
        db.flush()
        raise AgentRuntimeError(
            error_code=str(recommendation["error_code"]),
            message=str(recommendation.get("error_message") or recommendation.get("reason") or "recommendation failed"),
            detail={
                "stage": "recommend_dataset",
                "recommendation_reason": recommendation.get("reason"),
            },
        )
    task.selected_dataset = recommendation["primary_dataset"]
    task.recommendation_json = recommendation
    db.flush()
    return recommendation


def _tool_run_processing_pipeline(
    db: Session,
    task: TaskRunRecord,
    context: PipelineExecutionContext,
) -> dict[str, object]:
    del db
    operation_plan = ((task.plan_json or {}).get("operation_plan") or {})
    plan_nodes = list(operation_plan.get("nodes") or [])

    working_dir = Path(build_artifact_path(task.id, "pipeline_tmp")).parent
    pipeline_outputs = run_processing_pipeline(
        task_id=task.id,
        plan_nodes=plan_nodes,
        working_dir=working_dir,
    )
    context.pipeline_outputs = pipeline_outputs
    return {
        "mode": str(pipeline_outputs.get("mode") or "operation_plan"),
        "artifact_count": len(pipeline_outputs.get("artifacts") or []),
    }


def _tool_generate_outputs(
    db: Session,
    task: TaskRunRecord,
    context: PipelineExecutionContext,
    *,
    start: float,
) -> dict[str, object]:
    if context.pipeline_outputs is None:
        raise RuntimeError("Pipeline outputs missing before generate_outputs.")

    pipeline_outputs = context.pipeline_outputs
    tif_path = str(pipeline_outputs["tif_path"])
    png_path = str(pipeline_outputs["png_path"])
    methods_path = build_artifact_path(task.id, "methods.md")
    summary_path = build_artifact_path(task.id, "summary.md")

    artifact_specs = [
        ("png_map", png_path, "image/png"),
        ("geotiff", tif_path, "image/tiff"),
        ("methods_md", methods_path, "text/markdown"),
        ("summary_md", summary_path, "text/markdown"),
    ]
    explanation_context = ExplanationContext(
        dataset_name=task.selected_dataset or "unknown",
        mode=str(pipeline_outputs["mode"]),
        aoi_input=task.task_spec.aoi_input if task.task_spec else None,
        requested_time_range=task.requested_time_range,
        actual_time_range=pipeline_outputs["actual_time_range"],
        preferred_output=task.task_spec.preferred_output if task.task_spec else [],
        output_artifact_types=[artifact_type for artifact_type, _, _ in artifact_specs],
        item_count=len(pipeline_outputs["selected_item_ids"]),
        valid_pixel_ratio=(
            float(pipeline_outputs["valid_pixel_ratio"])
            if pipeline_outputs["valid_pixel_ratio"] is not None
            else None
        ),
        ndvi_min=float(pipeline_outputs["ndvi_min"]) if pipeline_outputs.get("ndvi_min") is not None else None,
        ndvi_max=float(pipeline_outputs["ndvi_max"]) if pipeline_outputs.get("ndvi_max") is not None else None,
        ndvi_mean=float(pipeline_outputs["ndvi_mean"]) if pipeline_outputs.get("ndvi_mean") is not None else None,
        output_width=(
            int(pipeline_outputs["output_width"])
            if pipeline_outputs.get("output_width") is not None
            else None
        ),
        output_height=(
            int(pipeline_outputs["output_height"])
            if pipeline_outputs.get("output_height") is not None
            else None
        ),
        output_crs=str(pipeline_outputs["output_crs"]) if pipeline_outputs.get("output_crs") else None,
        recommendation_reason=(task.recommendation_json or {}).get("reason"),
        recommendation_risk_note=(task.recommendation_json or {}).get("risk_note"),
        fallback_used=task.fallback_used,
    )
    methods_text = build_methods_text(explanation_context)
    summary_text = build_summary_text(explanation_context)
    Path(methods_path).write_text(methods_text, encoding="utf-8")
    Path(summary_path).write_text(summary_text, encoding="utf-8")

    for artifact in list(task.artifacts):
        db.delete(artifact)
    db.flush()

    for artifact_type, path, mime in artifact_specs:
        storage_key, size_bytes, checksum = persist_artifact_file(
            task.id,
            Path(path).name,
            path,
            content_type=mime,
        )
        db.add(
            ArtifactRecord(
                id=make_id("art"),
                task_id=task.id,
                artifact_type=artifact_type,
                storage_key=storage_key,
                mime_type=mime,
                size_bytes=size_bytes,
                checksum=checksum,
                metadata_json={"mode": pipeline_outputs["mode"]},
            )
        )

    task.methods_text = methods_text
    task.result_summary_text = summary_text
    task.actual_time_range = pipeline_outputs["actual_time_range"]
    task.duration_seconds = int(perf_counter() - start)
    db.flush()

    return {
        "artifact_types": [artifact_type for artifact_type, _, _ in artifact_specs],
        "duration_seconds": task.duration_seconds,
        "mode": pipeline_outputs["mode"],
    }


def maybe_skip_runtime_for_clarification(
    db: Session,
    task: TaskRunRecord,
    parsed_spec: ParsedTaskSpec,
) -> bool:
    if task.status != TASK_STATUS_WAITING_CLARIFICATION and not parsed_spec.need_confirmation:
        return False

    missing_fields = list(parsed_spec.missing_fields)
    detail = {
        "reason": "task_needs_clarification",
        "missing_fields": missing_fields,
    }
    if task.status != TASK_STATUS_WAITING_CLARIFICATION:
        set_task_status(
            db,
            task,
            TASK_STATUS_WAITING_CLARIFICATION,
            current_step="parse_task",
            event_type="task_waiting_clarification",
            detail={"missing_fields": missing_fields},
        )

    append_task_event(
        db,
        task_id=task.id,
        event_type="task_runtime_skipped",
        step_name="parse_task",
        status=task.status,
        detail=detail,
    )
    db.commit()
    logger.info("graph.runtime.skipped_for_clarification task_id=%s", task.id)
    return True


def check_runtime_limits(
    *,
    start: float,
    step_count: int,
    tool_calls: int,
) -> None:
    settings = get_settings()
    agent_max_steps = int(getattr(settings, "agent_max_steps", 12))
    agent_max_tool_calls = int(getattr(settings, "agent_max_tool_calls", 24))
    runtime_timeout_seconds = int(getattr(settings, "agent_runtime_timeout_seconds", 900))

    if step_count > agent_max_steps:
        raise AgentRuntimeError(
            error_code=ErrorCode.TASK_RUNTIME_MAX_STEPS_EXCEEDED,
            message="Task plan exceeds configured maximum step count.",
            detail={"step_count": step_count, "agent_max_steps": agent_max_steps},
        )
    if tool_calls >= agent_max_tool_calls:
        raise AgentRuntimeError(
            error_code=ErrorCode.TASK_RUNTIME_MAX_TOOL_CALLS_EXCEEDED,
            message="Task execution exceeded maximum tool call count.",
            detail={
                "tool_calls": tool_calls,
                "agent_max_tool_calls": agent_max_tool_calls,
            },
        )
    elapsed = int(perf_counter() - start)
    if runtime_timeout_seconds > 0 and elapsed > runtime_timeout_seconds:
        raise AgentRuntimeError(
            error_code=ErrorCode.TASK_RUNTIME_TIMEOUT,
            message="Task runtime exceeded configured timeout.",
            detail={
                "duration_seconds": elapsed,
                "agent_runtime_timeout_seconds": runtime_timeout_seconds,
            },
        )


def map_runtime_error(exc: Exception) -> tuple[str, dict[str, object]]:
    if isinstance(exc, AgentRuntimeError):
        return exc.error_code, exc.detail

    if isinstance(exc, ValueError) and "Invalid task status transition" in str(exc):
        return (
            ErrorCode.TASK_RUNTIME_INVALID_STATE_TRANSITION,
            {"exception_type": "ValueError", "transition_error": str(exc)},
        )

    return (
        ErrorCode.TASK_RUNTIME_FAILED,
        {"exception_type": type(exc).__name__},
    )


def mark_current_step_failed(
    db: Session,
    task: TaskRunRecord,
    *,
    detail: dict[str, object],
) -> None:
    current_step = task.current_step
    if not current_step:
        return

    step = next((item for item in task.steps if item.step_name == current_step), None)
    if step is None:
        return

    if step.status not in {STEP_STATUS_PENDING, STEP_STATUS_RUNNING}:
        return

    set_step_status(
        db,
        task,
        current_step,
        STEP_STATUS_FAILED,
        detail=detail,
    )
    task.plan_json = set_task_plan_step_status(
        task.plan_json,
        step_name=current_step,
        status=STEP_STATUS_FAILED,
        detail=detail,
    )


def execute_tool_step(
    db: Session,
    task: TaskRunRecord,
    context: PipelineExecutionContext,
    *,
    step_name: str,
    tool_name: str,
    title: str,
    handler,
    start: float,
) -> None:
    set_step_status(db, task, step_name, STEP_STATUS_RUNNING)
    task.plan_json = set_task_plan_step_status(
        task.plan_json,
        step_name=step_name,
        status=STEP_STATUS_RUNNING,
        detail={"tool_name": tool_name, "title": title},
    )
    append_task_event(
        db,
        task_id=task.id,
        event_type="tool_execution_started",
        step_name=step_name,
        status=task.status,
        detail={"tool_name": tool_name, "title": title},
    )
    db.commit()

    if step_name == "generate_outputs":
        detail = handler(db, task, context, start=start)
    else:
        detail = handler(db, task, context)

    set_step_status(db, task, step_name, STEP_STATUS_SUCCESS, detail=detail)
    task.plan_json = set_task_plan_step_status(
        task.plan_json,
        step_name=step_name,
        status=STEP_STATUS_SUCCESS,
        detail=detail,
    )
    append_task_event(
        db,
        task_id=task.id,
        event_type="tool_execution_completed",
        step_name=step_name,
        status=task.status,
        detail={"tool_name": tool_name, "title": title},
    )
    db.commit()


TOOL_REGISTRY = {
    "plan_task": _tool_plan_task,
    "normalize_aoi": _tool_normalize_aoi,
    "search_candidates": _tool_search_candidates,
    "recommend_dataset": _tool_recommend_dataset,
    "run_processing_pipeline": _tool_run_processing_pipeline,
    "generate_outputs": _tool_generate_outputs,
}
