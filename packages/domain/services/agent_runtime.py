from __future__ import annotations

from packages.domain.config import get_settings
from packages.domain.services.catalog import search_candidates
from packages.domain.services.graph import runner as graph_runner
from packages.domain.services.graph.runtime_helpers import (
    AgentRuntimeError,
    PipelineExecutionContext,
    TOOL_REGISTRY,
    check_runtime_limits,
    map_runtime_error,
)
from packages.domain.services.ndvi_pipeline import run_real_ndvi_pipeline
from packages.domain.services.recommendation import build_recommendation
from packages.domain.services.storage import build_artifact_path, persist_artifact_file


def _check_runtime_limits(*, start: float, step_count: int, tool_calls: int) -> None:
    check_runtime_limits(
        start=start,
        step_count=step_count,
        tool_calls=tool_calls,
    )


def _map_runtime_error(exc: Exception) -> tuple[str, dict[str, object]]:
    return map_runtime_error(exc)


def run_task_runtime(task_id: str) -> None:
    graph_runner.run_task_graph(task_id)


__all__ = [
    "AgentRuntimeError",
    "PipelineExecutionContext",
    "TOOL_REGISTRY",
    "_check_runtime_limits",
    "_map_runtime_error",
    "build_artifact_path",
    "build_recommendation",
    "get_settings",
    "persist_artifact_file",
    "run_real_ndvi_pipeline",
    "run_task_runtime",
    "search_candidates",
]
