from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.database import SessionLocal
from packages.domain.models import MessageRecord, SessionRecord, TaskEventRecord, TaskRunRecord


def _seed_task_with_events() -> str:
    with SessionLocal() as db:
        session = SessionRecord(id="ses_stream_api", title="stream", status="active")
        message = MessageRecord(
            id="msg_stream_api",
            session_id=session.id,
            role="user",
            content="run",
        )
        task = TaskRunRecord(
            id="task_stream_api",
            session_id=session.id,
            user_message_id=message.id,
            status="running",
            current_step="normalize_aoi",
            analysis_type="NDVI",
        )
        db.add(session)
        db.add(message)
        db.add(task)
        db.flush()
        db.add_all(
            [
                TaskEventRecord(
                    task_id=task.id,
                    event_type="task_started",
                    status="running",
                    detail_json={"current_step": "normalize_aoi"},
                ),
                TaskEventRecord(
                    task_id=task.id,
                    event_type="tool_execution_started",
                    step_name="normalize_aoi",
                    status="running",
                    detail_json={"tool_name": "aoi.normalize"},
                ),
            ]
        )
        db.commit()
        return task.id


def _cleanup_seeded_task(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None:
            return
        db.query(TaskEventRecord).filter(TaskEventRecord.task_id == task_id).delete(synchronize_session=False)
        db.query(TaskRunRecord).filter(TaskRunRecord.id == task_id).delete(synchronize_session=False)
        db.query(MessageRecord).filter(MessageRecord.id == task.user_message_id).delete(synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.id == task.session_id).delete(synchronize_session=False)
        db.commit()


def test_stream_task_events_emits_sse_records() -> None:
    task_id = _seed_task_with_events()
    try:
        with TestClient(app) as client:
            with client.stream(
                "GET",
                f"/api/v1/tasks/{task_id}/events/stream?since_id=0&max_events=2&poll_interval_ms=100",
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                raw = "".join(chunk.decode("utf-8") for chunk in response.iter_raw())

        data_lines = [line.removeprefix("data: ") for line in raw.splitlines() if line.startswith("data: ")]
        payloads = [json.loads(line) for line in data_lines]
        assert [item["event_type"] for item in payloads] == ["task_started", "tool_execution_started"]
    finally:
        _cleanup_seeded_task(task_id)


def test_stream_task_events_returns_404_for_missing_task() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/tasks/task_missing/events/stream?since_id=0&max_events=1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"
