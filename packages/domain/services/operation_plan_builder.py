from __future__ import annotations

from collections import defaultdict
from typing import Any

from packages.domain.services.operation_registry import OPERATION_SPECS, require_operation_spec
from packages.schemas.operation_plan import OperationNode, OperationPlan, OperationPlanStatus
from packages.schemas.task import ParsedTaskSpec

_RASTER_EXPORT_FORMATS = {"geotiff", "tif", "tiff", "png_map", "png"}
_VECTOR_EXPORT_FORMATS = {"geojson", "json", "gpkg", "shapefile", "shp"}
_TABLE_EXPORT_FORMATS = {"csv"}


def _default_operations_for_analysis(parsed: ParsedTaskSpec) -> list[str]:
    analysis_type = parsed.analysis_type
    params = parsed.operation_params
    if analysis_type == "NDVI":
        return ["raster.band_math"]
    if analysis_type == "NDWI":
        return ["raster.band_math"]
    if analysis_type == "BAND_MATH":
        return ["raster.band_math"]
    if analysis_type == "FILTER":
        return ["raster.resample"]
    if analysis_type == "SLOPE_ASPECT":
        product = str(params.get("product") or "slope_aspect").lower()
        if product == "aspect":
            return ["raster.terrain_aspect"]
        return ["raster.terrain_slope"]
    if analysis_type == "BUFFER":
        return ["raster.clip"]
    if analysis_type == "CLIP":
        return ["raster.clip"]
    return ["raster.clip"]


def _normalize_requested_operations(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    normalized: list[str] = []
    for item in raw_value:
        op_name = str(item).strip()
        if not op_name:
            continue
        if op_name not in OPERATION_SPECS:
            continue
        normalized.append(op_name)
    return list(dict.fromkeys(normalized))


def _pick_operation_sequence(parsed: ParsedTaskSpec) -> list[str]:
    requested = _normalize_requested_operations(parsed.operation_params.get("operations"))
    if requested:
        return requested
    return _default_operations_for_analysis(parsed)


def _extract_op_override_params(parsed: ParsedTaskSpec, op_name: str) -> dict[str, Any]:
    params = parsed.operation_params
    overrides: dict[str, Any] = {}

    operation_overrides = params.get("operation_overrides")
    if isinstance(operation_overrides, dict):
        scoped = operation_overrides.get(op_name)
        if isinstance(scoped, dict):
            overrides.update(scoped)

    if op_name == "raster.band_math":
        if parsed.analysis_type == "NDWI":
            overrides.setdefault("expression", "(green-nir)/(green+nir)")
        elif parsed.analysis_type == "NDVI":
            overrides.setdefault("expression", "(nir-red)/(nir+red)")
        if "expression" in params:
            overrides["expression"] = params["expression"]
        if "source_path" in params:
            overrides["source_path"] = params["source_path"]
        return overrides

    if op_name == "raster.clip":
        for key in ("source_path", "clip_path", "output_path", "clip_crs", "crop", "all_touched"):
            if key in params:
                overrides[key] = params[key]
        return overrides

    if op_name == "raster.reproject":
        for key in ("source_path", "target_crs"):
            if key in params:
                overrides[key] = params[key]
        return overrides

    if op_name == "raster.resample":
        for key in ("source_path", "resolution", "scale"):
            if key in params:
                overrides[key] = params[key]
        if parsed.analysis_type == "FILTER" and "resolution" not in overrides:
            window_size = params.get("window_size")
            if isinstance(window_size, int):
                overrides["resolution"] = max(1, window_size)
        return overrides

    if op_name == "vector.buffer":
        for key in ("source_path", "distance_m", "source_crs"):
            if key in params:
                overrides[key] = params[key]
        return overrides

    if op_name == "artifact.export":
        for key in ("source_path", "formats"):
            if key in params:
                overrides[key] = params[key]
        return overrides

    for key, value in params.items():
        if key in {"operations", "operation_overrides"}:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)):
            overrides[key] = value
    return overrides


def _output_base_type(output_type: str) -> str:
    if output_type.endswith("[]"):
        return output_type[:-2]
    return output_type


def _pick_refs_for_input_type(
    *,
    input_type: str,
    refs_by_type: dict[str, list[str]],
) -> list[str]:
    if input_type == "any":
        for candidate in ("raster", "vector", "table", "artifact"):
            if refs_by_type.get(candidate):
                return [refs_by_type[candidate][-1]]
        for refs in refs_by_type.values():
            if refs:
                return [refs[-1]]
        return []

    if input_type.endswith("[]"):
        base = input_type[:-2]
        return list(refs_by_type.get(base, []))
    return [refs_by_type[input_type][-1]] if refs_by_type.get(input_type) else []


def _normalize_export_formats(primary_type: str | None, preferred_output: list[str]) -> list[str]:
    normalized_preferred: list[str] = []
    for item in preferred_output:
        lowered = str(item).lower().strip()
        if lowered == "png":
            lowered = "png_map"
        if lowered in {"tif", "tiff"}:
            lowered = "geotiff"
        normalized_preferred.append(lowered)

    if primary_type == "vector":
        formats = [item for item in normalized_preferred if item in _VECTOR_EXPORT_FORMATS]
        return formats or ["geojson"]
    if primary_type == "table":
        formats = [item for item in normalized_preferred if item in _TABLE_EXPORT_FORMATS]
        return formats or ["csv"]
    formats = [item for item in normalized_preferred if item in _RASTER_EXPORT_FORMATS]
    if not formats:
        return ["geotiff", "png_map"]
    if "geotiff" not in formats:
        formats.insert(0, "geotiff")
    return formats


def _infer_primary_ref(
    refs_by_type: dict[str, list[str]],
) -> tuple[str | None, str | None]:
    for type_name in ("raster", "vector", "table", "artifact"):
        refs = refs_by_type.get(type_name, [])
        if refs:
            return refs[-1], type_name
    return None, None


def _build_operation_nodes(parsed: ParsedTaskSpec, op_sequence: list[str]) -> list[OperationNode]:
    nodes: list[OperationNode] = []
    refs_by_type: dict[str, list[str]] = defaultdict(list)
    ref_producer: dict[str, str] = {}

    for index, op_name in enumerate(op_sequence, start=1):
        spec = require_operation_spec(op_name)
        step_id = f"step_{index}_{op_name.replace('.', '_')}"

        inputs: dict[str, Any] = {}
        depends_on: list[str] = []
        for input_key, input_type in spec.input_types.items():
            matched_refs = _pick_refs_for_input_type(input_type=input_type, refs_by_type=refs_by_type)
            if not matched_refs:
                continue
            inputs[input_key] = matched_refs[-1]
            for ref in matched_refs:
                producer_step = ref_producer.get(ref)
                if producer_step and producer_step != step_id:
                    depends_on.append(producer_step)

        params = dict(spec.default_params)
        params.update(_extract_op_override_params(parsed, op_name))

        outputs: dict[str, str] = {}
        for output_key, output_type in spec.output_types.items():
            output_ref = f"{step_id}_{output_key}"
            outputs[output_key] = output_ref
            refs_by_type[_output_base_type(output_type)].append(output_ref)
            ref_producer[output_ref] = step_id

        if not depends_on and nodes:
            depends_on = [nodes[-1].step_id]
        depends_on = list(dict.fromkeys(depends_on))

        nodes.append(
            OperationNode(
                step_id=step_id,
                op_name=op_name,
                depends_on=depends_on,
                inputs=inputs,
                params=params,
                outputs=outputs,
                retry_policy={"max_retries": 1 if spec.retryable_errors else 0},
            )
        )

    if not nodes:
        return []

    if nodes[-1].op_name == "artifact.export":
        return nodes

    export_spec = require_operation_spec("artifact.export")
    step_id = f"step_{len(nodes) + 1}_artifact_export"
    primary_ref, primary_type = _infer_primary_ref(refs_by_type)
    export_inputs: dict[str, str] = {}
    export_depends_on: list[str] = []
    if primary_ref:
        export_inputs["primary"] = primary_ref
        producer_step = ref_producer.get(primary_ref)
        if producer_step:
            export_depends_on.append(producer_step)
    elif nodes:
        export_depends_on.append(nodes[-1].step_id)

    export_params = dict(export_spec.default_params)
    export_params.update(_extract_op_override_params(parsed, "artifact.export"))
    export_params["formats"] = _normalize_export_formats(primary_type, parsed.preferred_output)
    export_outputs = {"artifact": f"{step_id}_artifact"}

    nodes.append(
        OperationNode(
            step_id=step_id,
            op_name="artifact.export",
            depends_on=list(dict.fromkeys(export_depends_on)),
            inputs=export_inputs,
            params=export_params,
            outputs=export_outputs,
            retry_policy={"max_retries": 0},
        )
    )
    return nodes


def build_operation_plan_from_registry(
    parsed: ParsedTaskSpec,
    *,
    version: int = 1,
    status: OperationPlanStatus = "draft",
    missing_fields: list[str] | None = None,
) -> OperationPlan:
    sequence = _pick_operation_sequence(parsed)
    nodes = _build_operation_nodes(parsed, sequence)

    if not nodes:
        fallback_parsed = parsed.model_copy(
            update={"analysis_type": "CLIP", "operation_params": parsed.operation_params}
        )
        nodes = _build_operation_nodes(fallback_parsed, ["raster.clip"])

    return OperationPlan(
        version=version,
        status=status,
        missing_fields=list(missing_fields or []),
        nodes=nodes,
    )
