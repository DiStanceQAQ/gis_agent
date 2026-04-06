import pytest

from packages.domain.config import Settings


def test_llm_parser_planner_defaults_are_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", raising=False)

    settings = Settings()

    assert settings.llm_parser_legacy_fallback is False
    assert settings.llm_planner_legacy_fallback is False


def test_intent_router_defaults_are_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GIS_AGENT_INTENT_ROUTER_ENABLED",
        "GIS_AGENT_INTENT_TASK_CONFIDENCE_THRESHOLD",
        "GIS_AGENT_INTENT_HISTORY_LIMIT",
        "GIS_AGENT_INTENT_CONFIRMATION_KEYWORDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.intent_router_enabled is True
    assert settings.intent_task_confidence_threshold == 0.75
    assert settings.intent_history_limit == 8
    assert settings.intent_confirmation_keywords == "开始执行,按这个执行,确认执行,就按这个来"


def test_local_files_only_mode_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LOCAL_FILES_ONLY_MODE", raising=False)

    settings = Settings()

    assert settings.local_files_only_mode is False
