from __future__ import annotations

from collections import deque
from pathlib import Path
import re

from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.operation_registry import OPERATION_SPECS
from packages.schemas.operation_plan import OperationPlan


def _build_dependency_graph(plan: OperationPlan) -> dict[str, list[str]]:
    step_ids = {node.step_id for node in plan.nodes}
    graph: dict[str, list[str]] = {}

    for node in plan.nodes:
        if node.op_name not in OPERATION_SPECS:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                message=f"Unknown operation: {node.op_name}",
                detail={"step_id": node.step_id, "op_name": node.op_name},
            )

        unknown_deps = [dep for dep in node.depends_on if dep not in step_ids]
        if unknown_deps:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                message="Operation plan depends_on references unknown steps.",
                detail={"step_id": node.step_id, "unknown_depends_on": unknown_deps},
            )
        graph[node.step_id] = list(node.depends_on)
    return graph


def _detect_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def _walk(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False

        visiting.add(step_id)
        for dependency in graph.get(step_id, []):
            if _walk(dependency):
                return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    return any(_walk(step_id) for step_id in graph)


_EXTERNAL_PATH_HINT = re.compile(r"[/\\]|(\.(tif|tiff|json|geojson|shp|gpkg|csv|png)$)", re.IGNORECASE)
_RASTER_SUFFIXES = {".tif", ".tiff", ".img", ".vrt", ".jp2"}
_VECTOR_SUFFIXES = {".geojson", ".json", ".shp", ".gpkg", ".kml", ".wkt", ".zip"}
_TABLE_SUFFIXES = {".csv", ".tsv", ".parquet", ".xlsx", ".xls"}


def _topological_sort(graph: dict[str, list[str]]) -> list[str]:
    indegree: dict[str, int] = {step_id: 0 for step_id in graph}
    children: dict[str, list[str]] = {step_id: [] for step_id in graph}

    for step_id, dependencies in graph.items():
        indegree[step_id] = len(dependencies)
        for dep in dependencies:
            children.setdefault(dep, []).append(step_id)

    queue: deque[str] = deque([step_id for step_id, degree in indegree.items() if degree == 0])
    ordered: list[str] = []
    while queue:
        step_id = queue.popleft()
        ordered.append(step_id)
        for child in children.get(step_id, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return ordered


def _base_type(type_name: str) -> str:
    return type_name[:-2] if type_name.endswith("[]") else type_name


def _is_type_compatible(*, actual: str, expected: str) -> bool:
    if expected == "any":
        return True
    if expected.endswith("[]"):
        return _base_type(actual) == _base_type(expected)
    return _base_type(actual) == expected


def _looks_like_external_ref(value: str) -> bool:
    return bool(_EXTERNAL_PATH_HINT.search(value))


def _param_is_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_param_is_present(item) for item in value)
    if isinstance(value, dict):
        if "type" in value:
            return bool(str(value.get("type") or "").strip())
        return bool(value)
    return True


def _infer_ref_base_type(value: str) -> str | None:
    suffix = Path(value).suffix.lower()
    if suffix in _RASTER_SUFFIXES:
        return "raster"
    if suffix in _VECTOR_SUFFIXES:
        return "vector"
    if suffix in _TABLE_SUFFIXES:
        return "table"
    return None


def _value_matches_expected_input(*, value: object, expected_type: str) -> bool:
    if expected_type == "uploaded_file":
        return _param_is_present(value)

    base_expected = _base_type(expected_type)
    if base_expected == "any":
        return _param_is_present(value)

    if isinstance(value, list):
        if expected_type.endswith("[]"):
            return any(_value_matches_expected_input(value=item, expected_type=base_expected) for item in value)
        return any(_value_matches_expected_input(value=item, expected_type=expected_type) for item in value)

    if isinstance(value, dict):
        if base_expected == "vector":
            return bool(str(value.get("type") or "").strip())
        return _param_is_present(value)

    if not isinstance(value, str):
        return False

    candidate = value.strip()
    if not candidate:
        return False

    if base_expected == "vector" and candidate.lower().startswith("bbox("):
        return True

    if _looks_like_external_ref(candidate):
        inferred = _infer_ref_base_type(candidate)
        if inferred is None:
            return True
        return inferred == base_expected

    return False


def _candidate_param_keys_for_input(*, input_key: str, expected_type: str) -> set[str]:
    key = input_key.strip().lower()
    keys: set[str] = {f"{key}_path", f"{key}_paths"}

    if expected_type in {"raster", "raster[]"} or key in {"raster", "rasters", "template_raster"}:
        keys.update({"source_path", "source_paths", "raster_path", "raster_paths", "template_raster_path"})

    if expected_type in {"vector", "vector[]"} or key in {
        "vector",
        "vectors",
        "aoi",
        "clip_vector",
        "overlay",
        "mask",
        "right",
        "vector_b",
        "zones",
        "zone",
    }:
        keys.update(
            {
                "vector_path",
                "vector_paths",
                "aoi_path",
                "clip_path",
                "clip_vector_path",
                "clip_vector_paths",
                "overlay_path",
                "overlay_paths",
                "mask_path",
                "mask_paths",
                "zones_path",
                "zone_path",
                "right_path",
                "vector_b_path",
                "source_path",
                "source_paths",
                "geometry",
                "geometries",
                "bbox",
                "clip_geometry",
                "clip_geometries",
                "clip_bbox",
                "overlay_geometry",
                "overlay_geometries",
                "overlay_bbox",
                f"{key}_geometry",
                f"{key}_geometries",
                f"{key}_bbox",
            }
        )

    if expected_type in {"table", "table[]"} or key == "table":
        keys.update({"table_path", "table_paths"})

    if expected_type == "any" or key == "primary":
        keys.update({"primary_path", "primary_paths", "source_path", "source_paths"})

    if expected_type == "uploaded_file":
        keys.update({"source_path", "source_paths"})

    return keys


def _has_external_input_fallback(*, node, input_key: str, expected_type: str) -> bool:
    params = node.params or {}
    candidate_keys = _candidate_param_keys_for_input(input_key=input_key, expected_type=expected_type)
    for key in candidate_keys:
        if _value_matches_expected_input(value=params.get(key), expected_type=expected_type):
            return True
    if expected_type == "uploaded_file":
        return _value_matches_expected_input(value=(node.inputs or {}).get(input_key), expected_type=expected_type)
    return False


def _allows_implicit_demo_input(*, node, input_key: str, expected_type: str) -> bool:
    base_expected = _base_type(expected_type)
    if base_expected not in {"raster", "vector", "table", "any"}:
        return False
    if str(node.op_name).startswith("input.upload_"):
        return False
    if str(node.op_name) == "raster.clip" and input_key in {"aoi", "clip_vector", "vector"}:
        return False
    if list(node.depends_on or []):
        return False
    return True


def _validate_required_params(*, node, spec) -> None:
    params = node.params or {}
    missing_required = [key for key in spec.required_params if not _param_is_present(params.get(key))]
    if missing_required:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_SCHEMA_INVALID,
            message="Operation plan is missing required operation params.",
            detail={
                "reason": "param_missing",
                "step_id": node.step_id,
                "op_name": node.op_name,
                "missing_params": missing_required,
            },
        )

    for any_group in spec.required_param_any:
        if any(_param_is_present(params.get(key)) for key in any_group):
            continue
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_SCHEMA_INVALID,
            message="Operation plan params do not satisfy any-of requirement.",
            detail={
                "reason": "param_any_missing",
                "step_id": node.step_id,
                "op_name": node.op_name,
                "required_any": list(any_group),
            },
        )


def _validate_io_chain(plan: OperationPlan, *, graph: dict[str, list[str]]) -> None:
    node_by_id = {node.step_id: node for node in plan.nodes}
    ordered_steps = _topological_sort(graph)

    produced_refs: dict[str, str] = {}
    for step_id in ordered_steps:
        node = node_by_id[step_id]
        spec = OPERATION_SPECS[node.op_name]

        unknown_input_keys = [key for key in node.inputs.keys() if key not in spec.input_types]
        if unknown_input_keys:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                message="Operation plan contains unknown input keys.",
                detail={
                    "reason": "unknown_input_key",
                    "step_id": step_id,
                    "unknown_input_keys": unknown_input_keys,
                },
            )

        _validate_required_params(node=node, spec=spec)

        for input_key, expected_type in spec.input_types.items():
            raw_ref = node.inputs.get(input_key)
            ref = str(raw_ref).strip() if raw_ref is not None else ""
            if not ref:
                if _has_external_input_fallback(node=node, input_key=input_key, expected_type=expected_type):
                    continue
                if _allows_implicit_demo_input(node=node, input_key=input_key, expected_type=expected_type):
                    continue
                raise AppError.bad_request(
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message="Operation plan is missing required operation input.",
                    detail={
                        "reason": "input_missing",
                        "step_id": step_id,
                        "op_name": node.op_name,
                        "input_key": input_key,
                        "expected_type": expected_type,
                    },
                )

            actual_type = produced_refs.get(ref)
            if actual_type is None:
                if expected_type == "uploaded_file":
                    continue
                if _looks_like_external_ref(ref) and _value_matches_expected_input(value=ref, expected_type=expected_type):
                    continue
                raise AppError.bad_request(
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message="Operation plan references unresolved input.",
                    detail={
                        "reason": "input_ref_unresolved",
                        "step_id": step_id,
                        "op_name": node.op_name,
                        "input_key": input_key,
                        "input_ref": ref,
                    },
                )

            if not _is_type_compatible(actual=actual_type, expected=expected_type):
                raise AppError.bad_request(
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message="Operation plan input type does not match upstream output type.",
                    detail={
                        "reason": "input_type_mismatch",
                        "step_id": step_id,
                        "op_name": node.op_name,
                        "input_key": input_key,
                        "input_ref": ref,
                        "actual_type": actual_type,
                        "expected_type": expected_type,
                    },
                )

        for output_key, output_type in spec.output_types.items():
            output_ref = str((node.outputs or {}).get(output_key) or "").strip()
            if not output_ref:
                raise AppError.bad_request(
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message="Operation plan output reference is missing.",
                    detail={
                        "reason": "output_missing",
                        "step_id": step_id,
                        "op_name": node.op_name,
                        "output_key": output_key,
                    },
                )
            if output_ref in produced_refs:
                raise AppError.bad_request(
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message="Operation plan output reference must be unique.",
                    detail={
                        "reason": "output_ref_duplicated",
                        "step_id": step_id,
                        "output_key": output_key,
                        "output_ref": output_ref,
                    },
                )
            produced_refs[output_ref] = _base_type(output_type)


def validate_operation_plan(plan: OperationPlan) -> OperationPlan:
    if not plan.nodes:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_SCHEMA_INVALID,
            message="Operation plan must include at least one node.",
            detail={"reason": "empty_nodes"},
        )
    graph = _build_dependency_graph(plan)
    if _detect_cycle(graph):
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_DEPENDENCY_CYCLE,
            message="Operation plan contains dependency cycles.",
        )
    _validate_io_chain(plan, graph=graph)
    return plan.model_copy(update={"status": "validated"})
