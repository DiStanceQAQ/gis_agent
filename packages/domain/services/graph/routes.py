from __future__ import annotations

from .state import GISAgentState


def route_after_parse(state: GISAgentState) -> str:
    if state.get("need_clarification"):
        return "waiting_clarification"
    return "plan"


def route_after_plan(state: GISAgentState) -> str:
    if state.get("need_clarification"):
        return "waiting_clarification"
    if state.get("plan_status") == "failed":
        return "failed"
    return "execute"
