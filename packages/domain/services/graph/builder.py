from __future__ import annotations

from langgraph.graph import END, StateGraph

from .routes import route_after_parse, route_after_plan
from .state import GISAgentState


def _parse_task_node(state: GISAgentState) -> GISAgentState:
    return state


def _plan_task_node(state: GISAgentState) -> GISAgentState:
    return state


def _execute_task_node(state: GISAgentState) -> GISAgentState:
    return state


def _finalize_success_node(state: GISAgentState) -> GISAgentState:
    return state


def _finalize_failed_node(state: GISAgentState) -> GISAgentState:
    return state


def build_task_graph():
    graph = StateGraph(GISAgentState)
    graph.add_node("parse", _parse_task_node)
    graph.add_node("plan", _plan_task_node)
    graph.add_node("execute", _execute_task_node)
    graph.add_node("waiting_clarification", _finalize_success_node)
    graph.add_node("failed", _finalize_failed_node)
    graph.add_node("success", _finalize_success_node)

    graph.set_entry_point("parse")
    graph.add_conditional_edges(
        "parse",
        route_after_parse,
        {
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
            "execute": "execute",
        },
    )
    graph.add_edge("execute", "success")
    graph.add_edge("success", END)
    graph.add_edge("waiting_clarification", END)
    graph.add_edge("failed", END)

    return graph.compile()
