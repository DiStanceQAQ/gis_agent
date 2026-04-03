from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from packages.domain.models import TaskRunRecord
from packages.domain.services import agent_runtime as legacy_runtime

AgentRuntimeError = legacy_runtime.AgentRuntimeError
PipelineExecutionContext = legacy_runtime.PipelineExecutionContext
TOOL_REGISTRY = legacy_runtime.TOOL_REGISTRY


def maybe_skip_runtime_for_clarification(
    db: Session,
    task: TaskRunRecord,
    parsed_spec: Any,
) -> bool:
    return legacy_runtime._maybe_skip_runtime_for_clarification(db, task, parsed_spec)


def check_runtime_limits(*, start: float, step_count: int, tool_calls: int) -> None:
    legacy_runtime._check_runtime_limits(start=start, step_count=step_count, tool_calls=tool_calls)


def execute_tool_step(
    db: Session,
    task: TaskRunRecord,
    context: PipelineExecutionContext,
    *,
    step_name: str,
    tool_name: str,
    title: str,
    handler: Callable[..., Any],
    start: float,
) -> None:
    legacy_runtime._execute_tool_step(
        db,
        task,
        context,
        step_name=step_name,
        tool_name=tool_name,
        title=title,
        handler=handler,
        start=start,
    )


def map_runtime_error(exc: Exception) -> tuple[str, dict[str, object]]:
    return legacy_runtime._map_runtime_error(exc)


def mark_current_step_failed(
    db: Session,
    task: TaskRunRecord,
    *,
    detail: dict[str, object],
) -> None:
    legacy_runtime._mark_current_step_failed(
        db,
        task,
        detail=detail,
    )
