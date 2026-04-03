from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import (
    finalize_failed_node,
    finalize_success_node,
    generate_outputs_node,
    normalize_aoi_node,
    parse_task_node,
    plan_task_node,
    recommend_dataset_node,
    run_ndvi_pipeline_node,
    search_candidates_node,
)
from .routes import (
    route_after_generate_outputs,
    route_after_normalize_aoi,
    route_after_parse,
    route_after_plan,
    route_after_recommend_dataset,
    route_after_run_ndvi_pipeline,
    route_after_search_candidates,
)
from .state import GISAgentState


def build_task_graph():
    graph = StateGraph(GISAgentState)
    graph.add_node("parse", parse_task_node)
    graph.add_node("plan", plan_task_node)
    graph.add_node("normalize_aoi", normalize_aoi_node)
    graph.add_node("search_candidates", search_candidates_node)
    graph.add_node("recommend_dataset", recommend_dataset_node)
    graph.add_node("run_ndvi_pipeline", run_ndvi_pipeline_node)
    graph.add_node("generate_outputs", generate_outputs_node)
    graph.add_node("waiting_clarification", finalize_success_node)
    graph.add_node("failed", finalize_failed_node)
    graph.add_node("success", finalize_success_node)

    graph.set_entry_point("parse")
    graph.add_conditional_edges(
        "parse",
        route_after_parse,
        {
            "failed": "failed",
            "waiting_clarification": "waiting_clarification",
            "plan": "plan",
        },
    )
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
            "normalize_aoi": "normalize_aoi",
        },
    )
    graph.add_conditional_edges(
        "normalize_aoi",
        route_after_normalize_aoi,
        {
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
            "search_candidates": "search_candidates",
        },
    )
    graph.add_conditional_edges(
        "search_candidates",
        route_after_search_candidates,
        {
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
            "recommend_dataset": "recommend_dataset",
        },
    )
    graph.add_conditional_edges(
        "recommend_dataset",
        route_after_recommend_dataset,
        {
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
            "run_ndvi_pipeline": "run_ndvi_pipeline",
        },
    )
    graph.add_conditional_edges(
        "run_ndvi_pipeline",
        route_after_run_ndvi_pipeline,
        {
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
            "generate_outputs": "generate_outputs",
        },
    )
    graph.add_conditional_edges(
        "generate_outputs",
        route_after_generate_outputs,
        {
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
            "success": "success",
        },
    )
    graph.add_edge("success", END)
    graph.add_edge("waiting_clarification", END)
    graph.add_edge("failed", END)

    return graph.compile()
