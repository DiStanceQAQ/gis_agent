from apps.worker import tasks


def test_run_task_async_calls_graph_runner(monkeypatch) -> None:  # noqa: ANN001
    called: list[str] = []

    def _fake_run(task_id: str) -> None:
        called.append(task_id)

    monkeypatch.setattr(tasks, "run_task_graph", _fake_run)

    tasks.run_task_async("task_123")

    assert called == ["task_123"]
