from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from sqlalchemy.orm import Session

from packages.domain.logging import get_logger
from packages.domain.services.llm_client import LLMClient

logger = get_logger(__name__)

IntentLabel = Literal["chat", "task", "ambiguous"]

CONFIRMATION_KEYWORDS = (
    "确认",
    "继续",
    "好的",
    "没问题",
    "收到",
    "可以",
    "ok",
    "okay",
    "yes",
    "sure",
)


@dataclass(frozen=True)
class IntentResult:
    intent: IntentLabel
    confidence: float
    reason: str


def is_task_confirmation_message(message: str) -> bool:
    normalized = re.sub(r"\s+", "", message).lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in CONFIRMATION_KEYWORDS)


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


def _coerce_intent(payload: dict[str, object]) -> IntentResult | None:
    raw_intent = str(payload.get("intent") or "").strip().lower()
    if raw_intent not in {"chat", "task", "ambiguous"}:
        return None

    raw_confidence = payload.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None

    raw_reason = payload.get("reason", "")
    reason = str(raw_reason).strip()
    if not reason:
        return None

    return IntentResult(intent=cast(IntentLabel, raw_intent), confidence=confidence, reason=reason)


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
        return _fallback_intent_result(
            f"LLM intent classification failed; falling back to ambiguous. {exc}"
        )

    result = _coerce_intent(response.content_json)
    if result is None:
        return _fallback_intent_result("LLM intent classification returned an invalid payload.")
    return result
