from __future__ import annotations

import json

import pytest

from packages.domain.config import Settings
from packages.domain.services.llm_client import LLMClient, LLMClientError, build_prompt_hash


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    next_response: _FakeResponse | None = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        del args, kwargs

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb

    def post(self, *args, **kwargs) -> _FakeResponse:  # noqa: ANN002, ANN003
        del args, kwargs
        assert self.next_response is not None
        return self.next_response


def test_build_prompt_hash_is_deterministic() -> None:
    left = build_prompt_hash(system_prompt="system", user_prompt="user", model="gpt-test")
    right = build_prompt_hash(system_prompt="system", user_prompt="user", model="gpt-test")
    assert left == right
    assert len(left) == 64


def test_llm_client_chat_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.domain.services.llm_client.write_llm_call_log", lambda **kwargs: None)
    _FakeClient.next_response = _FakeResponse(
        status_code=200,
        payload={
            "id": "chatcmpl_test",
            "choices": [{"message": {"content": "{\"analysis_type\":\"NDVI\"}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
        headers={"x-request-id": "req_test_123"},
    )
    monkeypatch.setattr("packages.domain.services.llm_client.httpx.Client", _FakeClient)

    settings = Settings(
        llm_api_key="test_key",
        llm_base_url="https://example.com/v1",
        llm_max_retries=0,
    )
    client = LLMClient(settings)

    response = client.chat_json(system_prompt="you are parser", user_prompt="分析北京夏季 NDVI")

    assert response.model == settings.llm_model
    assert response.request_id == "req_test_123"
    assert response.content_json["analysis_type"] == "NDVI"
    assert response.usage.total_tokens == 20


def test_llm_client_writes_success_log(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[dict] = []

    def _fake_write(**kwargs):  # noqa: ANN001
        written.append(kwargs)

    monkeypatch.setattr("packages.domain.services.llm_client.write_llm_call_log", _fake_write)
    _FakeClient.next_response = _FakeResponse(
        status_code=200,
        payload={
            "id": "chatcmpl_test",
            "choices": [{"message": {"content": "{\"analysis_type\":\"NDVI\"}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
        headers={"x-request-id": "req_test_123"},
    )
    monkeypatch.setattr("packages.domain.services.llm_client.httpx.Client", _FakeClient)

    settings = Settings(
        llm_api_key="test_key",
        llm_base_url="https://example.com/v1",
        llm_max_retries=0,
    )
    client = LLMClient(settings)

    client.chat_json(system_prompt="sys", user_prompt="usr", phase="parse", task_id="task_123")

    assert written
    assert written[0]["phase"] == "parse"
    assert written[0]["task_id"] == "task_123"
    assert written[0]["status"] == "success"


def test_llm_client_requires_api_key() -> None:
    settings = Settings(llm_api_key=None, llm_base_url="https://example.com/v1")
    client = LLMClient(settings)

    with pytest.raises(LLMClientError) as exc_info:
        client.chat_json(system_prompt="system", user_prompt="user")

    assert "API key" in str(exc_info.value)


def test_llm_client_raises_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[dict] = []

    def _fake_write(**kwargs):  # noqa: ANN001
        written.append(kwargs)

    monkeypatch.setattr("packages.domain.services.llm_client.write_llm_call_log", _fake_write)
    _FakeClient.next_response = _FakeResponse(
        status_code=500,
        payload={"error": {"message": "server_error"}},
    )
    monkeypatch.setattr("packages.domain.services.llm_client.httpx.Client", _FakeClient)

    settings = Settings(
        llm_api_key="test_key",
        llm_base_url="https://example.com/v1",
        llm_max_retries=0,
    )
    client = LLMClient(settings)

    with pytest.raises(LLMClientError) as exc_info:
        client.chat_json(system_prompt="system", user_prompt="user", phase="plan", task_id="task_456")

    assert exc_info.value.status_code == 500
    assert written
    assert written[0]["status"] == "failed"
    assert written[0]["phase"] == "plan"
    assert written[0]["task_id"] == "task_456"


def test_llm_client_writes_failed_log_for_invalid_json_content(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[dict] = []

    def _fake_write(**kwargs):  # noqa: ANN001
        written.append(kwargs)

    monkeypatch.setattr("packages.domain.services.llm_client.write_llm_call_log", _fake_write)
    _FakeClient.next_response = _FakeResponse(
        status_code=200,
        payload={
            "id": "chatcmpl_bad_json",
            "choices": [{"message": {"content": "not-json"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        },
        headers={"x-request-id": "req_bad_json"},
    )
    monkeypatch.setattr("packages.domain.services.llm_client.httpx.Client", _FakeClient)

    settings = Settings(
        llm_api_key="test_key",
        llm_base_url="https://example.com/v1",
        llm_max_retries=0,
    )
    client = LLMClient(settings)

    with pytest.raises(LLMClientError) as exc_info:
        client.chat_json(
            system_prompt="system",
            user_prompt="user",
            phase="react_step",
            task_id="task_789",
        )

    assert "not valid JSON text" in str(exc_info.value)
    assert written
    assert written[0]["status"] == "failed"
    assert written[0]["phase"] == "react_step"
    assert written[0]["task_id"] == "task_789"
    assert "not valid JSON text" in str(written[0]["error_message"])
