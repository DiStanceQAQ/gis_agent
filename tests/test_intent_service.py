from __future__ import annotations

from typing import Any

import pytest

from packages.domain.services.chat import CHAT_FALLBACK_TEXT, generate_chat_reply
from packages.domain.services.intent import (
    IntentResult,
    classify_message_intent,
    is_task_confirmation_message,
)


class _FakeIntentResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.content_json = payload


class _FakeLLMClient:
    response: _FakeIntentResponse | None = None
    calls: list[dict[str, Any]] = []
    error: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        del args, kwargs

    def chat_json(self, **kwargs) -> _FakeIntentResponse:  # noqa: ANN003
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.fixture(autouse=True)
def _reset_fake_llm() -> None:
    _FakeLLMClient.response = None
    _FakeLLMClient.calls = []
    _FakeLLMClient.error = None


def test_is_task_confirmation_message_detects_confirmation_keywords() -> None:
    assert is_task_confirmation_message("好的，继续")
    assert is_task_confirmation_message("OK")
    assert is_task_confirmation_message("可以") is True


@pytest.mark.parametrize(
    "message",
    [
        "帮我分析 2024 年 6 月的 NDVI",
        "请解释一下什么是 NDVI",
        "",
        "可以帮我分析NDVI吗",
        "look at this map",
    ],
)
def test_is_task_confirmation_message_rejects_non_confirmations(message: str) -> None:
    assert is_task_confirmation_message(message) is False


def test_classify_message_intent_falls_back_to_ambiguous_on_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient", _FakeLLMClient)
    _FakeLLMClient.error = RuntimeError("boom")

    result = classify_message_intent(
        "帮我分析 2024 年 6 月的 NDVI",
        history=[{"role": "user", "content": "上一轮"}],
        task_id="task_123",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert result.intent == "ambiguous"
    assert result.confidence == 0.0
    assert "falling back to ambiguous" in result.reason
    assert _FakeLLMClient.calls[0]["phase"] == "intent"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_123"


def test_classify_message_intent_returns_task_for_explicit_confirmation_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient", _FakeLLMClient)
    _FakeLLMClient.error = RuntimeError("boom")

    result = classify_message_intent(
        "好的，继续",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_confirmed",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert result.intent == "task"
    assert result.confidence == 0.99
    assert "explicit confirmation" in result.reason
    assert _FakeLLMClient.calls[0]["phase"] == "intent"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_confirmed"


def test_classify_message_intent_returns_task_for_explicit_confirmation_with_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient", _FakeLLMClient)
    _FakeLLMClient.response = _FakeIntentResponse(
        {
            "intent": "task",
            "confidence": "not-a-number",
            "reason": "The user asks for an NDVI analysis.",
        }
    )

    result = classify_message_intent(
        "好的，继续",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_confirmed_payload",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert result.intent == "task"
    assert result.confidence == 0.99
    assert "explicit confirmation" in result.reason
    assert _FakeLLMClient.calls[0]["phase"] == "intent"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_confirmed_payload"


@pytest.mark.parametrize("payload", [["unexpected"], "unexpected"])
def test_classify_message_intent_returns_task_for_explicit_confirmation_with_non_dict_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient", _FakeLLMClient)
    _FakeLLMClient.response = _FakeIntentResponse(payload)  # type: ignore[arg-type]

    result = classify_message_intent(
        "好的，继续",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_confirmed_nondict",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert result.intent == "task"
    assert result.confidence == 0.99
    assert "explicit confirmation" in result.reason
    assert _FakeLLMClient.calls[0]["phase"] == "intent"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_confirmed_nondict"


def test_classify_message_intent_passes_through_valid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient", _FakeLLMClient)
    _FakeLLMClient.response = _FakeIntentResponse(
        {
            "intent": "task",
            "confidence": 0.94,
            "reason": "The user asks for an NDVI analysis.",
        }
    )

    result = classify_message_intent(
        "帮我分析 2024 年 6 月的 NDVI",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_456",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert result == IntentResult(
        intent="task",
        confidence=0.94,
        reason="The user asks for an NDVI analysis.",
    )
    assert _FakeLLMClient.calls[0]["phase"] == "intent"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_456"


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "task", "confidence": "0.94", "reason": "The user asks for an NDVI analysis."},
        {"intent": "task", "confidence": 1.5, "reason": "The user asks for an NDVI analysis."},
        {"intent": "task", "confidence": True, "reason": "The user asks for an NDVI analysis."},
        {"intent": "task", "confidence": 0.7, "reason": {"detail": "bad"}},
    ],
)
def test_classify_message_intent_rejects_invalid_payload_fields(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient", _FakeLLMClient)
    _FakeLLMClient.response = _FakeIntentResponse(payload)

    result = classify_message_intent(
        "帮我分析 2024 年 6 月的 NDVI",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_invalid_payload",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert result.intent == "ambiguous"
    assert result.confidence == 0.0


def test_generate_chat_reply_falls_back_on_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.domain.services.chat.LLMClient", _FakeLLMClient)
    _FakeLLMClient.error = RuntimeError("boom")

    reply = generate_chat_reply(
        user_message="你好",
        history=[{"role": "user", "content": "上一轮"}],
        task_id="task_789",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert reply == CHAT_FALLBACK_TEXT
    assert _FakeLLMClient.calls[0]["phase"] == "chat"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_789"


def test_generate_chat_reply_extracts_reply_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.domain.services.chat.LLMClient", _FakeLLMClient)
    _FakeLLMClient.response = _FakeIntentResponse({"reply": "当然，我可以帮你。"})

    reply = generate_chat_reply(
        user_message="你好",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_012",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert reply == "当然，我可以帮你。"
    assert _FakeLLMClient.calls[0]["phase"] == "chat"
    assert _FakeLLMClient.calls[0]["task_id"] == "task_012"


@pytest.mark.parametrize(
    "payload",
    [
        {"reply": {"text": "当然，我可以帮你。"}},
        {"reply": ["当然，我可以帮你。"]},
    ],
)
def test_generate_chat_reply_rejects_non_string_reply_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr("packages.domain.services.chat.LLMClient", _FakeLLMClient)
    _FakeLLMClient.response = _FakeIntentResponse(payload)

    reply = generate_chat_reply(
        user_message="你好",
        history=[{"role": "assistant", "content": "上一轮"}],
        task_id="task_bad_reply",
        db_session=object(),  # type: ignore[arg-type]
    )

    assert reply == CHAT_FALLBACK_TEXT
