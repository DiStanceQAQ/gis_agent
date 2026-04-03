from packages.domain.errors import ErrorCode
from packages.domain.services.graph.builder import build_task_graph
from packages.domain.services.graph.routes import route_after_execute, route_after_parse, route_after_plan


def test_route_after_parse_to_clarification() -> None:
    state = {"need_clarification": True}
    assert route_after_parse(state) == "waiting_clarification"


def test_route_after_plan_to_execute() -> None:
    state = {"need_clarification": False, "plan_status": "ready"}
    assert route_after_plan(state) == "execute"


def test_route_after_execute_to_failed() -> None:
    state = {"need_clarification": False, "plan_status": "failed"}
    assert route_after_execute(state) == "failed"


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
