from packages.domain.services.parser import parse_task_message


def test_parse_time_range_and_outputs() -> None:
    parsed = parse_task_message(
        "帮我计算 2024 年 6 到 8 月北京西山的 NDVI，并导出地图和 GeoTIFF。",
        has_upload=False,
    )

    assert parsed.need_confirmation is False
    assert parsed.time_range == {"start": "2024-06-01", "end": "2024-08-31"}
    assert "geotiff" in parsed.preferred_output
    assert parsed.analysis_type == "NDVI"


def test_parse_missing_time_requires_clarification() -> None:
    parsed = parse_task_message("帮我算北京西山 NDVI。", has_upload=False)

    assert parsed.need_confirmation is True
    assert "time_range" in parsed.missing_fields

