from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routers import messages as messages_router
from packages.schemas.message import MessageCreateResponse


def _parse_sse(raw: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    blocks = [block for block in raw.split("\n\n") if block.strip()]
    for block in blocks:
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
    return events


def test_message_stream_chat_mode_emits_delta_and_final_payload(monkeypatch) -> None:
    def _fake_create_message(*, db, payload):  # noqa: ANN001
        del db, payload
        return MessageCreateResponse(
            message_id="msg_stream_chat",
            mode="chat",
            task_id=None,
            task_status=None,
            assistant_message="你好，流式输出",
            intent="chat",
            intent_confidence=0.9,
            awaiting_task_confirmation=False,
            need_clarification=False,
            need_approval=False,
            missing_fields=[],
            clarification_message=None,
        )

    monkeypatch.setattr(messages_router, "create_message", _fake_create_message)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/messages?stream=true",
            json={"session_id": "ses_test", "content": "你好", "file_ids": []},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = "".join(chunk.decode("utf-8") for chunk in response.iter_raw())

    parsed = _parse_sse(raw)
    event_names = [name for name, _ in parsed]
    assert "ready" in event_names
    assert "delta" in event_names
    assert parsed[-2][0] == "message"
    assert parsed[-2][1]["mode"] == "chat"
    assert parsed[-1][0] == "done"


def test_message_stream_task_mode_emits_final_payload_without_delta(monkeypatch) -> None:
    def _fake_create_message(*, db, payload):  # noqa: ANN001
        del db, payload
        return MessageCreateResponse(
            message_id="msg_stream_task",
            mode="task",
            task_id="task_stream_1",
            task_status="queued",
            assistant_message=None,
            intent="task",
            intent_confidence=0.95,
            awaiting_task_confirmation=False,
            need_clarification=False,
            need_approval=True,
            missing_fields=[],
            clarification_message=None,
        )

    monkeypatch.setattr(messages_router, "create_message", _fake_create_message)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/messages?stream=true",
            json={"session_id": "ses_test", "content": "run", "file_ids": []},
        ) as response:
            assert response.status_code == 200
            raw = "".join(chunk.decode("utf-8") for chunk in response.iter_raw())

    parsed = _parse_sse(raw)
    event_names = [name for name, _ in parsed]
    assert "delta" not in event_names
    assert event_names == ["ready", "message", "done"]
    assert parsed[1][1]["task_id"] == "task_stream_1"
