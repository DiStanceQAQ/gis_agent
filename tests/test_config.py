import pytest

from packages.domain.config import get_settings


def test_llm_parser_planner_defaults_are_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_parser_legacy_fallback is False
    assert settings.llm_planner_legacy_fallback is False

