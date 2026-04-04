from __future__ import annotations

from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.operation_registry import OPERATION_SPECS
from packages.schemas.operation_plan import OperationPlan


def _build_dependency_graph(plan: OperationPlan) -> dict[str, list[str]]:
    step_ids = {node.step_id for node in plan.nodes}
    graph: dict[str, list[str]] = {}

    for node in plan.nodes:
        if node.op_name not in OPERATION_SPECS:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                message=f"Unknown operation: {node.op_name}",
                detail={"step_id": node.step_id, "op_name": node.op_name},
            )

        unknown_deps = [dep for dep in node.depends_on if dep not in step_ids]
        if unknown_deps:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                message="Operation plan depends_on references unknown steps.",
                detail={"step_id": node.step_id, "unknown_depends_on": unknown_deps},
            )
        graph[node.step_id] = list(node.depends_on)
    return graph


def _detect_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def _walk(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False

        visiting.add(step_id)
        for dependency in graph.get(step_id, []):
            if _walk(dependency):
                return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    return any(_walk(step_id) for step_id in graph)


def validate_operation_plan(plan: OperationPlan) -> OperationPlan:
    if not plan.nodes:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_SCHEMA_INVALID,
            message="Operation plan must include at least one node.",
            detail={"reason": "empty_nodes"},
        )
    graph = _build_dependency_graph(plan)
    if _detect_cycle(graph):
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_DEPENDENCY_CYCLE,
            message="Operation plan contains dependency cycles.",
        )
    return plan.model_copy(update={"status": "validated"})
