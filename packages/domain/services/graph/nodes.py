from __future__ import annotations

from packages.domain.database import SessionLocal
from packages.domain.errors import ErrorCode
from packages.domain.models import TaskRunRecord
from packages.domain.services.planner import (
    PLAN_STATUS_NEEDS_CLARIFICATION,
    PLAN_STATUS_SUCCESS,
    build_task_plan,
    set_task_plan_status,
)
from packages.domain.services.task_state import (
    TASK_STATUS_FAILED,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_task_status,
    write_task_error,
)
from packages.schemas.task import ParsedTaskSpec

from .state import GISAgentState


def parse_task_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None or task.task_spec is None:
            return {
                "plan_status": "failed",
                "error_code": ErrorCode.TASK_NOT_FOUND,
                "error_message": f"Task not found: {task_id}",
            }

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
            return {
                "plan_status": "failed",
                "error_code": ErrorCode.TASK_NOT_FOUND,
                "error_message": f"Task not found: {task_id}",
            }

        parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
        plan = build_task_plan(parsed, task_id=task_id)
        task.plan_json = plan.model_dump()
        db.commit()

        if plan.status == PLAN_STATUS_NEEDS_CLARIFICATION:
            return {"need_clarification": True, "plan_status": plan.status}

        return {"need_clarification": False, "plan_status": plan.status}


def execute_task_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    from packages.domain.services.agent_runtime import run_task_runtime_legacy

    run_task_runtime_legacy(task_id)

    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None:
            return {
                "plan_status": "failed",
                "error_code": ErrorCode.TASK_NOT_FOUND,
                "error_message": f"Task not found: {task_id}",
            }

        if task.status == TASK_STATUS_WAITING_CLARIFICATION:
            return {"need_clarification": True, "plan_status": PLAN_STATUS_NEEDS_CLARIFICATION}

        if task.status == TASK_STATUS_FAILED:
            return {
                "plan_status": "failed",
                "error_code": task.error_code or ErrorCode.TASK_RUNTIME_FAILED,
                "error_message": task.error_message or "LangGraph execution failed.",
            }

        return {"need_clarification": False, "plan_status": PLAN_STATUS_SUCCESS}


def finalize_success_node(state: GISAgentState) -> GISAgentState:
    task_id = state["task_id"]
    if state.get("need_clarification"):
        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            if task is not None:
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
