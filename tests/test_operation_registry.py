from packages.domain.services.operation_registry import require_operation_spec


def test_registry_contains_clip_and_export_specs() -> None:
    clip = require_operation_spec("raster.clip")
    export = require_operation_spec("artifact.export")

    assert clip.op_name == "raster.clip"
    assert "raster" in clip.input_types
    assert export.op_name == "artifact.export"
