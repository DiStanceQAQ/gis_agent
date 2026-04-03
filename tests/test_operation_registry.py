from packages.domain.services.operation_registry import require_operation_spec


def test_registry_contains_clip_and_export_specs() -> None:
    clip = require_operation_spec("raster.clip")
    export = require_operation_spec("artifact.export")
    slope = require_operation_spec("raster.terrain_slope")
    hillshade = require_operation_spec("raster.hillshade")
    mosaic = require_operation_spec("raster.mosaic")
    reclassify = require_operation_spec("raster.reclassify")
    mask = require_operation_spec("raster.mask")
    rasterize = require_operation_spec("raster.rasterize")

    assert clip.op_name == "raster.clip"
    assert "raster" in clip.input_types
    assert slope.op_name == "raster.terrain_slope"
    assert hillshade.default_params["azimuth"] == 315.0
    assert "rasters" in mosaic.input_types
    assert "rules" in reclassify.default_params
    assert mask.default_params["invert"] is False
    assert rasterize.default_params["burn_value"] == 1.0
    assert export.op_name == "artifact.export"
