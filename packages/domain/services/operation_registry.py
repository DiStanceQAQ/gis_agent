from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OperationSpec:
    op_name: str
    input_types: dict[str, str]
    output_types: dict[str, str]
    default_params: dict[str, Any]
    executor_name: str
    retryable_errors: tuple[str, ...] = ()
    required_params: tuple[str, ...] = ()
    required_param_any: tuple[tuple[str, ...], ...] = ()


OPERATION_SPECS: dict[str, OperationSpec] = {
    "input.upload_raster": OperationSpec(
        op_name="input.upload_raster",
        input_types={"upload_id": "uploaded_file"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_input_upload_raster",
        required_params=("source_path",),
    ),
    "input.upload_vector": OperationSpec(
        op_name="input.upload_vector",
        input_types={"upload_id": "uploaded_file"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_input_upload_vector",
        required_params=("source_path",),
    ),
    "raster.clip": OperationSpec(
        op_name="raster.clip",
        input_types={"raster": "raster", "aoi": "vector"},
        output_types={"raster": "raster"},
        default_params={"crop": True},
        executor_name="_op_raster_clip",
    ),
    "raster.reproject": OperationSpec(
        op_name="raster.reproject",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={"target_crs": "EPSG:4326"},
        executor_name="_op_raster_reproject",
    ),
    "raster.resample": OperationSpec(
        op_name="raster.resample",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={"resolution": 10},
        executor_name="_op_raster_resample",
    ),
    "raster.band_math": OperationSpec(
        op_name="raster.band_math",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_raster_band_math",
        required_params=("expression",),
    ),
    "raster.zonal_stats": OperationSpec(
        op_name="raster.zonal_stats",
        input_types={"raster": "raster", "zones": "vector"},
        output_types={"table": "table"},
        default_params={"stats": ["mean", "min", "max"]},
        executor_name="_op_raster_zonal_stats",
    ),
    "raster.terrain_slope": OperationSpec(
        op_name="raster.terrain_slope",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_raster_terrain_slope",
    ),
    "raster.terrain_aspect": OperationSpec(
        op_name="raster.terrain_aspect",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_raster_terrain_aspect",
    ),
    "raster.hillshade": OperationSpec(
        op_name="raster.hillshade",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={"azimuth": 315.0, "altitude": 45.0},
        executor_name="_op_raster_hillshade",
    ),
    "raster.mosaic": OperationSpec(
        op_name="raster.mosaic",
        input_types={"rasters": "raster[]"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_raster_mosaic",
    ),
    "raster.reclassify": OperationSpec(
        op_name="raster.reclassify",
        input_types={"raster": "raster"},
        output_types={"raster": "raster"},
        default_params={"rules": [{"min": 0.0, "max": 1.0, "value": 1.0}]},
        executor_name="_op_raster_reclassify",
    ),
    "raster.mask": OperationSpec(
        op_name="raster.mask",
        input_types={"raster": "raster", "vector": "vector"},
        output_types={"raster": "raster"},
        default_params={"invert": False},
        executor_name="_op_raster_mask",
    ),
    "raster.rasterize": OperationSpec(
        op_name="raster.rasterize",
        input_types={"vector": "vector", "raster": "raster"},
        output_types={"raster": "raster"},
        default_params={"burn_value": 1.0},
        executor_name="_op_raster_rasterize",
    ),
    "vector.buffer": OperationSpec(
        op_name="vector.buffer",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={"distance_m": 100.0},
        executor_name="_op_vector_buffer",
    ),
    "vector.clip": OperationSpec(
        op_name="vector.clip",
        input_types={"vector": "vector", "clip_vector": "vector"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_vector_clip",
    ),
    "vector.intersection": OperationSpec(
        op_name="vector.intersection",
        input_types={"vector": "vector", "overlay": "vector"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_vector_intersection",
    ),
    "vector.dissolve": OperationSpec(
        op_name="vector.dissolve",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_vector_dissolve",
    ),
    "vector.reproject": OperationSpec(
        op_name="vector.reproject",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={"target_crs": "EPSG:4326"},
        executor_name="_op_vector_reproject",
    ),
    "vector.union": OperationSpec(
        op_name="vector.union",
        input_types={"vector": "vector", "overlay": "vector"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_vector_union",
    ),
    "vector.erase": OperationSpec(
        op_name="vector.erase",
        input_types={"vector": "vector", "overlay": "vector"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_vector_erase",
    ),
    "vector.simplify": OperationSpec(
        op_name="vector.simplify",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={"tolerance": 0.0001},
        executor_name="_op_vector_simplify",
    ),
    "vector.spatial_join": OperationSpec(
        op_name="vector.spatial_join",
        input_types={"vector": "vector", "overlay": "vector"},
        output_types={"vector": "vector"},
        default_params={"predicate": "intersects"},
        executor_name="_op_vector_spatial_join",
    ),
    "vector.repair": OperationSpec(
        op_name="vector.repair",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_vector_repair",
    ),
    "artifact.export": OperationSpec(
        op_name="artifact.export",
        input_types={"primary": "any"},
        output_types={"artifact": "artifact"},
        default_params={"formats": ["geotiff", "png_map", "summary_md"]},
        executor_name="_op_artifact_export",
    ),
}


def require_operation_spec(op_name: str) -> OperationSpec:
    return OPERATION_SPECS[op_name]


def list_operation_specs() -> list[OperationSpec]:
    return list(OPERATION_SPECS.values())
