from packages.domain.services.explanation import ExplanationContext, build_methods_text, build_summary_text


def test_build_methods_text_for_real_pipeline_includes_execution_details() -> None:
    text = build_methods_text(
        ExplanationContext(
            dataset_name="sentinel2",
            mode="real",
            aoi_input="bbox(116.1,39.8,116.5,40.1)",
            requested_time_range={"start": "2024-06-01", "end": "2024-06-30"},
            actual_time_range={"start": "2024-06-03", "end": "2024-06-27"},
            preferred_output=["png_map", "methods_text", "geotiff"],
            output_artifact_types=["png_map", "geotiff", "methods_md", "summary_md"],
            item_count=4,
            valid_pixel_ratio=0.83,
            ndvi_min=-0.12,
            ndvi_max=0.86,
            ndvi_mean=0.41,
            output_width=256,
            output_height=192,
            output_crs="EPSG:32650",
        )
    )

    assert "Sentinel-2" in text
    assert "实际执行时间窗为 2024-06-03 至 2024-06-27" in text
    assert "4 景" in text
    assert "256x192" in text
    assert "GeoTIFF" in text


def test_build_summary_text_for_baseline_pipeline_includes_recommendation_and_risk() -> None:
    text = build_summary_text(
        ExplanationContext(
            dataset_name="landsat89",
            mode="baseline",
            aoi_input="bbox(116.1,39.8,116.5,40.1)",
            requested_time_range={"start": "2024-06-01", "end": "2024-06-30"},
            actual_time_range={"start": "2024-06-01", "end": "2024-06-30"},
            preferred_output=["png_map", "methods_text"],
            valid_pixel_ratio=1.0,
            ndvi_mean=0.512,
            recommendation_reason="用户明确指定使用 Landsat 8/9，因此本次按该数据源执行。",
            recommendation_risk_note="当前是按用户指定数据源执行；Sentinel-2 仍可能是更稳妥的备选。",
        )
    )

    assert "Landsat 8/9" in text
    assert "baseline 输出" in text
    assert "推荐依据" in text
    assert "风险提示" in text


def test_build_summary_text_marks_fallback_usage() -> None:
    text = build_summary_text(
        ExplanationContext(
            dataset_name="sentinel2",
            mode="baseline",
            aoi_input="bbox(116.1,39.8,116.5,40.1)",
            requested_time_range={"start": "2024-06-01", "end": "2024-06-30"},
            actual_time_range={"start": "2024-06-01", "end": "2024-06-30"},
            fallback_used=True,
        )
    )

    assert "fallback" in text
