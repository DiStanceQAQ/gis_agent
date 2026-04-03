from __future__ import annotations

from time import perf_counter

from packages.domain.database import SessionLocal
from packages.domain.errors import ErrorCode
from packages.domain.logging import get_logger
from packages.domain.models import TaskRunRecord
from packages.domain.services.planner import (
    PLAN_STATUS_FAILED,
    PLAN_STATUS_NEEDS_CLARIFICATION,
    PLAN_STATUS_RUNNING,
    PLAN_STATUS_SUCCESS,
    build_task_plan,
    set_task_plan_status,
)
from packages.domain.services.task_state import (
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_WAITING_CLARIFICATION,
    ensure_task_steps,
    set_task_status,
    write_task_error,
)
from packages.domain.services.tool_registry import get_tool_definition
from packages.schemas.task import ParsedTaskSpec

from . import runtime_helpers
from .state import GISAgentState

logger = get_logger(__name__)


def _build_missing_task_state(task_id: str) -> GISAgentState:
    return {
        "plan_status": PLAN_STATUS_FAILED,
        "error_code": ErrorCode.TASK_NOT_FOUND,
        "error_message": f"Task not found: {task_id}",
    }


def parse_task_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None or task.task_spec is None:
            return _build_missing_task_state(task_id)

        parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
        return {
            "need_clarification": bool(parsed.need_confirmation),
            "parsed_spec": parsed.model_dump(),
        }


def plan_task_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None or task.task_spec is None:
            return _build_missing_task_state(task_id)

        parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
        plan = build_task_plan(parsed, task_id=task_id)
        task.plan_json = plan.model_dump()
        db.commit()

        if plan.status == PLAN_STATUS_NEEDS_CLARIFICATION:
            return {"need_clarification": True, "plan_status": plan.status}

        return {
            "need_clarification": False,
            "plan_status": plan.status,
            "runtime_started_at": perf_counter(),
            "tool_calls": 0,
            "runtime_context": runtime_helpers.PipelineExecutionContext(parsed_spec=parsed),
        }


def _prepare_runtime_start(
    *,
    db,
    task: TaskRunRecord,
    state: GISAgentState,
    start: float,
) -> GISAgentState | None:
    context = state.get("runtime_context")
    if context is None:
        if task.task_spec is None:
            return _build_missing_task_state(task.id)
        parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
        context = runtime_helpers.PipelineExecutionContext(parsed_spec=parsed)

    if runtime_helpers.maybe_skip_runtime_for_clarification(db, task, context.parsed_spec):
        return {
            "need_clarification": True,
            "plan_status": PLAN_STATUS_NEEDS_CLARIFICATION,
            "runtime_context": context,
            "runtime_started_at": start,
            "tool_calls": int(state.get("tool_calls", 0)),
        }

    step_names = [
        str(step.get("step_name"))
        for step in (task.plan_json or {}).get("steps", [])
        if step.get("step_name")
    ]
    ensure_task_steps(db, task, step_names)
    task.plan_json = set_task_plan_status(task.plan_json, PLAN_STATUS_RUNNING)
    first_step = step_names[0] if step_names else None
    set_task_status(
        db,
        task,
        TASK_STATUS_RUNNING,
        current_step=first_step,
        event_type="task_started",
        detail={
            "current_step": first_step,
            "plan_mode": (task.plan_json or {}).get("mode"),
        },
        force=True,
    )
    db.commit()
    return None


def _run_runtime_step(state: GISAgentState, *, step_name: str) -> GISAgentState:
    task_id = state["task_id"]
    start = float(state.get("runtime_started_at", perf_counter()))
    tool_calls = int(state.get("tool_calls", 0))

    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None:
            return _build_missing_task_state(task_id)

        context = state.get("runtime_context")
        if context is None:
            if task.task_spec is None:
                return _build_missing_task_state(task_id)
            parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
            context = runtime_helpers.PipelineExecutionContext(parsed_spec=parsed)

        if step_name == "normalize_aoi" and task.status != TASK_STATUS_RUNNING:
            early = _prepare_runtime_start(db=db, task=task, state=state, start=start)
            if early is not None:
                return early

        try:
            runtime_helpers.check_runtime_limits(
                start=start,
                step_count=len((task.plan_json or {}).get("steps", [])),
                tool_calls=tool_calls,
            )
            tool_def = get_tool_definition(step_name)
            handler = runtime_helpers.TOOL_REGISTRY.get(step_name)
            if tool_def is None or handler is None:
                raise runtime_helpers.AgentRuntimeError(
                    error_code=ErrorCode.TASK_RUNTIME_UNKNOWN_TOOL,
                    message=f"Unknown tool step: {step_name}",
                    detail={"step_name": step_name},
                )

            runtime_helpers.execute_tool_step(
                db,
                task,
                context,
                step_name=step_name,
                tool_name=tool_def.tool_name,
                title=tool_def.title,
                handler=handler,
                start=start,
            )
            tool_calls += 1

            if step_name == "generate_outputs":
                task.plan_json = set_task_plan_status(task.plan_json, PLAN_STATUS_SUCCESS)
                set_task_status(
                    db,
                    task,
                    TASK_STATUS_SUCCESS,
                    current_step="generate_outputs",
                    event_type="task_completed",
                    detail={
                        "duration_seconds": task.duration_seconds,
                        "selected_dataset": task.selected_dataset,
                        "fallback_used": task.fallback_used,
                    },
                    force=True,
                )
                db.commit()
                return {
                    "need_clarification": False,
                    "plan_status": PLAN_STATUS_SUCCESS,
                    "runtime_context": context,
                    "runtime_started_at": start,
                    "tool_calls": tool_calls,
                }

            return {
                "need_clarification": False,
                "plan_status": PLAN_STATUS_RUNNING,
                "runtime_context": context,
                "runtime_started_at": start,
                "tool_calls": tool_calls,
            }
        except Exception as exc:  # pragma: no cover - defensive guard
            error_code, error_detail = runtime_helpers.map_runtime_error(exc)
            write_task_error(
                task,
                error_code=error_code,
                error_message=str(exc),
            )
            task.duration_seconds = int(perf_counter() - start)
            failure_detail = {
                "error_code": task.error_code,
                "error_message": task.error_message,
                "failed_step": task.current_step,
                "duration_seconds": task.duration_seconds,
            }
            failure_detail.update(error_detail)
            runtime_helpers.mark_current_step_failed(
                db,
                task,
                detail=failure_detail,
            )
            task.plan_json = set_task_plan_status(task.plan_json, PLAN_STATUS_FAILED)
            set_task_status(
                db,
                task,
                TASK_STATUS_FAILED,
                current_step=task.current_step,
                event_type="task_failed",
                detail=failure_detail,
                force=True,
            )
            db.commit()
            logger.exception("graph.node.failed task_id=%s step_name=%s", task_id, step_name)
            return {
                "plan_status": PLAN_STATUS_FAILED,
                "error_code": task.error_code or ErrorCode.TASK_RUNTIME_FAILED,
                "error_message": task.error_message or "LangGraph execution failed.",
                "runtime_context": context,
                "runtime_started_at": start,
                "tool_calls": tool_calls,
            }


def normalize_aoi_node(state: GISAgentState) -> GISAgentState:
    return _run_runtime_step(state, step_name="normalize_aoi")


def search_candidates_node(state: GISAgentState) -> GISAgentState:
    return _run_runtime_step(state, step_name="search_candidates")


def recommend_dataset_node(state: GISAgentState) -> GISAgentState:
    return _run_runtime_step(state, step_name="recommend_dataset")


def run_ndvi_pipeline_node(state: GISAgentState) -> GISAgentState:
    return _run_runtime_step(state, step_name="run_ndvi_pipeline")


def generate_outputs_node(state: GISAgentState) -> GISAgentState:
    return _run_runtime_step(state, step_name="generate_outputs")


def finalize_success_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    if state.get("need_clarification"):
        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            if task is not None and task.status != TASK_STATUS_WAITING_CLARIFICATION:
                task.plan_json = set_task_plan_status(task.plan_json, PLAN_STATUS_NEEDS_CLARIFICATION)
                set_task_status(
                    db,
                    task,
                    TASK_STATUS_WAITING_CLARIFICATION,
                    current_step="parse_task",
                    event_type="task_waiting_clarification",
                    detail={"source": "langgraph"},
                    force=True,
                )
                db.commit()
    return state


def finalize_failed_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is not None and task.status != TASK_STATUS_FAILED:
            write_task_error(
                task,
                error_code=state.get("error_code") or ErrorCode.TASK_RUNTIME_FAILED,
                error_message=state.get("error_message") or "LangGraph execution failed.",
            )
            set_task_status(
                db,
                task,
                TASK_STATUS_FAILED,
                current_step=task.current_step,
                event_type="task_failed",
                detail={"source": "langgraph"},
                force=True,
            )
            db.commit()
    return state
