from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import ArtifactRecord, TaskRunRecord
from packages.domain.services.llm_client import LLMClient, LLMClientError
from packages.domain.services.catalog import (
    build_candidate_summaries,
    persist_candidates,
    search_candidates,
)
from packages.domain.services.explanation import (
    ExplanationContext,
    build_methods_text,
    build_summary_text,
)
from packages.domain.services.llm_call_logs import write_llm_call_log
from packages.domain.services.processing_pipeline import run_processing_pipeline
from packages.domain.services.planner import (
    ensure_task_plan,
    set_task_plan_step_status,
)
from packages.domain.services.qc import evaluate_candidate_qc
from packages.domain.services.recommendation import build_recommendation
from packages.domain.services.storage import (
    build_artifact_path,
    collect_artifact_metadata,
    infer_artifact_mime_type,
    persist_artifact_file,
)
from packages.domain.services.task_events import append_task_event
from packages.domain.services.task_state import (
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SKIPPED,
    STEP_STATUS_SUCCESS,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_step_status,
    set_task_status,
)
from packages.domain.utils import make_id
from packages.schemas.agent import LLMReactStepDecision
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


_SHAPEFILE_COMPONENT_SUFFIXES = (
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".sbn",
    ".sbx",
    ".qix",
    ".ain",
    ".aih",
    ".ixs",
    ".mxs",
)


def _prepare_artifact_for_storage(
    *,
    artifact_type: str,
    source_path: str,
    mime_type: str,
) -> tuple[str, str, str, dict[str, object]]:
    path = Path(source_path)
    if artifact_type != "shapefile" or path.suffix.lower() != ".shp":
        return source_path, mime_type, source_path, {}

    components = [path.with_suffix(suffix) for suffix in _SHAPEFILE_COMPONENT_SUFFIXES]
    existing = [item for item in components if item.exists()]
    if not existing:
        return source_path, mime_type, source_path, {}

    zip_path = path.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in existing:
            archive.write(item, arcname=item.name)

    component_names = [item.name for item in existing]
    extra_metadata = {
        "packaged_format": "shapefile_zip",
        "packaged_components": component_names,
        "metadata_source": path.name,
    }
    return str(zip_path), "application/zip", str(path), extra_metadata


def _require_task_bbox(task: TaskRunRecord) -> list[float]:
    if task.aoi and task.aoi.bbox_bounds_json:
        return [float(value) for value in task.aoi.bbox_bounds_json]
    raise ValueError(
        "Task AOI was not normalized; refusing to run pipeline without a normalized AOI bbox."
    )


def _tool_plan_task(
    db: Session, task: TaskRunRecord, context: PipelineExecutionContext
) -> dict[str, object]:
    task.plan_json = ensure_task_plan(
        task.plan_json, context.parsed_spec, task_id=task.id
    ).model_dump()
    db.flush()
    return {
        "plan_status": (task.plan_json or {}).get("status"),
        "objective": (task.plan_json or {}).get("objective"),
        "tool_count": len((task.plan_json or {}).get("steps", [])),
    }


def _tool_normalize_aoi(
    db: Session, task: TaskRunRecord, context: PipelineExecutionContext
) -> dict[str, object]:
    del db
    bbox = _require_task_bbox(task)
    context.bbox = bbox
    return {
        "bbox": bbox,
        "aoi_name": task.aoi.name if task.aoi else None,
        "aoi_area_km2": task.aoi.area_km2 if task.aoi else None,
    }


def _tool_search_candidates(
    db: Session, task: TaskRunRecord, context: PipelineExecutionContext
) -> dict[str, object]:
    settings = get_settings()
    if bool(getattr(settings, "local_files_only_mode", False)):
        context.candidates = []
        context.candidate_qc = None
        detail = {
            "candidate_count": 0,
            "collections": [],
            "qc": {
                "status": "skipped",
                "issues": [],
                "reason": "local_files_only_mode",
            },
            "candidate_summaries": [],
            "local_files_only_mode": True,
        }
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_catalog_candidates_skipped",
            step_name="search_candidates",
            status=task.status,
            detail=detail,
        )
        return detail

    candidates = search_candidates(context.parsed_spec, task.aoi)
    candidate_qc = evaluate_candidate_qc(candidates)
    persist_candidates(db, task=task, candidates=candidates)
    context.candidates = candidates
    context.candidate_qc = candidate_qc
    candidate_summaries = build_candidate_summaries(candidates)
    qc_detail = candidate_qc.to_dict()

    detail = {
        "candidate_count": len(candidates),
        "collections": [candidate.collection_id for candidate in candidates],
        "qc": qc_detail,
        "candidate_summaries": candidate_summaries,
    }
    append_task_event(
        db,
        task_id=task.id,
        event_type="task_catalog_candidates_ready",
        step_name="search_candidates",
        status=task.status,
        detail=detail,
    )
    if candidate_qc.status == "warn":
        append_task_event(
            db,
            task_id=task.id,
            event_type="task_qc_warning",
            step_name="search_candidates",
            status=task.status,
            detail={
                "qc": qc_detail,
                "candidate_summaries": candidate_summaries,
            },
        )
    return detail


def _tool_recommend_dataset(
    db: Session, task: TaskRunRecord, context: PipelineExecutionContext
) -> dict[str, object]:
    settings = get_settings()
    if bool(getattr(settings, "local_files_only_mode", False)):
        recommendation = {
            "primary_dataset": "local_file",
            "backup_dataset": None,
            "scores": {"local_file": 1.0},
            "reason": "本地文件模式下直接使用用户指定或上传的文件，不执行目录推荐。",
            "risk_note": None,
            "confidence": 1.0,
        }
        context.recommendation = recommendation
        task.selected_dataset = recommendation["primary_dataset"]
        task.recommendation_json = recommendation
        db.flush()
        return recommendation

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
            message=str(
                recommendation.get("error_message")
                or recommendation.get("reason")
                or "recommendation failed"
            ),
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
    if not task.selected_dataset:
        task.selected_dataset = "local_file"
    operation_plan = (task.plan_json or {}).get("operation_plan") or {}
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
    tif_path_raw = pipeline_outputs.get("tif_path")
    png_path_raw = pipeline_outputs.get("png_path")
    tif_path = str(tif_path_raw) if tif_path_raw else None
    png_path = str(png_path_raw) if png_path_raw else None
    methods_path = build_artifact_path(task.id, "methods.md")
    summary_path = build_artifact_path(task.id, "summary.md")

    artifact_specs: list[dict[str, str | None]] = []
    seen_artifacts: set[tuple[str, str]] = set()

    def _append_artifact_spec(
        *,
        artifact_type: str,
        path: str,
        source_step: str | None,
        mime_type: str | None = None,
    ) -> None:
        artifact_key = (artifact_type, str(Path(path)))
        if artifact_key in seen_artifacts:
            return
        seen_artifacts.add(artifact_key)
        artifact_specs.append(
            {
                "artifact_type": artifact_type,
                "path": str(path),
                "source_step": source_step,
                "mime_type": mime_type
                or infer_artifact_mime_type(path, artifact_type=artifact_type),
            }
        )

    for artifact in pipeline_outputs.get("artifacts") or []:
        artifact_type = str(artifact.get("artifact_type") or "")
        artifact_path = str(artifact.get("path") or "")
        if not artifact_type or not artifact_path:
            continue
        _append_artifact_spec(
            artifact_type=artifact_type,
            path=artifact_path,
            source_step=str(artifact.get("source_step") or "run_processing_pipeline"),
        )

    if png_path and not any(item["artifact_type"] == "png_map" for item in artifact_specs):
        _append_artifact_spec(
            artifact_type="png_map",
            path=png_path,
            source_step="run_processing_pipeline",
            mime_type="image/png",
        )
    if tif_path and not any(item["artifact_type"] == "geotiff" for item in artifact_specs):
        _append_artifact_spec(
            artifact_type="geotiff",
            path=tif_path,
            source_step="run_processing_pipeline",
            mime_type="image/tiff",
        )

    if not artifact_specs:
        raise RuntimeError("No exportable artifacts were produced by operation plan execution.")

    _append_artifact_spec(
        artifact_type="methods_md",
        path=methods_path,
        source_step="generate_outputs",
        mime_type="text/markdown",
    )
    _append_artifact_spec(
        artifact_type="summary_md",
        path=summary_path,
        source_step="generate_outputs",
        mime_type="text/markdown",
    )
    explanation_context = ExplanationContext(
        dataset_name=task.selected_dataset or "unknown",
        mode=str(pipeline_outputs["mode"]),
        aoi_input=task.task_spec.aoi_input if task.task_spec else None,
        requested_time_range=task.requested_time_range,
        actual_time_range=pipeline_outputs["actual_time_range"],
        preferred_output=task.task_spec.preferred_output if task.task_spec else [],
        output_artifact_types=[str(item["artifact_type"]) for item in artifact_specs],
        item_count=len(pipeline_outputs["selected_item_ids"]),
        valid_pixel_ratio=(
            float(pipeline_outputs["valid_pixel_ratio"])
            if pipeline_outputs["valid_pixel_ratio"] is not None
            else None
        ),
        ndvi_min=float(pipeline_outputs["ndvi_min"])
        if pipeline_outputs.get("ndvi_min") is not None
        else None,
        ndvi_max=float(pipeline_outputs["ndvi_max"])
        if pipeline_outputs.get("ndvi_max") is not None
        else None,
        ndvi_mean=float(pipeline_outputs["ndvi_mean"])
        if pipeline_outputs.get("ndvi_mean") is not None
        else None,
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
        output_crs=str(pipeline_outputs["output_crs"])
        if pipeline_outputs.get("output_crs")
        else None,
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

    for item in artifact_specs:
        artifact_type = str(item["artifact_type"])
        path = str(item["path"])
        mime = str(item["mime_type"])
        source_step = str(item["source_step"]) if item["source_step"] else None

        publish_path, publish_mime, metadata_source_path, extra_metadata = (
            _prepare_artifact_for_storage(
                artifact_type=artifact_type,
                source_path=path,
                mime_type=mime,
            )
        )
        storage_key, size_bytes, checksum = persist_artifact_file(
            task.id,
            Path(publish_path).name,
            publish_path,
            content_type=publish_mime,
        )
        metadata = collect_artifact_metadata(
            metadata_source_path,
            artifact_type=artifact_type,
            source_step=source_step,
        )
        if extra_metadata:
            metadata.update(extra_metadata)
        metadata["mode"] = pipeline_outputs["mode"]
        db.add(
            ArtifactRecord(
                id=make_id("art"),
                task_id=task.id,
                artifact_type=artifact_type,
                storage_key=storage_key,
                mime_type=publish_mime,
                size_bytes=size_bytes,
                checksum=checksum,
                metadata_json=metadata,
            )
        )

    task.methods_text = methods_text
    task.result_summary_text = summary_text
    task.actual_time_range = pipeline_outputs["actual_time_range"]
    task.duration_seconds = int(perf_counter() - start)
    db.flush()

    return {
        "artifact_types": [str(item["artifact_type"]) for item in artifact_specs],
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


@lru_cache
def _load_step_react_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "react_step.md"
    return prompt_path.read_text(encoding="utf-8")


def _build_step_react_user_prompt(
    *,
    step_name: str,
    tool_name: str,
    title: str,
    context: PipelineExecutionContext,
) -> str:
    payload = {
        "task": "react_step_decision",
        "language": "zh-CN",
        "step": {
            "step_name": step_name,
            "tool_name": tool_name,
            "title": title,
        },
        "parsed_spec": context.parsed_spec.model_dump(),
        "runtime_context": {
            "candidate_count": len(context.candidates),
            "has_recommendation": context.recommendation is not None,
            "has_pipeline_outputs": context.pipeline_outputs is not None,
        },
        "constraints": {
            "allowed_function_name": tool_name,
            "allowed_decisions": ["continue", "skip", "fail"],
        },
        "required_fields": ["decision", "function_name", "arguments", "reasoning_summary"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_deterministic_step_react_decision(
    *,
    step_name: str,
    tool_name: str,
    title: str,
    context: PipelineExecutionContext,
) -> LLMReactStepDecision:
    parsed_dataset = context.parsed_spec.requested_dataset or "auto"
    return LLMReactStepDecision(
        decision="continue",
        function_name=tool_name,
        arguments={"step_name": step_name},
        reasoning_summary=(
            f"当前步骤 {step_name} 依赖已满足，继续调用 {tool_name}（{title}）推进流程；"
            f"数据源偏好={parsed_dataset}。"
        ),
    )


def _build_step_react_decision(
    *,
    db: Session | None,
    task: TaskRunRecord,
    step_name: str,
    tool_name: str,
    title: str,
    context: PipelineExecutionContext,
) -> LLMReactStepDecision:
    settings = get_settings()
    use_llm = bool(getattr(settings, "llm_step_react_enabled", True))
    fallback_enabled = bool(getattr(settings, "llm_step_react_legacy_fallback", True))
    schema_retries = max(0, int(getattr(settings, "llm_step_react_schema_retries", 1)))
    llm_base_url = getattr(settings, "llm_base_url", None)
    llm_model = getattr(settings, "llm_model", None)
    llm_temperature = float(getattr(settings, "llm_temperature", 0.2))
    llm_config_complete = bool(isinstance(llm_base_url, str) and llm_base_url.strip() and llm_model)

    if not use_llm:
        if db is not None:
            _write_react_step_log(
                db=db,
                task=task,
                step_name=step_name,
                tool_name=tool_name,
                status="success",
                latency_ms=0,
                error_message="llm_step_react_disabled",
            )
        return _build_deterministic_step_react_decision(
            step_name=step_name,
            tool_name=tool_name,
            title=title,
            context=context,
        )
    if not llm_config_complete:
        if db is not None:
            _write_react_step_log(
                db=db,
                task=task,
                step_name=step_name,
                tool_name=tool_name,
                status="success",
                latency_ms=0,
                error_message="llm_step_react_config_incomplete_fallback",
            )
        return _build_deterministic_step_react_decision(
            step_name=step_name,
            tool_name=tool_name,
            title=title,
            context=context,
        )

    system_prompt = _load_step_react_system_prompt()
    user_prompt = _build_step_react_user_prompt(
        step_name=step_name,
        tool_name=tool_name,
        title=title,
        context=context,
    )
    client = LLMClient(settings)
    attempt = 0
    last_validation_errors: list[dict[str, object]] = []

    while attempt <= schema_retries:
        try:
            response = client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=str(llm_model),
                temperature=llm_temperature,
                phase="react_step",
                task_id=task.id,
                db_session=db,
            )
        except LLMClientError as exc:
            if fallback_enabled:
                logger.warning(
                    "step_react.llm_failed step_name=%s detail=%s fallback=deterministic",
                    step_name,
                    exc.detail,
                )
                if db is not None:
                    _write_react_step_log(
                        db=db,
                        task=task,
                        step_name=step_name,
                        tool_name=tool_name,
                        status="success",
                        latency_ms=0,
                        error_message="llm_step_react_failed_fallback",
                    )
                return _build_deterministic_step_react_decision(
                    step_name=step_name,
                    tool_name=tool_name,
                    title=title,
                    context=context,
                )
            raise AgentRuntimeError(
                error_code=ErrorCode.TASK_RUNTIME_FAILED,
                message=f"Step ReAct LLM request failed: {step_name}",
                detail={"step_name": step_name, "llm_detail": exc.detail},
            ) from exc

        try:
            return LLMReactStepDecision.model_validate(response.content_json)
        except ValidationError as exc:
            last_validation_errors = [
                {"loc": list(item.get("loc", ())), "msg": item.get("msg"), "type": item.get("type")}
                for item in exc.errors()
            ]
            if attempt >= schema_retries:
                break
            attempt += 1
            user_prompt = json.dumps(
                {
                    "task": "repair_invalid_step_react_json",
                    "instructions": "上一轮输出未通过 schema 校验。请仅输出修复后的 JSON。",
                    "previous_json": response.content_json,
                    "validation_errors": last_validation_errors,
                    "allowed_function_name": tool_name,
                    "step_name": step_name,
                },
                ensure_ascii=False,
                indent=2,
            )

    if fallback_enabled:
        logger.warning(
            "step_react.schema_failed step_name=%s fallback=deterministic errors=%s",
            step_name,
            last_validation_errors,
        )
        if db is not None:
            _write_react_step_log(
                db=db,
                task=task,
                step_name=step_name,
                tool_name=tool_name,
                status="success",
                latency_ms=0,
                error_message="llm_step_react_schema_failed_fallback",
            )
        return _build_deterministic_step_react_decision(
            step_name=step_name,
            tool_name=tool_name,
            title=title,
            context=context,
        )

    raise AgentRuntimeError(
        error_code=ErrorCode.TASK_LLM_REACT_SCHEMA_VALIDATION_FAILED,
        message="Step ReAct decision failed schema validation.",
        detail={"step_name": step_name, "validation_errors": last_validation_errors},
    )


def _build_step_react_observation(detail: dict[str, object]) -> dict[str, object]:
    preview = {key: detail[key] for key in list(detail.keys())[:5]}
    return {
        "observation_keys": sorted(detail.keys()),
        "observation_preview": preview,
    }


def _write_react_step_log(
    *,
    db: Session,
    task: TaskRunRecord,
    step_name: str,
    tool_name: str,
    status: str,
    latency_ms: int,
    error_message: str | None = None,
) -> None:
    prompt_hash = hashlib.sha256(f"{step_name}:{tool_name}".encode("utf-8")).hexdigest()
    write_llm_call_log(
        task_id=task.id,
        phase="react_step",
        model_name="deterministic_step_react",
        request_id=None,
        prompt_hash=prompt_hash,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        latency_ms=max(0, latency_ms),
        status=status,
        error_message=error_message,
        db_session=db,
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
) -> bool:
    step_react_started = perf_counter()
    settings = get_settings()
    step_react_max_rounds = max(1, int(getattr(settings, "agent_step_react_max_rounds", 1)))
    step_react_timeout_seconds = int(getattr(settings, "agent_step_react_timeout_seconds", 30))
    for step_react_round in range(1, step_react_max_rounds + 1):
        if (
            step_react_timeout_seconds > 0
            and (perf_counter() - step_react_started) > step_react_timeout_seconds
        ):
            raise AgentRuntimeError(
                error_code=ErrorCode.TASK_RUNTIME_TIMEOUT,
                message=f"Step ReAct timed out before tool execution: {step_name}",
                detail={
                    "step_name": step_name,
                    "step_react_round": step_react_round,
                    "step_react_timeout_seconds": step_react_timeout_seconds,
                },
            )

        react_decision = _build_step_react_decision(
            db=db,
            task=task,
            step_name=step_name,
            tool_name=tool_name,
            title=title,
            context=context,
        )
        append_task_event(
            db,
            task_id=task.id,
            event_type="step_react_decision",
            step_name=step_name,
            status=task.status,
            detail={
                "decision": react_decision.decision,
                "reasoning_summary": react_decision.reasoning_summary,
                "function_name": react_decision.function_name,
                "arguments": react_decision.arguments,
                "step_react_round": step_react_round,
                "step_react_max_rounds": step_react_max_rounds,
            },
        )

        if react_decision.decision == "skip":
            skip_detail = {
                "decision": "skip",
                "reasoning_summary": react_decision.reasoning_summary,
                "step_react_round": step_react_round,
                "step_react_max_rounds": step_react_max_rounds,
            }
            set_step_status(db, task, step_name, STEP_STATUS_SKIPPED, detail=skip_detail)
            task.plan_json = set_task_plan_step_status(
                task.plan_json,
                step_name=step_name,
                status=STEP_STATUS_SKIPPED,
                detail=skip_detail,
            )
            append_task_event(
                db,
                task_id=task.id,
                event_type="step_skipped_by_react",
                step_name=step_name,
                status=task.status,
                detail=skip_detail,
            )
            append_task_event(
                db,
                task_id=task.id,
                event_type="step_react_observation",
                step_name=step_name,
                status=task.status,
                detail=_build_step_react_observation(skip_detail),
            )
            db.commit()
            return False

        if react_decision.decision == "fail":
            raise AgentRuntimeError(
                error_code=ErrorCode.TASK_RUNTIME_FAILED,
                message=f"ReAct decision marked step as failed: {step_name}",
                detail={
                    "step_name": step_name,
                    "decision": react_decision.decision,
                    "reasoning_summary": react_decision.reasoning_summary,
                    "step_react_round": step_react_round,
                    "step_react_max_rounds": step_react_max_rounds,
                },
            )

        if react_decision.function_name != tool_name:
            if step_react_round < step_react_max_rounds:
                append_task_event(
                    db,
                    task_id=task.id,
                    event_type="step_react_retry",
                    step_name=step_name,
                    status=task.status,
                    detail={
                        "reason": "function_name_mismatch",
                        "expected_function_name": tool_name,
                        "actual_function_name": react_decision.function_name,
                        "step_react_round": step_react_round,
                        "step_react_max_rounds": step_react_max_rounds,
                    },
                )
                db.commit()
                continue
            raise AgentRuntimeError(
                error_code=ErrorCode.TASK_RUNTIME_UNKNOWN_TOOL,
                message=f"ReAct selected unknown tool: {react_decision.function_name}",
                detail={
                    "step_name": step_name,
                    "tool_name": react_decision.function_name,
                    "step_react_round": step_react_round,
                    "step_react_max_rounds": step_react_max_rounds,
                },
            )

        append_task_event(
            db,
            task_id=task.id,
            event_type="function_call_requested",
            step_name=step_name,
            status=task.status,
            detail={
                "function_name": react_decision.function_name,
                "arguments": react_decision.arguments,
            },
        )

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

        try:
            if step_name == "generate_outputs":
                detail = handler(db, task, context, start=start)
            else:
                detail = handler(db, task, context)
        except Exception as exc:
            error_code, error_detail = map_runtime_error(exc)
            failure_detail = {
                "tool_name": tool_name,
                "title": title,
                "error_code": error_code,
                "error_message": str(exc),
            }
            failure_detail.update(error_detail)
            append_task_event(
                db,
                task_id=task.id,
                event_type="tool_execution_failed",
                step_name=step_name,
                status=task.status,
                detail=failure_detail,
            )
            db.commit()
            raise

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
        append_task_event(
            db,
            task_id=task.id,
            event_type="function_call_completed",
            step_name=step_name,
            status=task.status,
            detail={"function_name": react_decision.function_name},
        )
        append_task_event(
            db,
            task_id=task.id,
            event_type="step_react_observation",
            step_name=step_name,
            status=task.status,
            detail=_build_step_react_observation(detail),
        )
        db.commit()
        return True

    raise AgentRuntimeError(
        error_code=ErrorCode.TASK_RUNTIME_MAX_STEPS_EXCEEDED,
        message=f"Step ReAct exceeded max rounds for {step_name}.",
        detail={
            "step_name": step_name,
            "step_react_round": step_react_max_rounds,
            "step_react_max_rounds": step_react_max_rounds,
        },
    )


TOOL_REGISTRY = {
    "plan_task": _tool_plan_task,
    "normalize_aoi": _tool_normalize_aoi,
    "search_candidates": _tool_search_candidates,
    "recommend_dataset": _tool_recommend_dataset,
    "run_processing_pipeline": _tool_run_processing_pipeline,
    "generate_outputs": _tool_generate_outputs,
}
