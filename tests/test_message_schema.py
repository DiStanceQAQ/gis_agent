import pytest
from pydantic import ValidationError

from packages.schemas.message import MessageCreateResponse
from packages.schemas.task import TaskDetailResponse


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


def test_message_create_response_accepts_understanding_payload() -> None:
    payload = MessageCreateResponse(
        message_id="msg_1",
        mode="task",
        task_id="task_1",
        task_status="waiting_clarification",
        response_mode="show_revision",
        understanding={
            "intent": "task_correction",
            "intent_confidence": 0.91,
            "understanding_summary": "把 AOI 来源切换为上传 shp。",
            "editable_fields": ["aoi_input", "aoi_source_type"],
            "field_confidences": {
                "aoi_input": {
                    "score": 0.84,
                    "level": "high",
                    "evidence": [
                        {
                            "source": "current_message",
                            "weight": 0.70,
                            "detail": "命中 shp 提示",
                        }
                    ],
                }
            },
        },
        response_payload={"execution_blocked": False},
    )

    assert payload.response_mode == "show_revision"
    assert payload.understanding is not None
    assert payload.understanding.intent == "task_correction"


def test_task_detail_response_accepts_active_revision_summary() -> None:
    detail = TaskDetailResponse(
        task_id="task_1",
        status="waiting_clarification",
        analysis_type="WORKFLOW",
        interaction_state="execution_blocked",
        last_response_mode="ask_missing_fields",
        active_revision={
            "revision_id": "rev_2",
            "revision_number": 2,
            "change_type": "correction",
            "created_at": "2026-04-08T10:00:00+00:00",
            "understanding_summary": "上传 shp 中的江西作为 AOI。",
            "execution_blocked": True,
            "execution_blocked_reason": "AOI normalization failed",
            "field_confidences": {},
            "ranked_candidates": {},
        },
        revisions=[
            {
                "revision_id": "rev_1",
                "revision_number": 1,
                "change_type": "initial",
                "created_at": "2026-04-08T09:50:00+00:00",
                "understanding_summary": "初始版本。",
                "field_confidences": {},
                "ranked_candidates": {},
            }
        ],
    )

    assert detail.interaction_state == "execution_blocked"
    assert detail.last_response_mode == "ask_missing_fields"
    assert detail.active_revision is not None
    assert detail.active_revision.execution_blocked is True
    assert detail.active_revision.created_at is not None
    assert len(detail.revisions) == 1
    assert detail.revisions[0].revision_id == "rev_1"
    assert detail.revisions[0].created_at is not None
