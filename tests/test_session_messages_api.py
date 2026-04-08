from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.database import SessionLocal
from packages.domain.models import MessageRecord, SessionRecord
from packages.domain.services import orchestrator


def _cleanup_session_messages(session_id: str) -> None:
    with SessionLocal() as db:
        db.query(MessageRecord).filter(MessageRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
        db.commit()


def _seed_session_with_messages() -> tuple[str, list[str]]:
    with SessionLocal() as db:
        session = orchestrator.create_session(db)
        session_id = session.session_id
        base = datetime.now(timezone.utc)
        messages = [
            MessageRecord(
                id="msg_hist_1",
                session_id=session_id,
                role="user",
                content="hello-1",
                created_at=base,
            ),
            MessageRecord(
                id="msg_hist_2",
                session_id=session_id,
                role="assistant",
                content="reply-1",
                created_at=base + timedelta(seconds=1),
            ),
            MessageRecord(
                id="msg_hist_3",
                session_id=session_id,
                role="user",
                content="hello-2",
                created_at=base + timedelta(seconds=2),
            ),
            MessageRecord(
                id="msg_hist_4",
                session_id=session_id,
                role="assistant",
                content="reply-2",
                created_at=base + timedelta(seconds=3),
            ),
        ]
        db.add_all(messages)
        db.commit()
        return session_id, [item.id for item in messages]


def test_list_session_messages_supports_cursor_pagination() -> None:
    session_id, message_ids = _seed_session_with_messages()
    try:
        with TestClient(app) as client:
            first = client.get(f"/api/v1/sessions/{session_id}/messages?limit=2")
            assert first.status_code == 200
            first_payload = first.json()

            assert first_payload["session_id"] == session_id
            assert [item["message_id"] for item in first_payload["messages"]] == message_ids[2:4]
            assert first_payload["next_cursor"] == message_ids[2]

            second = client.get(
                f"/api/v1/sessions/{session_id}/messages?limit=2&cursor={first_payload['next_cursor']}"
            )
            assert second.status_code == 200
            second_payload = second.json()

            assert [item["message_id"] for item in second_payload["messages"]] == message_ids[0:2]
            assert second_payload["next_cursor"] is None
    finally:
        _cleanup_session_messages(session_id)


def test_list_session_messages_returns_404_for_missing_session() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions/ses_missing/messages?limit=10")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"
