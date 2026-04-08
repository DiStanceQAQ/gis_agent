import pytest

from packages.domain.config import Settings


def test_llm_parser_planner_defaults_are_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", raising=False)

    settings = Settings(_env_file=None)

    assert settings.llm_parser_legacy_fallback is False
    assert settings.llm_planner_legacy_fallback is False


def test_intent_router_defaults_are_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GIS_AGENT_INTENT_ROUTER_ENABLED",
        "GIS_AGENT_INTENT_TASK_CONFIDENCE_THRESHOLD",
        "GIS_AGENT_INTENT_TASK_CONFIRMATION_REQUIRED",
        "GIS_AGENT_INTENT_HISTORY_LIMIT",
        "GIS_AGENT_INTENT_CONFIRMATION_KEYWORDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.intent_router_enabled is True
    assert settings.intent_task_confidence_threshold == 0.75
    assert settings.intent_task_confirmation_required is False
    assert settings.intent_history_limit == 8
    assert settings.intent_confirmation_keywords == "开始执行,按这个执行,确认执行,就按这个来"


def test_local_files_only_mode_defaults_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LOCAL_FILES_ONLY_MODE", raising=False)

    settings = Settings(_env_file=None)

    assert settings.local_files_only_mode is True


def test_conversation_understanding_feature_flags_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIS_AGENT_CONVERSATION_CONTEXT_ENABLED", raising=False)
    monkeypatch.delenv("GIS_AGENT_UNDERSTANDING_ENGINE_ENABLED", raising=False)
    monkeypatch.delenv("GIS_AGENT_TASK_REVISIONS_ENABLED", raising=False)
    monkeypatch.delenv("GIS_AGENT_RESPONSE_MODE_ENABLED", raising=False)
    monkeypatch.delenv("GIS_AGENT_MESSAGE_UNDERSTANDING_PAYLOAD_ENABLED", raising=False)
    monkeypatch.delenv("GIS_AGENT_REVISION_BACKFILL_LAZY_ENABLED", raising=False)

    settings = Settings(_env_file=None)

    assert settings.conversation_context_enabled is True
    assert settings.understanding_engine_enabled is True
    assert settings.task_revisions_enabled is True
    assert settings.response_mode_enabled is True
    assert settings.message_understanding_payload_enabled is True
    assert settings.revision_backfill_lazy_enabled is True
    assert settings.understanding_intent_medium_threshold == 0.60
    assert settings.understanding_intent_high_threshold == 0.85
