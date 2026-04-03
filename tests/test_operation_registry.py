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
    vector_clip = require_operation_spec("vector.clip")
    vector_intersection = require_operation_spec("vector.intersection")
    vector_dissolve = require_operation_spec("vector.dissolve")
    vector_reproject = require_operation_spec("vector.reproject")
    vector_union = require_operation_spec("vector.union")
    vector_erase = require_operation_spec("vector.erase")
    vector_simplify = require_operation_spec("vector.simplify")
    vector_spatial_join = require_operation_spec("vector.spatial_join")
    vector_repair = require_operation_spec("vector.repair")

    assert clip.op_name == "raster.clip"
    assert "raster" in clip.input_types
    assert slope.op_name == "raster.terrain_slope"
    assert hillshade.default_params["azimuth"] == 315.0
    assert "rasters" in mosaic.input_types
    assert "rules" in reclassify.default_params
    assert mask.default_params["invert"] is False
    assert rasterize.default_params["burn_value"] == 1.0
    assert "clip_vector" in vector_clip.input_types
    assert "overlay" in vector_intersection.input_types
    assert vector_dissolve.output_types["vector"] == "vector"
    assert vector_reproject.default_params["target_crs"] == "EPSG:4326"
    assert "overlay" in vector_union.input_types
    assert "overlay" in vector_erase.input_types
    assert vector_simplify.default_params["tolerance"] > 0
    assert vector_spatial_join.default_params["predicate"] == "intersects"
    assert vector_repair.op_name == "vector.repair"
    assert export.op_name == "artifact.export"
