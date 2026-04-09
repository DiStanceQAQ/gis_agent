from __future__ import annotations

from typing import Any, Literal, TypeAlias, cast


AnalysisType: TypeAlias = Literal[
    "WORKFLOW",
    "NDVI",
    "NDWI",
    "BAND_MATH",
    "FILTER",
    "SLOPE_ASPECT",
    "BUFFER",
    "CLIP",
]

_ANALYSIS_TYPE_ALIASES: dict[str, AnalysisType] = {
    "workflow": "WORKFLOW",
    "generic": "WORKFLOW",
    "general": "WORKFLOW",
    "gis": "WORKFLOW",
    "ndvi": "NDVI",
    "ndwi": "NDWI",
    "band_math": "BAND_MATH",
    "band-math": "BAND_MATH",
    "bandmath": "BAND_MATH",
    "filter": "FILTER",
    "slope_aspect": "SLOPE_ASPECT",
    "slope-aspect": "SLOPE_ASPECT",
    "slopeaspect": "SLOPE_ASPECT",
    "buffer": "BUFFER",
    "clip": "CLIP",
    "raster_clip": "CLIP",
    "raster-clip": "CLIP",
    "raster.clip": "CLIP",
    "crop": "CLIP",
}

_TEMPORAL_ANALYSIS_TYPES: set[AnalysisType] = {"NDVI", "NDWI"}
_FILTER_METHODS = {"median", "mean", "gaussian"}
_SLOPE_PRODUCTS = {"slope", "aspect", "slope_aspect"}


def normalize_analysis_type(value: Any) -> AnalysisType:
    if value is None:
        return "WORKFLOW"
    text = str(value).strip()
    if not text:
        return "WORKFLOW"

    token = text.replace(" ", "_")
    alias_key = token.lower()
    if alias_key in _ANALYSIS_TYPE_ALIASES:
        return _ANALYSIS_TYPE_ALIASES[alias_key]

    upper_token = token.upper()
    if upper_token in {
        "WORKFLOW",
        "NDVI",
        "NDWI",
        "BAND_MATH",
        "FILTER",
        "SLOPE_ASPECT",
        "BUFFER",
        "CLIP",
    }:
        return cast(AnalysisType, upper_token)

    raise ValueError(
        "analysis_type must be one of workflow/ndvi/ndwi/band_math/filter/slope_aspect/buffer/clip"
    )


def analysis_type_requires_time_range(analysis_type: AnalysisType) -> bool:
    return analysis_type in _TEMPORAL_ANALYSIS_TYPES


def normalize_operation_params(analysis_type: AnalysisType, value: Any) -> dict[str, Any]:
    if value is None:
        params: dict[str, Any] = {}
    elif isinstance(value, dict):
        params = dict(value)
    else:
        raise ValueError("operation_params must be a JSON object")

    if analysis_type in {"NDVI", "NDWI"}:
        expected_index = analysis_type.lower()
        raw_index = params.get("index")
        if raw_index is None:
            params["index"] = expected_index
            return params

        index = str(raw_index).strip().lower().replace("-", "_")
        if index != expected_index:
            raise ValueError(f"{analysis_type} operation_params.index must be {expected_index!r}")
        params["index"] = expected_index
        return params

    if analysis_type == "BAND_MATH":
        expression = str(params.get("expression") or "").strip()
        if not expression:
            raise ValueError("BAND_MATH requires operation_params.expression")
        params["expression"] = expression
        return params

    if analysis_type == "FILTER":
        raw_method = params.get("method", params.get("filter_type", "median"))
        method = str(raw_method).strip().lower()
        if method not in _FILTER_METHODS:
            raise ValueError("FILTER operation_params.method must be one of median/mean/gaussian")

        raw_window_size = params.get("window_size", 3)
        try:
            window_size = int(raw_window_size)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "FILTER operation_params.window_size must be a positive odd integer"
            ) from exc
        if window_size <= 0 or window_size % 2 == 0:
            raise ValueError("FILTER operation_params.window_size must be a positive odd integer")

        params["method"] = method
        params["window_size"] = window_size
        params.pop("filter_type", None)
        return params

    if analysis_type == "SLOPE_ASPECT":
        raw_product = params.get("product", params.get("terrain_product", "slope_aspect"))
        product = str(raw_product).strip().lower().replace("-", "_")
        if product not in _SLOPE_PRODUCTS:
            raise ValueError(
                "SLOPE_ASPECT operation_params.product must be slope/aspect/slope_aspect"
            )
        params["product"] = product
        params.pop("terrain_product", None)
        return params

    if analysis_type == "BUFFER":
        raw_distance = params.get("distance_m", params.get("distance"))
        if raw_distance is None:
            raise ValueError("BUFFER requires operation_params.distance_m")
        try:
            distance_m = float(raw_distance)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "BUFFER operation_params.distance_m must be a positive number"
            ) from exc
        if distance_m <= 0:
            raise ValueError("BUFFER operation_params.distance_m must be a positive number")
        params["distance_m"] = distance_m
        params.pop("distance", None)
        return params

    if analysis_type == "CLIP":
        for key in ("source_path", "clip_path", "output_path"):
            value = params.get(key)
            if value is None:
                continue
            params[key] = str(value).strip()
        if "clip_crs" in params and params.get("clip_crs") is not None:
            params["clip_crs"] = str(params["clip_crs"]).strip() or "EPSG:4326"
        if "crop" in params and params.get("crop") is not None:
            params["crop"] = bool(params["crop"])
        return params

    if analysis_type == "WORKFLOW":
        raw_operations = params.get("operations")
        if raw_operations is not None:
            if isinstance(raw_operations, list):
                operations = [str(item).strip() for item in raw_operations if str(item).strip()]
                params["operations"] = list(dict.fromkeys(operations))
            elif isinstance(raw_operations, str):
                token = raw_operations.strip()
                params["operations"] = [token] if token else []
            else:
                raise ValueError(
                    "WORKFLOW operation_params.operations must be a list of operation names"
                )
        return params

    return params
