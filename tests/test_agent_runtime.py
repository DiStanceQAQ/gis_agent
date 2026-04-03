from __future__ import annotations

from time import perf_counter

import pytest

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.services import agent_runtime
from packages.domain.services.agent_runtime import (
    AgentRuntimeError,
    _check_runtime_limits,
    _map_runtime_error,
    run_task_runtime,
)


def test_check_runtime_limits_raises_max_steps_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_AGENT_MAX_STEPS", "1")
    get_settings.cache_clear()

    with pytest.raises(AgentRuntimeError) as exc_info:
        _check_runtime_limits(start=perf_counter(), step_count=2, tool_calls=0)

    assert exc_info.value.error_code == ErrorCode.TASK_RUNTIME_MAX_STEPS_EXCEEDED
    get_settings.cache_clear()


def test_check_runtime_limits_raises_max_tool_calls_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_AGENT_MAX_TOOL_CALLS", "1")
    get_settings.cache_clear()

    with pytest.raises(AgentRuntimeError) as exc_info:
        _check_runtime_limits(start=perf_counter(), step_count=1, tool_calls=1)

    assert exc_info.value.error_code == ErrorCode.TASK_RUNTIME_MAX_TOOL_CALLS_EXCEEDED
    get_settings.cache_clear()


def test_check_runtime_limits_raises_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_AGENT_RUNTIME_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()

    with pytest.raises(AgentRuntimeError) as exc_info:
        _check_runtime_limits(start=perf_counter() - 2.0, step_count=1, tool_calls=0)

    assert exc_info.value.error_code == ErrorCode.TASK_RUNTIME_TIMEOUT
    get_settings.cache_clear()


def test_map_runtime_error_preserves_unknown_tool_code() -> None:
    error = AgentRuntimeError(
        error_code=ErrorCode.TASK_RUNTIME_UNKNOWN_TOOL,
        message="Unknown tool step: foo",
        detail={"step_name": "foo"},
    )

    error_code, detail = _map_runtime_error(error)

    assert error_code == ErrorCode.TASK_RUNTIME_UNKNOWN_TOOL
    assert detail["step_name"] == "foo"


def test_run_task_runtime_delegates_to_graph_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def _fake_run(task_id: str) -> None:
        called.append(task_id)

    monkeypatch.setattr(agent_runtime, "run_task_graph", _fake_run)

    run_task_runtime("task_123")

    assert called == ["task_123"]


def test_agent_runtime_no_longer_exposes_legacy_runtime_entry() -> None:
    assert not hasattr(agent_runtime, "run_task_runtime_legacy")
