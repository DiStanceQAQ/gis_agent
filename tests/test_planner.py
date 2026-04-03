import pytest

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.services.llm_client import LLMResponse, LLMUsage
from packages.domain.services.planner import (
    PLAN_STATUS_NEEDS_CLARIFICATION,
    PLAN_STATUS_RUNNING,
    PLAN_STATUS_READY,
    build_task_plan,
    set_task_plan_step_status,
)
from packages.domain.services.task_state import STEP_STATUS_RUNNING, STEP_STATUS_SUCCESS
from packages.schemas.task import ParsedTaskSpec


@pytest.fixture
def _legacy_planner_mode_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LLM_API_KEY", raising=False)
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_planner_default_does_not_enable_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", raising=False)
    get_settings.cache_clear()

    assert get_settings().llm_planner_legacy_fallback is False
    get_settings.cache_clear()


def test_build_task_plan_for_ready_request(
    _legacy_planner_mode_for_tests: None,
) -> None:
    parsed = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
        requested_dataset="landsat89",
    )

    plan = build_task_plan(parsed)

    assert plan.status == PLAN_STATUS_READY
    assert plan.mode == "agent_driven_gis_workspace"
    assert plan.steps[0].step_name == "plan_task"
    assert plan.steps[0].tool_name == "planner.build"
    assert plan.steps[-1].step_name == "generate_outputs"
    assert "landsat89" in plan.objective


def test_build_task_plan_for_clarification_request(
    _legacy_planner_mode_for_tests: None,
) -> None:
    parsed = ParsedTaskSpec(
        aoi_input="北京西山",
        aoi_source_type="place_alias",
        need_confirmation=True,
        missing_fields=["aoi_boundary"],
    )

    plan = build_task_plan(parsed)

    assert plan.status == PLAN_STATUS_NEEDS_CLARIFICATION
    assert plan.missing_fields == ["aoi_boundary"]


def test_set_task_plan_step_status_updates_plan_payload(
    _legacy_planner_mode_for_tests: None,
) -> None:
    plan = build_task_plan(
        ParsedTaskSpec(
            aoi_input="bbox(116.1,39.8,116.5,40.1)",
            aoi_source_type="bbox",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
        )
    )

    payload = set_task_plan_step_status(
        plan.model_dump(),
        step_name="plan_task",
        status=STEP_STATUS_RUNNING,
        detail={"tool_name": "planner.build"},
    )
    payload = set_task_plan_step_status(
        payload,
        step_name="plan_task",
        status=STEP_STATUS_SUCCESS,
        detail={"tool_name": "planner.build", "tool_count": 6},
    )

    assert payload["status"] == PLAN_STATUS_RUNNING
    assert payload["steps"][0]["status"] == STEP_STATUS_SUCCESS
    assert payload["steps"][0]["detail"] == {"tool_name": "planner.build", "tool_count": 6}


def _mock_llm_plan_response(payload: dict) -> LLMResponse:
    return LLMResponse(
        model="gpt-4o-mini",
        request_id="req_plan_mock",
        content_text=str(payload),
        content_json=payload,
        usage=LLMUsage(input_tokens=11, output_tokens=22, total_tokens=33),
        latency_ms=15,
        raw_payload={"choices": [{"message": {"content": str(payload)}}]},
    )


def _valid_plan_payload() -> dict:
    return {
        "version": "agent-v2",
        "mode": "llm_plan_execute_gis_workspace",
        "objective": "完成 NDVI 自动处理并发布结果",
        "reasoning_summary": "按标准 GIS 计划执行。",
        "missing_fields": [],
        "steps": [
            {
                "step_name": "plan_task",
                "tool_name": "planner.build",
                "title": "规划任务",
                "purpose": "生成任务计划",
                "depends_on": [],
            },
            {
                "step_name": "normalize_aoi",
                "tool_name": "aoi.normalize",
                "title": "标准化研究区",
                "purpose": "规范 AOI",
                "depends_on": ["plan_task"],
            },
            {
                "step_name": "search_candidates",
                "tool_name": "catalog.search",
                "title": "搜索候选",
                "purpose": "检索候选目录",
                "depends_on": ["normalize_aoi"],
            },
            {
                "step_name": "recommend_dataset",
                "tool_name": "recommendation.rank",
                "title": "推荐数据源",
                "purpose": "选择主备方案",
                "depends_on": ["search_candidates"],
            },
            {
                "step_name": "run_processing_pipeline",
                "tool_name": "processing.run",
                "title": "执行分析",
                "purpose": "执行 NDVI 处理",
                "depends_on": ["recommend_dataset"],
            },
            {
                "step_name": "generate_outputs",
                "tool_name": "artifacts.publish",
                "title": "发布结果",
                "purpose": "发布产物",
                "depends_on": ["run_processing_pipeline"],
            },
        ],
    }


def test_build_task_plan_uses_llm_main_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", "false")
    get_settings.cache_clear()

    called = {"count": 0}

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self, kwargs
        called["count"] += 1
        return _mock_llm_plan_response(_valid_plan_payload())

    monkeypatch.setattr("packages.domain.services.planner.LLMClient.chat_json", _fake_chat_json)
    parsed = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
    )
    plan = build_task_plan(parsed)

    assert called["count"] == 1
    assert plan.mode == "llm_plan_execute_gis_workspace"
    assert [step.step_name for step in plan.steps] == [
        "plan_task",
        "normalize_aoi",
        "search_candidates",
        "recommend_dataset",
        "run_processing_pipeline",
        "generate_outputs",
    ]
    get_settings.cache_clear()


def test_build_task_plan_retries_on_schema_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_SCHEMA_RETRIES", "2")
    get_settings.cache_clear()

    prompts: list[str] = []
    invalid_payload = _valid_plan_payload()
    invalid_payload["steps"] = invalid_payload["steps"][:-1]
    responses = [
        _mock_llm_plan_response(invalid_payload),
        _mock_llm_plan_response(_valid_plan_payload()),
    ]

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self
        prompts.append(kwargs["user_prompt"])
        return responses.pop(0)

    monkeypatch.setattr("packages.domain.services.planner.LLMClient.chat_json", _fake_chat_json)
    parsed = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
    )
    plan = build_task_plan(parsed)

    assert plan.steps[-1].step_name == "generate_outputs"
    assert len(prompts) == 2
    assert "repair_invalid_json_output" in prompts[1]
    get_settings.cache_clear()


def test_build_task_plan_falls_back_to_legacy_when_llm_unavailable(
    _legacy_planner_mode_for_tests: None,
) -> None:
    parsed = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
    )
    plan = build_task_plan(parsed)

    assert plan.mode == "agent_driven_gis_workspace"
    assert plan.status == PLAN_STATUS_READY


def test_build_task_plan_returns_error_code_when_schema_validation_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_SCHEMA_RETRIES", "0")
    get_settings.cache_clear()

    invalid_payload = _valid_plan_payload()
    invalid_payload["steps"] = invalid_payload["steps"][:-2]

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self, kwargs
        return _mock_llm_plan_response(invalid_payload)

    monkeypatch.setattr("packages.domain.services.planner.LLMClient.chat_json", _fake_chat_json)
    parsed = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
    )
    plan = build_task_plan(parsed)

    assert plan.status == PLAN_STATUS_NEEDS_CLARIFICATION
    assert plan.error_code == ErrorCode.TASK_LLM_PLANNER_SCHEMA_VALIDATION_FAILED
    assert plan.error_message is not None
    get_settings.cache_clear()
