from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import (
    execute_task_node,
    finalize_failed_node,
    finalize_success_node,
    parse_task_node,
    plan_task_node,
)
from .routes import route_after_execute, route_after_parse, route_after_plan
from .state import GISAgentState

def build_task_graph():
    graph = StateGraph(GISAgentState)
    graph.add_node("parse", parse_task_node)
    graph.add_node("plan", plan_task_node)
    graph.add_node("execute", execute_task_node)
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
            "execute": "execute",
        },
    )
    graph.add_conditional_edges(
        "execute",
        route_after_execute,
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
