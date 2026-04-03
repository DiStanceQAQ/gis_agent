from __future__ import annotations

from .state import GISAgentState


def _route_after_step(state: GISAgentState, *, success_next: str) -> str:
    if state.get("need_clarification"):
        return "waiting_clarification"
    if state.get("plan_status") == "approval_required":
        return "approval_required"
    if state.get("plan_status") == "failed":
        return "failed"
    return success_next


def route_after_parse(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="plan")


def route_after_plan(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="normalize_aoi")


def route_after_normalize_aoi(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="search_candidates")


def route_after_search_candidates(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="recommend_dataset")


def route_after_recommend_dataset(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="run_processing_pipeline")


def route_after_run_processing_pipeline(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="generate_outputs")


def route_after_generate_outputs(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="success")
