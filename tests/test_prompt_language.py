from __future__ import annotations

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "packages" / "domain" / "prompts"


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def test_intent_prompt_is_chinese_led_but_keeps_schema_tokens() -> None:
    content = _read_prompt("intent.md")

    assert "你是 GIS Agent 的意图路由器。" in content
    assert "`intent`" in content
    assert "`task`" in content
    assert "`chat`" in content
    assert "`ambiguous`" in content


def test_chat_prompt_is_chinese_led_but_keeps_reply_key() -> None:
    content = _read_prompt("chat.md")

    assert "你是一个有帮助的 GIS 助手。" in content
    assert "`reply`" in content


def test_chat_stream_prompt_is_chinese_led() -> None:
    content = _read_prompt("chat_stream.md")

    assert "你是一个有帮助的 GIS 助手。" in content
    assert "只返回纯文本。" in content
