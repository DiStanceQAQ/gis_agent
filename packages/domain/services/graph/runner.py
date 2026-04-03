from __future__ import annotations

from functools import lru_cache

from .builder import build_task_graph


@lru_cache
def _compiled_graph():
    return build_task_graph()


def run_task_graph(task_id: str) -> None:
    graph = _compiled_graph()
    graph.invoke({"task_id": task_id})
