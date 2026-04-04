import pytest
from pydantic import ValidationError

from packages.schemas.message import MessageCreateResponse


def test_message_create_response_defaults_to_task_mode() -> None:
    response = MessageCreateResponse.model_validate(
        {
            "message_id": "msg-1",
            "task_id": "task-1",
            "task_status": "queued",
        }
    )

    assert response.mode == "task"
    assert response.task_id == "task-1"
    assert response.task_status == "queued"
    assert response.assistant_message is None
    assert response.intent is None
    assert response.intent_confidence is None
    assert response.awaiting_task_confirmation is False
    assert response.need_clarification is False
    assert response.need_approval is False
    assert response.missing_fields == []
    assert response.clarification_message is None


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        ({"message_id": "msg-1", "mode": "task"}, "task_id"),
        ({"message_id": "msg-1", "mode": "task", "task_id": "", "task_status": "queued"}, "task_id"),
        ({"message_id": "msg-1", "mode": "task", "task_id": "task-1", "task_status": ""}, "task_status"),
    ],
)
def test_message_create_response_requires_task_fields_in_task_mode(
    payload: dict[str, object],
    expected_field: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        MessageCreateResponse.model_validate(payload)

    assert expected_field in str(exc_info.value)


def test_message_create_response_allows_chat_mode_without_task_fields() -> None:
    response = MessageCreateResponse.model_validate(
        {
            "message_id": "msg-1",
            "mode": "chat",
            "assistant_message": "hello",
            "intent": "greeting",
            "intent_confidence": 0.91,
        }
    )

    assert response.mode == "chat"
    assert response.task_id is None
    assert response.task_status is None
    assert response.assistant_message == "hello"
    assert response.intent == "greeting"
    assert response.intent_confidence == 0.91
    assert response.awaiting_task_confirmation is False
