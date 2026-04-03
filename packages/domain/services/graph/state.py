from __future__ import annotations

from typing import Any, TypedDict


class GISAgentState(TypedDict, total=False):
    task_id: str
    need_clarification: bool
    plan_status: str
    error_code: str
    error_message: str
    runtime_started_at: float
    tool_calls: int
    parsed_spec: dict[str, Any]
    task_plan: dict[str, Any]
    recommendation: dict[str, Any]
    runtime_context: Any
