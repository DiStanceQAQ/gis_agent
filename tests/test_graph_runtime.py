import inspect
from types import SimpleNamespace

import pytest

from packages.domain.services.graph import nodes as graph_nodes
from packages.domain.services.graph import runtime_helpers
from packages.domain.errors import ErrorCode
from packages.domain.config import get_settings
from packages.domain.models import TaskRunRecord, TaskSpecRecord
from packages.domain.services.llm_client import LLMResponse, LLMUsage
from packages.domain.services.graph import builder as graph_builder
from packages.domain.services.graph.builder import build_task_graph
from packages.domain.services.graph.routes import (
    route_after_normalize_aoi,
    route_after_generate_outputs,
    route_after_parse,
    route_after_plan,
    route_after_run_processing_pipeline,
    route_after_recommend_dataset,
    route_after_search_candidates,
)
from packages.domain.services.task_state import TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_QUEUED
from packages.schemas.task import ParsedTaskSpec
from packages.schemas.agent import LLMReactStepDecision


def test_route_after_parse_to_clarification() -> None:
    state = {"need_clarification": True}
    assert route_after_parse(state) == "waiting_clarification"


def test_route_after_plan_to_normalize_aoi() -> None:
    state = {"need_clarification": False, "plan_status": "ready"}
    assert route_after_plan(state) == "normalize_aoi"


def test_route_after_plan_to_approval_required() -> None:
    state = {"need_clarification": False, "plan_status": "approval_required"}
    assert route_after_plan(state) == "approval_required"


def test_route_after_generate_outputs_to_failed() -> None:
    state = {"need_clarification": False, "plan_status": "failed"}
    assert route_after_generate_outputs(state) == "failed"


def test_route_after_normalize_aoi_to_search_candidates() -> None:
    state = {"need_clarification": False, "plan_status": "running"}
    assert route_after_normalize_aoi(state) == "search_candidates"


def test_route_after_search_candidates_to_recommend_dataset() -> None:
    state = {"need_clarification": False, "plan_status": "running"}
    assert route_after_search_candidates(state) == "recommend_dataset"


def test_route_after_recommend_dataset_to_run_processing_pipeline() -> None:
    state = {"need_clarification": False, "plan_status": "running"}
    assert route_after_recommend_dataset(state) == "run_processing_pipeline"


def test_route_after_run_processing_pipeline_to_generate_outputs() -> None:
    state = {"need_clarification": False, "plan_status": "running"}
    assert route_after_run_processing_pipeline(state) == "generate_outputs"


def test_run_task_graph_marks_failed_for_missing_task(monkeypatch) -> None:  # noqa: ANN001
    class _FakeSession:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            del exc_type, exc, tb

        def get(self, model, task_id):  # noqa: ANN001
            del model, task_id
            return None

    monkeypatch.setattr("packages.domain.services.graph.nodes.SessionLocal", lambda: _FakeSession())
    graph = build_task_graph()
    result = graph.invoke({"task_id": "task_does_not_exist"})

    assert result["plan_status"] == "failed"
    assert result["error_code"] == ErrorCode.TASK_NOT_FOUND


def test_graph_nodes_do_not_depend_on_agent_runtime_private_entry() -> None:
    source = inspect.getsource(graph_nodes)
    assert "_run_task_runtime_legacy" not in source
    assert "agent_runtime" not in source


def test_runtime_helpers_do_not_depend_on_agent_runtime_module() -> None:
    source = inspect.getsource(runtime_helpers)
    assert "services import agent_runtime" not in source
    assert "services.agent_runtime" not in source


def test_build_task_graph_executes_full_success_path_in_order(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    def _parse(state):  # noqa: ANN001
        calls.append("parse")
        return {"need_clarification": False}

    def _plan(state):  # noqa: ANN001
        calls.append("plan")
        return {"need_clarification": False, "plan_status": "ready"}

    def _normalize(state):  # noqa: ANN001
        calls.append("normalize_aoi")
        return {"need_clarification": False, "plan_status": "running"}

    def _search(state):  # noqa: ANN001
        calls.append("search_candidates")
        return {"need_clarification": False, "plan_status": "running"}

    def _recommend(state):  # noqa: ANN001
        calls.append("recommend_dataset")
        return {"need_clarification": False, "plan_status": "running"}

    def _run_processing(state):  # noqa: ANN001
        calls.append("run_processing_pipeline")
        return {"need_clarification": False, "plan_status": "running"}

    def _generate(state):  # noqa: ANN001
        calls.append("generate_outputs")
        return {"need_clarification": False, "plan_status": "running"}

    def _success(state):  # noqa: ANN001
        calls.append("success")
        return state

    monkeypatch.setattr(graph_builder, "parse_task_node", _parse)
    monkeypatch.setattr(graph_builder, "plan_task_node", _plan)
    monkeypatch.setattr(graph_builder, "normalize_aoi_node", _normalize)
    monkeypatch.setattr(graph_builder, "search_candidates_node", _search)
    monkeypatch.setattr(graph_builder, "recommend_dataset_node", _recommend)
    monkeypatch.setattr(graph_builder, "run_processing_pipeline_node", _run_processing)
    monkeypatch.setattr(graph_builder, "generate_outputs_node", _generate)
    monkeypatch.setattr(graph_builder, "finalize_success_node", _success)

    graph = graph_builder.build_task_graph()
    result = graph.invoke({"task_id": "task_fake"})

    assert calls == [
        "parse",
        "plan",
        "normalize_aoi",
        "search_candidates",
        "recommend_dataset",
        "run_processing_pipeline",
        "generate_outputs",
        "success",
    ]
    assert result.get("plan_status") == "running"


def test_build_task_graph_routes_to_waiting_clarification_terminal(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    def _parse(state):  # noqa: ANN001
        calls.append("parse")
        return {"need_clarification": True, "plan_status": "needs_clarification"}

    def _waiting(state):  # noqa: ANN001
        calls.append("waiting_clarification")
        return state

    monkeypatch.setattr(graph_builder, "parse_task_node", _parse)
    monkeypatch.setattr(graph_builder, "finalize_success_node", _waiting)

    graph = graph_builder.build_task_graph()
    result = graph.invoke({"task_id": "task_fake"})

    assert calls == ["parse", "waiting_clarification"]
    assert result.get("need_clarification") is True


def test_build_task_graph_routes_to_failed_terminal(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    def _parse(state):  # noqa: ANN001
        calls.append("parse")
        return {"need_clarification": False}

    def _plan(state):  # noqa: ANN001
        calls.append("plan")
        return {"plan_status": "failed"}

    def _failed(state):  # noqa: ANN001
        calls.append("failed")
        return state

    monkeypatch.setattr(graph_builder, "parse_task_node", _parse)
    monkeypatch.setattr(graph_builder, "plan_task_node", _plan)
    monkeypatch.setattr(graph_builder, "finalize_failed_node", _failed)

    graph = graph_builder.build_task_graph()
    result = graph.invoke({"task_id": "task_fake"})

    assert calls == ["parse", "plan", "failed"]
    assert result.get("plan_status") == "failed"


def test_plan_task_node_blocks_unapproved_operation_plan(monkeypatch) -> None:  # noqa: ANN001
    class _TaskSpec:
        raw_spec_json = {"analysis_type": "NDVI"}

    class _Task:
        id = "task_approval"
        status = TASK_STATUS_AWAITING_APPROVAL
        task_spec = _TaskSpec()
        plan_json = {"status": "ready", "operation_plan": {"status": "draft", "version": 1, "nodes": []}}

    class _FakeSession:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            del exc_type, exc, tb

        def get(self, model, task_id):  # noqa: ANN001
            del model, task_id
            return _Task()

        def commit(self) -> None:
            raise AssertionError("unapproved plan should be blocked before any commit")

    monkeypatch.setattr("packages.domain.services.graph.nodes.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "packages.domain.services.graph.nodes.build_task_plan",
        lambda parsed, task_id=None: (_ for _ in ()).throw(AssertionError("must not rebuild unapproved plans")),
    )

    result = graph_nodes.plan_task_node({"task_id": "task_approval"})
    assert result["plan_status"] == "approval_required"
    assert result["error_code"] == ErrorCode.PLAN_APPROVAL_REQUIRED


def test_plan_task_node_preserves_approved_operation_plan(monkeypatch) -> None:  # noqa: ANN001
    class _TaskSpec:
        raw_spec_json = {"analysis_type": "NDVI"}

    class _Task:
        id = "task_approved"
        status = TASK_STATUS_QUEUED
        task_spec = _TaskSpec()
        plan_json = {"status": "ready", "operation_plan": {"status": "approved", "version": 2, "nodes": []}}

    class _FakeSession:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            del exc_type, exc, tb

        def get(self, model, task_id):  # noqa: ANN001
            del model, task_id
            return _Task()

        def commit(self) -> None:
            raise AssertionError("approved plans should not be overwritten in plan node")

    monkeypatch.setattr("packages.domain.services.graph.nodes.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "packages.domain.services.graph.nodes.build_task_plan",
        lambda parsed, task_id=None: (_ for _ in ()).throw(AssertionError("must not rebuild approved plans")),
    )

    result = graph_nodes.plan_task_node({"task_id": "task_approved"})
    assert result["plan_status"] == "ready"
    assert result["need_clarification"] is False


def test_runtime_step_skips_unplanned_step_without_tool_call(monkeypatch) -> None:  # noqa: ANN001
    task = TaskRunRecord(
        id="task_clip",
        session_id="ses_clip",
        user_message_id="msg_clip",
        status="running",
    )
    task.task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        preferred_output=["geotiff"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={
            "analysis_type": "CLIP",
            "operation_params": {
                "source_path": "/tmp/source.tif",
                "clip_path": "/tmp/aoi.geojson",
            },
        },
    )
    task.plan_json = {
        "status": "running",
        "steps": [
            {"step_name": "plan_task"},
            {"step_name": "normalize_aoi"},
            {"step_name": "run_processing_pipeline"},
            {"step_name": "generate_outputs"},
        ],
    }

    class _FakeSession:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            del exc_type, exc, tb

        def get(self, model, task_id):  # noqa: ANN001
            del model, task_id
            return task

        def commit(self) -> None:
            return None

        def flush(self) -> None:
            return None

        def add(self, record) -> None:  # noqa: ANN001
            del record
            return None

    monkeypatch.setattr("packages.domain.services.graph.nodes.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "packages.domain.services.graph.nodes.runtime_helpers.execute_tool_step",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected tool execution")),  # noqa: ARG005
    )

    state = {
        "task_id": task.id,
        "runtime_started_at": runtime_helpers.perf_counter(),
        "tool_calls": 0,
        "runtime_context": runtime_helpers.PipelineExecutionContext(parsed_spec=ParsedTaskSpec()),
    }
    result = graph_nodes._run_runtime_step(state, step_name="search_candidates")

    assert result["plan_status"] == "running"
    assert result["tool_calls"] == 0


def test_step_react_decision_uses_llm_when_enabled(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GIS_AGENT_LLM_STEP_REACT_ENABLED", "true")
    get_settings.cache_clear()

    calls = {"count": 0}

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self
        calls["count"] += 1
        assert kwargs["phase"] == "react_step"
        return LLMResponse(
            model="gpt-4o-mini",
            request_id="req-react",
            content_text="{}",
            content_json={
                "decision": "continue",
                "function_name": "catalog.search",
                "arguments": {"step_name": "search_candidates"},
                "reasoning_summary": "继续执行目录搜索",
            },
            usage=LLMUsage(input_tokens=12, output_tokens=8, total_tokens=20),
            latency_ms=11,
            raw_payload={},
        )

    monkeypatch.setattr("packages.domain.services.graph.runtime_helpers.LLMClient.chat_json", _fake_chat_json)
    decision = runtime_helpers._build_step_react_decision(
        db=None,
        task=TaskRunRecord(id="task_react", session_id="ses_react", user_message_id="msg_react"),
        step_name="search_candidates",
        tool_name="catalog.search",
        title="搜索候选",
        context=runtime_helpers.PipelineExecutionContext(parsed_spec=ParsedTaskSpec()),
    )

    assert calls["count"] == 1
    assert decision.decision == "continue"
    assert decision.function_name == "catalog.search"
    get_settings.cache_clear()


class _FakeDB:
    def add(self, record) -> None:  # noqa: ANN001
        del record

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None


def test_execute_tool_step_supports_step_react_retry_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    task = TaskRunRecord(id="task_retry", session_id="ses_retry", user_message_id="msg_retry", status="running")
    task.plan_json = {
        "status": "running",
        "steps": [
            {"step_name": "search_candidates", "status": "pending"},
        ],
    }
    context = runtime_helpers.PipelineExecutionContext(parsed_spec=ParsedTaskSpec())
    events: list[str] = []
    decisions = iter(
        [
            LLMReactStepDecision(
                decision="continue",
                function_name="unknown.tool",
                arguments={"step_name": "search_candidates"},
                reasoning_summary="第一轮误选工具",
            ),
            LLMReactStepDecision(
                decision="continue",
                function_name="catalog.search",
                arguments={"step_name": "search_candidates"},
                reasoning_summary="第二轮修正为正确工具",
            ),
        ]
    )
    handler_calls = {"count": 0}

    monkeypatch.setattr(
        runtime_helpers,
        "get_settings",
        lambda: SimpleNamespace(agent_step_react_max_rounds=2, agent_step_react_timeout_seconds=30),
    )
    monkeypatch.setattr(runtime_helpers, "_build_step_react_decision", lambda **kwargs: next(decisions))
    monkeypatch.setattr(runtime_helpers, "append_task_event", lambda *args, **kwargs: events.append(kwargs["event_type"]))

    def _handler(db, task, context):  # noqa: ANN001
        del db, task, context
        handler_calls["count"] += 1
        return {"ok": True}

    runtime_helpers.execute_tool_step(
        _FakeDB(),
        task,
        context,
        step_name="search_candidates",
        tool_name="catalog.search",
        title="搜索候选",
        handler=_handler,
        start=runtime_helpers.perf_counter(),
    )

    assert events.count("step_react_decision") == 2
    assert "step_react_retry" in events
    assert handler_calls["count"] == 1


def test_execute_tool_step_skip_marks_step_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    task = TaskRunRecord(id="task_skip", session_id="ses_skip", user_message_id="msg_skip", status="running")
    task.plan_json = {
        "status": "running",
        "steps": [
            {"step_name": "search_candidates", "status": "pending"},
        ],
    }
    context = runtime_helpers.PipelineExecutionContext(parsed_spec=ParsedTaskSpec())
    events: list[str] = []

    monkeypatch.setattr(
        runtime_helpers,
        "get_settings",
        lambda: SimpleNamespace(agent_step_react_max_rounds=1, agent_step_react_timeout_seconds=30),
    )
    monkeypatch.setattr(
        runtime_helpers,
        "_build_step_react_decision",
        lambda **kwargs: LLMReactStepDecision(
            decision="skip",
            function_name=None,
            arguments={"step_name": "search_candidates"},
            reasoning_summary="当前上下文允许跳过",
        ),
    )
    monkeypatch.setattr(runtime_helpers, "append_task_event", lambda *args, **kwargs: events.append(kwargs["event_type"]))

    def _handler(db, task, context):  # noqa: ANN001
        del db, task, context
        raise AssertionError("handler should not run for skip decision")

    runtime_helpers.execute_tool_step(
        _FakeDB(),
        task,
        context,
        step_name="search_candidates",
        tool_name="catalog.search",
        title="搜索候选",
        handler=_handler,
        start=runtime_helpers.perf_counter(),
    )

    step = next(item for item in task.steps if item.step_name == "search_candidates")
    assert step.status == "skipped"
    plan_step = next(item for item in (task.plan_json or {}).get("steps", []) if item.get("step_name") == "search_candidates")
    assert plan_step.get("status") == "skipped"
    assert "step_skipped_by_react" in events
    assert "tool_execution_started" not in events
    assert "tool_execution_completed" not in events


def test_execute_tool_step_emits_tool_execution_failed_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = TaskRunRecord(id="task_tool_fail", session_id="ses_tool_fail", user_message_id="msg_tool_fail", status="running")
    task.plan_json = {
        "status": "running",
        "steps": [
            {"step_name": "search_candidates", "status": "pending"},
        ],
    }
    context = runtime_helpers.PipelineExecutionContext(parsed_spec=ParsedTaskSpec())
    events: list[str] = []

    monkeypatch.setattr(
        runtime_helpers,
        "get_settings",
        lambda: SimpleNamespace(agent_step_react_max_rounds=1, agent_step_react_timeout_seconds=30),
    )
    monkeypatch.setattr(
        runtime_helpers,
        "_build_step_react_decision",
        lambda **kwargs: LLMReactStepDecision(
            decision="continue",
            function_name="catalog.search",
            arguments={"step_name": "search_candidates"},
            reasoning_summary="正常执行",
        ),
    )
    monkeypatch.setattr(runtime_helpers, "append_task_event", lambda *args, **kwargs: events.append(kwargs["event_type"]))

    def _handler(db, task, context):  # noqa: ANN001
        del db, task, context
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        runtime_helpers.execute_tool_step(
            _FakeDB(),
            task,
            context,
            step_name="search_candidates",
            tool_name="catalog.search",
            title="搜索候选",
            handler=_handler,
            start=runtime_helpers.perf_counter(),
        )

    assert "tool_execution_started" in events
    assert "tool_execution_failed" in events
    assert "tool_execution_completed" not in events
