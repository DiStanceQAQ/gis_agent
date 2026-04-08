from __future__ import annotations

from packages.schemas.task import ParsedTaskSpec

from packages.domain.services.operation_plan_builder import build_operation_plan_from_registry


def test_build_operation_plan_for_clip_request_uses_registry_chain() -> None:
    parsed = ParsedTaskSpec(
        analysis_type="CLIP",
        operation_params={
            "operations": ["raster.clip"],
            "source_path": "/tmp/source.tif",
            "clip_path": "/tmp/aoi.geojson",
            "crop": True,
        },
        preferred_output=["geotiff"],
    )

    plan = build_operation_plan_from_registry(parsed, status="draft", version=1, missing_fields=[])

    assert [node.op_name for node in plan.nodes] == ["raster.clip", "artifact.export"]
    clip_node, export_node = plan.nodes
    assert clip_node.params["source_path"] == "/tmp/source.tif"
    assert clip_node.params["clip_path"] == "/tmp/aoi.geojson"
    assert export_node.inputs["primary"] == clip_node.outputs["raster"]
    assert export_node.params["formats"] == ["geotiff"]


def test_build_operation_plan_for_ndvi_uses_band_math_then_export() -> None:
    parsed = ParsedTaskSpec(
        analysis_type="NDVI",
        operation_params={
            "operations": ["raster.band_math"],
            "expression": "(nir-red)/(nir+red)",
            "source_path": "/tmp/source.tif",
        },
        preferred_output=["geotiff", "png_map"],
    )

    plan = build_operation_plan_from_registry(parsed, status="draft", version=1, missing_fields=[])

    assert [node.op_name for node in plan.nodes] == ["raster.band_math", "artifact.export"]
    assert plan.nodes[0].params["expression"] == "(nir-red)/(nir+red)"
    assert plan.nodes[0].params["source_path"] == "/tmp/source.tif"
    assert plan.nodes[1].inputs["primary"] == plan.nodes[0].outputs["raster"]
    assert plan.nodes[1].params["formats"] == ["geotiff", "png_map"]


def test_build_operation_plan_for_buffer_with_explicit_operation_uses_vector_export() -> None:
    parsed = ParsedTaskSpec(
        analysis_type="BUFFER",
        operation_params={
            "operations": ["vector.buffer"],
            "distance_m": 300,
            "source_path": "/tmp/roads.geojson",
        },
    )

    plan = build_operation_plan_from_registry(parsed, status="draft", version=1, missing_fields=[])

    assert [node.op_name for node in plan.nodes] == ["vector.buffer", "artifact.export"]
    assert plan.nodes[0].params["distance_m"] == 300
    assert plan.nodes[0].params["source_path"] == "/tmp/roads.geojson"
    assert plan.nodes[1].params["formats"] == ["geojson"]


def test_build_operation_plan_supports_custom_operation_sequence() -> None:
    parsed = ParsedTaskSpec(
        analysis_type="CLIP",
        operation_params={
            "operations": ["raster.reproject", "raster.clip"],
            "source_path": "/tmp/source.tif",
            "clip_path": "/tmp/aoi.geojson",
            "target_crs": "EPSG:3857",
        },
        preferred_output=["geotiff", "png_map"],
    )

    plan = build_operation_plan_from_registry(parsed, status="draft", version=1, missing_fields=[])

    assert [node.op_name for node in plan.nodes] == [
        "raster.reproject",
        "raster.clip",
        "artifact.export",
    ]
    reproject_node = plan.nodes[0]
    clip_node = plan.nodes[1]
    assert clip_node.inputs["raster"] == reproject_node.outputs["raster"]
    assert "step_1_raster_reproject" in clip_node.depends_on
    assert plan.nodes[-1].params["formats"] == ["geotiff", "png_map"]


def test_build_operation_plan_for_workflow_without_operations_keeps_empty_plan() -> None:
    parsed = ParsedTaskSpec(
        analysis_type="WORKFLOW",
        operation_params={},
    )

    plan = build_operation_plan_from_registry(parsed, status="draft", version=1, missing_fields=[])

    assert plan.nodes == []
    assert "operations" in plan.missing_fields


def test_build_operation_plan_without_explicit_operations_stays_in_clarification_mode() -> None:
    parsed = ParsedTaskSpec(
        analysis_type="NDVI",
        operation_params={"source_path": "/tmp/source.tif"},
    )

    plan = build_operation_plan_from_registry(parsed, status="draft", version=1, missing_fields=[])

    assert plan.nodes == []
    assert "operations" in plan.missing_fields
