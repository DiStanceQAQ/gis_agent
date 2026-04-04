from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy.orm import Session

from packages.domain.config import Settings, get_settings
from packages.domain.logging import get_logger
from packages.domain.services.llm_call_logs import write_llm_call_log

logger = get_logger(__name__)


@dataclass
class LLMUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class LLMResponse:
    model: str
    request_id: str | None
    content_text: str
    content_json: dict[str, Any]
    usage: LLMUsage
    latency_ms: int
    raw_payload: dict[str, Any]


class LLMClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail or {}


def build_prompt_hash(*, system_prompt: str, user_prompt: str, model: str) -> str:
    payload = f"{model}\n{system_prompt}\n{user_prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.llm_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self.settings.llm_api_key:
            raise LLMClientError("LLM API key is missing.", detail={"config_key": "GIS_AGENT_LLM_API_KEY"})
        return {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        phase: str = "unknown",
        task_id: str | None = None,
        db_session: Session | None = None,
    ) -> LLMResponse:
        resolved_model = model or self.settings.llm_model
        resolved_temperature = (
            self.settings.llm_temperature if temperature is None else temperature
        )
        prompt_hash = build_prompt_hash(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=resolved_model,
        )
        payload: dict[str, Any] = {
            "model": resolved_model,
            "temperature": resolved_temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        endpoint = f"{self.base_url}/chat/completions"
        attempt = 0
        max_retries = max(0, self.settings.llm_max_retries)

        def _write_log(
            *,
            status: str,
            latency_ms: int,
            request_id: str | None = None,
            input_tokens: int | None = None,
            output_tokens: int | None = None,
            total_tokens: int | None = None,
            error_message: str | None = None,
        ) -> None:
            write_llm_call_log(
                task_id=task_id,
                phase=phase,
                model_name=resolved_model,
                request_id=request_id,
                prompt_hash=prompt_hash,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                status=status,
                error_message=error_message,
                db_session=db_session,
            )

        try:
            headers = self._headers()
        except LLMClientError as exc:
            _write_log(
                status="failed",
                latency_ms=0,
                error_message=str(exc),
            )
            raise

        while True:
            started = perf_counter()
            try:
                with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
                    response = client.post(endpoint, headers=headers, json=payload)
                latency_ms = int((perf_counter() - started) * 1000)
            except httpx.TimeoutException as exc:
                retryable = attempt < max_retries
                if retryable:
                    attempt += 1
                    time.sleep(min(0.8 * attempt, 2.0))
                    continue
                latency_ms = int((perf_counter() - started) * 1000)
                message = "LLM request timed out."
                _write_log(
                    status="failed",
                    latency_ms=latency_ms,
                    error_message=message,
                )
                raise LLMClientError(
                    message,
                    retryable=False,
                    detail={"attempt": attempt + 1, "timeout_seconds": self.settings.llm_timeout_seconds},
                ) from exc
            except httpx.HTTPError as exc:
                retryable = attempt < max_retries
                if retryable:
                    attempt += 1
                    time.sleep(min(0.8 * attempt, 2.0))
                    continue
                latency_ms = int((perf_counter() - started) * 1000)
                message = "LLM request failed due to network error."
                _write_log(
                    status="failed",
                    latency_ms=latency_ms,
                    error_message=message,
                )
                raise LLMClientError(
                    message,
                    retryable=False,
                    detail={"attempt": attempt + 1},
                ) from exc

            if response.status_code >= 400:
                retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
                if retryable and attempt < max_retries:
                    attempt += 1
                    time.sleep(min(0.8 * attempt, 2.0))
                    continue
                request_id = response.headers.get("x-request-id")
                message = f"LLM request returned status {response.status_code}."
                _write_log(
                    status="failed",
                    latency_ms=latency_ms,
                    request_id=request_id,
                    error_message=f"{message} body={response.text[:800]}",
                )
                raise LLMClientError(
                    "LLM request returned an error response.",
                    status_code=response.status_code,
                    retryable=False,
                    detail={"body": response.text[:800], "attempt": attempt + 1},
                )

            try:
                data = response.json()
            except ValueError as exc:
                request_id = response.headers.get("x-request-id")
                message = "LLM response is not valid JSON."
                _write_log(
                    status="failed",
                    latency_ms=latency_ms,
                    request_id=request_id,
                    error_message=message,
                )
                raise LLMClientError(
                    message,
                    status_code=response.status_code,
                    retryable=False,
                    detail={"body": response.text[:400]},
                ) from exc

            choice = ((data.get("choices") or [{}])[0]).get("message") or {}
            content_text = str(choice.get("content") or "").strip()
            if not content_text:
                request_id = response.headers.get("x-request-id") or data.get("id")
                message = "LLM response is empty."
                _write_log(
                    status="failed",
                    latency_ms=latency_ms,
                    request_id=request_id,
                    error_message=message,
                )
                raise LLMClientError(
                    message,
                    status_code=response.status_code,
                    retryable=False,
                    detail={"body": json.dumps(data)[:500]},
                )
            try:
                content_json = json.loads(content_text)
            except json.JSONDecodeError as exc:
                request_id = response.headers.get("x-request-id") or data.get("id")
                message = "LLM response content is not valid JSON text."
                _write_log(
                    status="failed",
                    latency_ms=latency_ms,
                    request_id=request_id,
                    error_message=message,
                )
                raise LLMClientError(
                    message,
                    status_code=response.status_code,
                    retryable=False,
                    detail={"content_text": content_text[:500]},
                ) from exc

            usage_payload = data.get("usage") or {}
            usage = LLMUsage(
                input_tokens=usage_payload.get("prompt_tokens"),
                output_tokens=usage_payload.get("completion_tokens"),
                total_tokens=usage_payload.get("total_tokens"),
            )
            request_id = response.headers.get("x-request-id") or data.get("id")
            _write_log(
                status="success",
                latency_ms=latency_ms,
                request_id=request_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                error_message=None,
            )
            logger.info(
                "llm.chat_json.ok model=%s latency_ms=%s prompt_tokens=%s completion_tokens=%s",
                resolved_model,
                latency_ms,
                usage.input_tokens,
                usage.output_tokens,
            )
            return LLMResponse(
                model=resolved_model,
                request_id=request_id,
                content_text=content_text,
                content_json=content_json,
                usage=usage,
                latency_ms=latency_ms,
                raw_payload=data,
            )
