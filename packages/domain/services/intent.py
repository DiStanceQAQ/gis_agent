from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.logging import get_logger
from packages.domain.services.llm_client import LLMClient

logger = get_logger(__name__)

IntentLabel = Literal["chat", "task", "ambiguous"]

CONFIRMATION_KEYWORDS = (
    "确认",
    "继续",
    "好的",
    "好的呀",
    "好的呢",
    "没问题",
    "收到",
    "可以",
    "行",
    "行吧",
    "行的",
    "ok",
    "okay",
    "yes",
    "sure",
)

_CONFIRMATION_STRIP_PATTERN = re.compile(r"[\s，。！？!?、,.;；:：·\"'“”‘’（）()\[\]{}<>…\-]+")
def _normalize_confirmation_text(text: str) -> str:
    return _CONFIRMATION_STRIP_PATTERN.sub("", text).lower()


def _load_confirmation_keywords() -> tuple[str, ...]:
    configured = tuple(
        keyword.strip()
        for keyword in get_settings().intent_confirmation_keywords.split(",")
        if keyword.strip()
    )
    merged = (*CONFIRMATION_KEYWORDS, *configured)
    # Keep original order while removing duplicates.
    return tuple(dict.fromkeys(merged))


def _build_confirmation_pattern() -> re.Pattern[str] | None:
    normalized_keywords = [
        _normalize_confirmation_text(keyword)
        for keyword in _load_confirmation_keywords()
    ]
    normalized_keywords = [keyword for keyword in normalized_keywords if keyword]
    if not normalized_keywords:
        return None
    ordered = sorted(set(normalized_keywords), key=len, reverse=True)
    return re.compile(rf"^(?:{'|'.join(re.escape(keyword) for keyword in ordered)})+$", re.IGNORECASE)


@dataclass(frozen=True)
class IntentResult:
    intent: IntentLabel
    confidence: float
    reason: str


def is_task_confirmation_message(message: str) -> bool:
    normalized = _normalize_confirmation_text(message)
    if not normalized:
        return False
    pattern = _build_confirmation_pattern()
    if pattern is None:
        return False
    return pattern.fullmatch(normalized) is not None


def _load_intent_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "intent.md"
    return prompt_path.read_text(encoding="utf-8")


def _build_intent_user_prompt(message: str, history: list[dict[str, str]]) -> str:
    payload = {
        "message": message,
        "history": history,
        "task_confirmation_hint": is_task_confirmation_message(message),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fallback_intent_result(reason: str) -> IntentResult:
    return IntentResult(intent="ambiguous", confidence=0.0, reason=reason)


def _coerce_intent(payload: object) -> IntentResult | None:
    if not isinstance(payload, dict):
        return None

    raw_intent = payload.get("intent")
    if not isinstance(raw_intent, str):
        return None
    raw_intent = raw_intent.strip().lower()
    if raw_intent not in {"chat", "task", "ambiguous"}:
        return None

    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        return None
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None

    raw_reason = payload.get("reason")
    if not isinstance(raw_reason, str):
        return None
    reason = raw_reason.strip()
    if not reason:
        return None

    return IntentResult(intent=raw_intent, confidence=confidence, reason=reason)


def _confirmation_fallback_result() -> IntentResult:
    return IntentResult(
        intent="task",
        confidence=0.99,
        reason="The message is an explicit confirmation, and intent routing fell back locally after an LLM failure.",
    )


def classify_message_intent(
    message: str,
    *,
    history: list[dict[str, str]],
    task_id: str | None = None,
    db_session: Session | None = None,
) -> IntentResult:
    system_prompt = _load_intent_system_prompt()
    user_prompt = _build_intent_user_prompt(message, history)

    try:
        response = LLMClient().chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            phase="intent",
            task_id=task_id,
            db_session=db_session,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("intent.classification_failed", exc_info=True)
        if is_task_confirmation_message(message):
            return _confirmation_fallback_result()
        return _fallback_intent_result(
            f"LLM intent classification failed; falling back to ambiguous. {exc}"
        )

    result = _coerce_intent(response.content_json)
    if result is None:
        if is_task_confirmation_message(message):
            return _confirmation_fallback_result()
        return _fallback_intent_result("LLM intent classification returned an invalid payload.")
    return result
