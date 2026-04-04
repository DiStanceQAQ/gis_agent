from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from packages.domain.logging import get_logger
from packages.domain.services.llm_client import LLMClient

logger = get_logger(__name__)

CHAT_FALLBACK_TEXT = "抱歉，我现在无法生成回复，请稍后再试。"


def _load_chat_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "chat.md"
    return prompt_path.read_text(encoding="utf-8")


def _build_chat_user_prompt(user_message: str, history: list[dict[str, str]]) -> str:
    payload = {
        "user_message": user_message,
        "history": history,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _extract_reply(content_json: dict[str, object]) -> str | None:
    reply = content_json.get("reply")
    if not isinstance(reply, str):
        return None
    text = reply.strip()
    return text or None


def generate_chat_reply(
    *,
    user_message: str,
    history: list[dict[str, str]],
    task_id: str | None = None,
    db_session: Session | None = None,
) -> str:
    system_prompt = _load_chat_system_prompt()
    user_prompt = _build_chat_user_prompt(user_message, history)

    try:
        response = LLMClient().chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            phase="chat",
            task_id=task_id,
            db_session=db_session,
        )
    except Exception:  # pragma: no cover - defensive fallback
        logger.warning("chat.reply_failed", exc_info=True)
        return CHAT_FALLBACK_TEXT

    reply = _extract_reply(response.content_json)
    if reply is None:
        logger.warning("chat.reply_missing_field")
        return CHAT_FALLBACK_TEXT
    return reply
