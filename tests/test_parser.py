import pytest

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.services.llm_client import LLMResponse, LLMUsage
from packages.domain.services.parser import (
    extract_parser_failure_error,
    is_followup_request,
    parse_followup_override_message,
    parse_task_message,
)


@pytest.fixture(autouse=True)
def _default_parser_mode_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", "true")
    monkeypatch.delenv("GIS_AGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_SCHEMA_RETRIES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_parser_default_does_not_enable_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", raising=False)
    get_settings.cache_clear()

    assert get_settings().llm_parser_legacy_fallback is False
    get_settings.cache_clear()


def test_parse_time_range_and_outputs() -> None:
    parsed = parse_task_message(
        "帮我计算 2024 年 6 到 8 月 bbox(116.1, 39.8, 116.5, 40.1) 的 NDVI，并导出地图和 GeoTIFF。",
        has_upload=False,
    )

    assert parsed.need_confirmation is False
    assert parsed.time_range == {"start": "2024-06-01", "end": "2024-08-31"}
    assert "geotiff" in parsed.preferred_output
    assert parsed.analysis_type == "NDVI"


def test_parse_place_alias_sets_named_aoi_source_type() -> None:
    parsed = parse_task_message("帮我计算 2024 年 6 到 8 月北京西山的 NDVI。", has_upload=False)

    assert parsed.need_confirmation is False
    assert parsed.aoi_source_type == "place_alias"


def test_parse_admin_name_sets_admin_source_type() -> None:
    parsed = parse_task_message("帮我计算 2024 年 6 到 8 月海淀区的 NDVI。", has_upload=False)

    assert parsed.need_confirmation is False
    assert parsed.aoi_source_type == "admin_name"


def test_parse_missing_time_requires_clarification() -> None:
    parsed = parse_task_message("帮我算北京西山 NDVI。", has_upload=False)

    assert parsed.need_confirmation is True
    assert "time_range" in parsed.missing_fields


def test_parse_iso_date_range_with_bbox() -> None:
    parsed = parse_task_message(
        "帮我分析 bbox(116.1,39.8,116.5,40.1) 在 2024-06-01 到 2024-06-30 的 NDVI。",
        has_upload=False,
    )

    assert parsed.need_confirmation is False
    assert parsed.time_range == {"start": "2024-06-01", "end": "2024-06-30"}
    assert parsed.aoi_source_type == "bbox"
    assert parsed.aoi_input == "bbox(116.1,39.8,116.5,40.1)"


def test_parse_chinese_day_range_with_repeated_year() -> None:
    parsed = parse_task_message(
        "请分析 2024年6月1日到2024年6月30日 海淀区 NDVI。",
        has_upload=False,
    )

    assert parsed.need_confirmation is False
    assert parsed.time_range == {"start": "2024-06-01", "end": "2024-06-30"}
    assert parsed.aoi_source_type == "admin_name"
    assert parsed.aoi_input == "海淀区"


def test_parse_chinese_day_range_without_repeated_year() -> None:
    parsed = parse_task_message(
        "请分析 2024年6月1日到6月30日 北京西山 NDVI。",
        has_upload=False,
    )

    assert parsed.need_confirmation is False
    assert parsed.time_range == {"start": "2024-06-01", "end": "2024-06-30"}
    assert parsed.aoi_source_type == "place_alias"
    assert parsed.aoi_input == "北京西山"


@pytest.mark.parametrize(
    ("message", "expected_range"),
    [
        ("请看 2024/06/01 至 2024/06/15 北京西山 NDVI。", {"start": "2024-06-01", "end": "2024-06-15"}),
        ("请看 2024.06.01 至 2024.06.15 北京西山 NDVI。", {"start": "2024-06-01", "end": "2024-06-15"}),
        ("请看 2024年6月 北京西山 NDVI。", {"start": "2024-06-01", "end": "2024-06-30"}),
        ("请看 2024年夏季 北京西山 NDVI。", {"start": "2024-06-01", "end": "2024-08-31"}),
    ],
)
def test_parse_multiple_time_expressions(message: str, expected_range: dict[str, str]) -> None:
    parsed = parse_task_message(message, has_upload=False)

    assert parsed.need_confirmation is False
    assert parsed.time_range == expected_range
    assert parsed.aoi_input == "北京西山"


def test_parse_followup_override_for_time_only() -> None:
    override = parse_followup_override_message("换成 2023 年 6 月", has_upload=False)

    assert override == {"time_range": {"start": "2023-06-01", "end": "2023-06-30"}}


def test_parse_followup_override_for_dataset_and_output() -> None:
    override = parse_followup_override_message("改成 Landsat，并重新导出 GeoTIFF", has_upload=False)

    assert override["requested_dataset"] == "landsat89"
    assert override["preferred_output"] == ["geotiff"]


def test_parse_followup_override_with_bbox_change() -> None:
    override = parse_followup_override_message(
        "换成 bbox(117.0,39.7,117.2,39.9)，并重新跑一遍",
        has_upload=False,
    )

    assert override["aoi_input"] == "bbox(117.0,39.7,117.2,39.9)"
    assert override["aoi_source_type"] == "bbox"


def test_parse_followup_override_with_uploaded_aoi_only() -> None:
    override = parse_followup_override_message("继续，直接用刚上传的边界", has_upload=True)

    assert override["aoi_input"] == "uploaded_aoi"
    assert override["aoi_source_type"] == "file_upload"


@pytest.mark.parametrize(
    ("message", "expected_dataset"),
    [
        ("帮我分析 2024年6月 北京西山的 Sentinel-2 NDVI。", "sentinel2"),
        ("帮我分析 2024年6月 北京西山的哨兵二号 NDVI。", "sentinel2"),
        ("帮我分析 2024年6月 北京西山的 Landsat NDVI。", "landsat89"),
    ],
)
def test_parse_requested_dataset_hints(message: str, expected_dataset: str) -> None:
    parsed = parse_task_message(message, has_upload=False)

    assert parsed.requested_dataset == expected_dataset


def test_parse_task_message_with_uploaded_aoi_only() -> None:
    parsed = parse_task_message("请帮我计算 2024年6月 的 NDVI。", has_upload=True)

    assert parsed.need_confirmation is False
    assert parsed.aoi_input == "uploaded_aoi"
    assert parsed.aoi_source_type == "file_upload"


def test_parse_task_message_with_uploaded_aoi_hint_prefers_file_upload() -> None:
    parsed = parse_task_message("请基于上传的边界分析 2024年6月 NDVI。", has_upload=True)

    assert parsed.need_confirmation is False
    assert parsed.aoi_input == "uploaded_aoi"
    assert parsed.aoi_source_type == "file_upload"


def test_parse_task_message_with_upload_and_explicit_bbox_prefers_bbox() -> None:
    parsed = parse_task_message(
        "请基于上传的边界，但实际按 bbox(116.1,39.8,116.5,40.1) 分析 2024年6月 NDVI。",
        has_upload=True,
    )

    assert parsed.need_confirmation is False
    assert parsed.aoi_input == "bbox(116.1,39.8,116.5,40.1)"
    assert parsed.aoi_source_type == "bbox"


def test_parse_winter_season_handles_cross_year_leap_february() -> None:
    parsed = parse_task_message("请看 2023年冬季 北京西山 NDVI。", has_upload=False)

    assert parsed.need_confirmation is False
    assert parsed.time_range == {"start": "2023-12-01", "end": "2024-02-29"}
    assert parsed.aoi_input == "北京西山"


@pytest.mark.parametrize(
    ("message", "expected_priority"),
    [
        ("换成 2023 年 6 月，想看地块细节", "spatial"),
        ("换成 2023 年 6 月，做跨多年连续性对比", "temporal"),
    ],
)
def test_parse_followup_override_updates_user_priority(message: str, expected_priority: str) -> None:
    override = parse_followup_override_message(message, has_upload=False)

    assert override["user_priority"] == expected_priority


def test_parse_followup_override_with_place_alias_change() -> None:
    override = parse_followup_override_message("换成北京西山，重新跑一次", has_upload=False)

    assert override["aoi_input"] == "北京西山"
    assert override["aoi_source_type"] == "place_alias"


def test_is_followup_request_detects_common_followup_phrases() -> None:
    assert is_followup_request("换成 2023 年 6 月")
    assert is_followup_request("重新导出 GeoTIFF")
    assert is_followup_request("改成 Landsat")


def _mock_llm_response(payload: dict) -> LLMResponse:
    return LLMResponse(
        model="gpt-4o-mini",
        request_id="req_mock",
        content_text=str(payload),
        content_json=payload,
        usage=LLMUsage(input_tokens=10, output_tokens=10, total_tokens=20),
        latency_ms=12,
        raw_payload={"choices": [{"message": {"content": str(payload)}}]},
    )


def test_parse_task_message_uses_llm_main_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", "false")
    get_settings.cache_clear()

    called = {"count": 0}

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self, kwargs
        called["count"] += 1
        return _mock_llm_response(
            {
                "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
                "aoi_source_type": "bbox",
                "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
                "requested_dataset": "sentinel2",
                "analysis_type": "NDVI",
                "preferred_output": ["png_map", "geotiff", "methods_text"],
                "user_priority": "balanced",
                "need_confirmation": False,
                "missing_fields": [],
                "clarification_message": None,
                "operation_params": {},
            }
        )

    monkeypatch.setattr("packages.domain.services.parser.LLMClient.chat_json", _fake_chat_json)
    parsed = parse_task_message("请帮我分析 2024年6月 bbox(116.1,39.8,116.5,40.1) NDVI。", has_upload=False)

    assert called["count"] == 1
    assert parsed.need_confirmation is False
    assert parsed.aoi_source_type == "bbox"
    assert parsed.requested_dataset == "sentinel2"
    assert parsed.analysis_type == "NDVI"
    assert parsed.operation_params["index"] == "ndvi"
    assert (parsed.created_from or "").startswith("llm:")
    get_settings.cache_clear()


def test_parse_task_message_retries_on_schema_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_SCHEMA_RETRIES", "2")
    get_settings.cache_clear()

    prompts: list[str] = []
    responses = [
        _mock_llm_response(
            {
                "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
                "aoi_source_type": "bbox",
                "time_range": {"start": "2024-06-01"},
                "analysis_type": "NDVI",
                "preferred_output": ["png_map"],
                "user_priority": "balanced",
                "need_confirmation": False,
                "missing_fields": [],
                "clarification_message": None,
                "operation_params": {},
            }
        ),
        _mock_llm_response(
            {
                "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
                "aoi_source_type": "bbox",
                "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
                "analysis_type": "NDVI",
                "preferred_output": ["png_map"],
                "user_priority": "balanced",
                "need_confirmation": False,
                "missing_fields": [],
                "clarification_message": None,
                "operation_params": {},
            }
        ),
    ]

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self
        prompts.append(kwargs["user_prompt"])
        return responses.pop(0)

    monkeypatch.setattr("packages.domain.services.parser.LLMClient.chat_json", _fake_chat_json)
    parsed = parse_task_message("请帮我分析 2024年6月 bbox(116.1,39.8,116.5,40.1) NDVI。", has_upload=False)

    assert parsed.need_confirmation is False
    assert len(prompts) == 2
    assert "repair_invalid_json_output" in prompts[1]
    get_settings.cache_clear()


def test_parse_task_message_writes_back_clarification_after_repair_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_API_KEY", "test_key")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_SCHEMA_RETRIES", "1")
    get_settings.cache_clear()

    def _fake_chat_json(self, **kwargs):  # noqa: ANN001
        del self, kwargs
        return _mock_llm_response(
            {
                "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
                "aoi_source_type": "bbox",
                "time_range": {"start": "2024-06-01"},
                "analysis_type": "NDVI",
                "preferred_output": ["png_map"],
                "user_priority": "balanced",
                "need_confirmation": False,
                "missing_fields": [],
                "clarification_message": None,
                "operation_params": {},
            }
        )

    monkeypatch.setattr("packages.domain.services.parser.LLMClient.chat_json", _fake_chat_json)
    parsed = parse_task_message("请帮我分析 NDVI。", has_upload=False)

    assert parsed.need_confirmation is True
    assert "任务解析失败" in (parsed.clarification_message or "")
    assert (parsed.created_from or "").startswith("llm_parse_failed")
    error_code, error_message = extract_parser_failure_error(parsed)
    assert error_code == ErrorCode.TASK_LLM_PARSER_SCHEMA_VALIDATION_FAILED
    assert error_message is not None
    get_settings.cache_clear()
