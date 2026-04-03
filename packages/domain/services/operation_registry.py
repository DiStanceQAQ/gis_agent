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


OPERATION_SPECS: dict[str, OperationSpec] = {
    "input.upload_raster": OperationSpec(
        op_name="input.upload_raster",
        input_types={"upload_id": "uploaded_file"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_input_upload_raster",
    ),
    "input.upload_vector": OperationSpec(
        op_name="input.upload_vector",
        input_types={"upload_id": "uploaded_file"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_input_upload_vector",
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
        default_params={"expression": "(nir-red)/(nir+red)"},
        executor_name="_op_raster_band_math",
    ),
    "raster.zonal_stats": OperationSpec(
        op_name="raster.zonal_stats",
        input_types={"raster": "raster", "zones": "vector"},
        output_types={"table": "table"},
        default_params={"stats": ["mean", "min", "max"]},
        executor_name="_op_raster_zonal_stats",
    ),
    "vector.buffer": OperationSpec(
        op_name="vector.buffer",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={"distance_m": 100.0},
        executor_name="_op_vector_buffer",
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
