from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.errors import ErrorCode
from packages.domain.logging import get_logger
from packages.domain.services.llm_client import LLMClient, LLMClientError
from packages.domain.services.aoi import parse_bbox_text
from packages.schemas.agent import LLMParsedSpec
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)


SEASON_MONTHS = {
    "春季": (3, 5),
    "夏季": (6, 8),
    "秋季": (9, 11),
}

DATE_RANGE_SEPARATOR = r"(?:到|至|~|～|-|—|–)"

ABSOLUTE_DATE_RANGE_PATTERNS = [
    re.compile(
        r"(?P<start_year>\d{4})\s*[-/.年]\s*(?P<start_month>\d{1,2})\s*[-/.月]\s*(?P<start_day>\d{1,2})\s*(?:日)?"
        rf"\s*{DATE_RANGE_SEPARATOR}\s*"
        r"(?P<end_year>\d{4})\s*[-/.年]\s*(?P<end_month>\d{1,2})\s*[-/.月]\s*(?P<end_day>\d{1,2})\s*(?:日)?"
    ),
    re.compile(
        r"(?P<start_year>\d{4})\s*年\s*(?P<start_month>\d{1,2})\s*月\s*(?P<start_day>\d{1,2})\s*日?"
        rf"\s*{DATE_RANGE_SEPARATOR}\s*"
        r"(?:(?P<end_year>\d{4})\s*年\s*)?(?P<end_month>\d{1,2})\s*月\s*(?P<end_day>\d{1,2})\s*日?"
    ),
]
MONTH_RANGE_PATTERN = re.compile(
    rf"(?P<year>\d{{4}})\s*年\s*(?P<start_month>\d{{1,2}})\s*(?:月)?\s*{DATE_RANGE_SEPARATOR}\s*(?P<end_month>\d{{1,2}})\s*月"
)
MONTH_PATTERN = re.compile(r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月")
SEASON_PATTERN = re.compile(r"(?P<year>\d{4})\s*年\s*(?P<season>春季|夏季|秋季|冬季)")

AOI_NOISE_PATTERN = re.compile(
    r"(?:请|帮我|帮忙|分析一下|分析|计算|查看|看看|看|统计|生成|输出|导出|一张图|结果|的|范围内|期间|算|给出|做|对比|比较|一下)",
    flags=re.IGNORECASE,
)
FOLLOWUP_NOISE_PATTERN = re.compile(
    r"(?:换成|改成|改为|改用|换到|切换到|还是用|继续用|重新导出|重新生成|重跑|再跑一次|再来一次|重新跑|重新|继续|一遍|一次)",
    flags=re.IGNORECASE,
)
AOI_LEADING_CONNECTOR_PATTERN = re.compile(r"^(?:在|于|针对|关于)\s+")
AOI_TRAILING_CONNECTOR_PATTERN = re.compile(r"\s+(?:在|于|针对|关于|区域|研究区)\s*$")
FOLLOWUP_HINT_PATTERN = re.compile(
    r"(?:换成|改成|改为|改用|重新导出|重跑|再跑一次|再来一次|继续用|还是用|切换到)",
    flags=re.IGNORECASE,
)
UPLOADED_AOI_HINT_PATTERN = re.compile(r"(?:上传|边界|文件|刚上传|上传的)", flags=re.IGNORECASE)
BBOX_LITERAL_PATTERN = re.compile(r"(bbox\s*\([^)]*\)|\[[^\]]+\])", flags=re.IGNORECASE)
CLIP_INTENT_PATTERN = re.compile(r"(?:裁剪|裁切|clip|mask|掩膜)", flags=re.IGNORECASE)
RASTER_HINT_PATTERN = re.compile(r"(?:栅格|影像|raster|tif|tiff)", flags=re.IGNORECASE)
RASTER_PATH_PATTERN = re.compile(r"((?:[A-Za-z]:)?/[^\s,，。；;]+?\.(?:tif|tiff|img|vrt|jp2))", flags=re.IGNORECASE)
VECTOR_PATH_PATTERN = re.compile(
    r"((?:[A-Za-z]:)?/[^\s,，。；;]+?\.(?:geojson|json|shp|gpkg|kml))",
    flags=re.IGNORECASE,
)

OUTPUT_HINTS = {
    "geotiff": ("geotiff", "tiff", "tif"),
    "png_map": ("地图", "图", "png"),
    "methods_text": ("方法", "说明"),
}
PRIORITY_HINTS = {
    "spatial": ("细节", "地块", "局部差异"),
    "temporal": ("多期", "连续性", "跨多年"),
}
DATASET_HINTS = {
    "sentinel2": ("sentinel-2", "sentinel 2", "sentinel2", "哨兵", "哨兵二号"),
    "landsat89": ("landsat 8/9", "landsat8/9", "landsat 8", "landsat 9", "landsat89", "landsat"),
}
FULLWIDTH_TRANSLATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "～": "~",
        "—": "-",
        "–": "-",
    }
)


@dataclass
class ParseState:
    original_text: str
    remaining_text: str
    has_upload: bool
    time_range: dict[str, str] | None = None
    requested_dataset: str | None = None
    preferred_output: list[str] = field(default_factory=list)
    user_priority: str = "balanced"
    aoi_input: str | None = None
    aoi_source_type: str | None = None
    analysis_type: str = "NDVI"
    operation_params: dict[str, Any] = field(default_factory=dict)


def _normalize_message(text: str) -> str:
    normalized = text.translate(FULLWIDTH_TRANSLATION)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _infer_aoi_source_type(aoi_input: str | None, *, has_upload: bool, detected_bbox: list[float] | None) -> str | None:
    if has_upload:
        return "file_upload"
    if detected_bbox:
        return "bbox"
    if not aoi_input:
        return None

    compact = re.sub(r"\s+", "", aoi_input)
    if re.search(r"(自治区|自治州|特别行政区|省|市|区|县|州|盟|旗)$", compact):
        return "admin_name"
    return "place_alias"


def _month_range(year: int, month: int) -> tuple[str, str]:
    end_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{end_day:02d}"


def _season_range(year: int, season: str) -> tuple[str, str] | None:
    if season == "冬季":
        winter_end_day = monthrange(year + 1, 2)[1]
        return f"{year:04d}-12-01", f"{year + 1:04d}-02-{winter_end_day:02d}"
    months = SEASON_MONTHS.get(season)
    if months is None:
        return None
    start_month, end_month = months
    end_day = monthrange(year, end_month)[1]
    return f"{year:04d}-{start_month:02d}-01", f"{year:04d}-{end_month:02d}-{end_day:02d}"


def _safe_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _extract_absolute_date_range(text: str) -> tuple[dict[str, str] | None, str]:
    for pattern in ABSOLUTE_DATE_RANGE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue

        start_year = int(match.group("start_year"))
        end_year = int(match.group("end_year") or start_year)
        start_month = int(match.group("start_month"))
        end_month = int(match.group("end_month"))
        start_day = int(match.group("start_day"))
        end_day = int(match.group("end_day"))

        start = _safe_date(start_year, start_month, start_day)
        end = _safe_date(end_year, end_month, end_day)
        if start is None or end is None:
            continue
        return {"start": start, "end": end}, pattern.sub("", text, count=1)

    return None, text


def _extract_time_range(text: str) -> tuple[dict[str, str] | None, str]:
    time_range, remaining = _extract_absolute_date_range(text)
    if time_range is not None:
        return time_range, remaining

    month_range_match = MONTH_RANGE_PATTERN.search(text)
    if month_range_match:
        year = int(month_range_match.group("year"))
        start_month = int(month_range_match.group("start_month"))
        end_month = int(month_range_match.group("end_month"))
        end_day = monthrange(year, end_month)[1]
        return {
            "start": f"{year:04d}-{start_month:02d}-01",
            "end": f"{year:04d}-{end_month:02d}-{end_day:02d}",
        }, MONTH_RANGE_PATTERN.sub("", text, count=1)

    month_match = MONTH_PATTERN.search(text)
    if month_match:
        year = int(month_match.group("year"))
        month = int(month_match.group("month"))
        start, end = _month_range(year, month)
        return {"start": start, "end": end}, MONTH_PATTERN.sub("", text, count=1)

    season_match = SEASON_PATTERN.search(text)
    if season_match:
        season_range = _season_range(int(season_match.group("year")), season_match.group("season"))
        if season_range:
            return {
                "start": season_range[0],
                "end": season_range[1],
            }, SEASON_PATTERN.sub("", text, count=1)

    return None, text


def _detect_requested_dataset(text: str) -> str | None:
    normalized = text.lower()
    for dataset_name, hints in DATASET_HINTS.items():
        if any(hint in normalized or hint in text for hint in hints):
            return dataset_name
    return None


def _detect_preferred_output(text: str, *, include_defaults: bool) -> list[str]:
    outputs = ["png_map", "methods_text"] if include_defaults else []
    normalized = text.lower()
    for output_name, hints in OUTPUT_HINTS.items():
        if any(hint in normalized or hint in text for hint in hints):
            outputs.append(output_name)
    return list(dict.fromkeys(outputs))


def _detect_user_priority(text: str) -> str:
    for priority, hints in PRIORITY_HINTS.items():
        if any(hint in text for hint in hints):
            return priority
    return "balanced"


def _clean_aoi_text(text: str) -> str:
    cleaned = AOI_NOISE_PATTERN.sub(" ", text)
    cleaned = FOLLOWUP_NOISE_PATTERN.sub(" ", cleaned)
    cleaned = cleaned.replace("并", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" ,.;:，。；：")
    cleaned = AOI_LEADING_CONNECTOR_PATTERN.sub("", cleaned)
    cleaned = AOI_TRAILING_CONNECTOR_PATTERN.sub("", cleaned)
    return cleaned.strip(" ,.;:，。；：")


def _remove_token_hints(text: str) -> str:
    cleaned = re.sub(r"NDVI|植被情况|植被长势", " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"地图和|地图|GeoTIFF|TIFF|PNG|方法说明|结果解释|导出 GeoTIFF|导出|图和|结果摘要|摘要",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    for hints in DATASET_HINTS.values():
        for hint in hints:
            cleaned = re.sub(re.escape(hint), " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_plausible_aoi_text(aoi_input: str | None) -> bool:
    if not aoi_input:
        return False
    compact = re.sub(r"\s+", "", aoi_input)
    if parse_bbox_text(aoi_input):
        return True
    return len(compact) >= 2 and bool(re.search(r"[A-Za-z\u4e00-\u9fff]", compact))


def _extract_explicit_bbox_text(text: str) -> str | None:
    match = BBOX_LITERAL_PATTERN.search(text)
    if match is not None:
        return match.group(1).replace(" ", "")
    return None


def _clean_path_candidate(path_text: str) -> str:
    return path_text.strip().strip("\"'`“”‘’，。；;")


def _extract_path_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        candidate = _clean_path_candidate(match.group(1))
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        matches.append(candidate)
    return matches


def _is_clip_request(message: str) -> bool:
    if CLIP_INTENT_PATTERN.search(message) and RASTER_HINT_PATTERN.search(message):
        return True
    raster_paths = _extract_path_matches(RASTER_PATH_PATTERN, message)
    vector_paths = _extract_path_matches(VECTOR_PATH_PATTERN, message)
    return bool(raster_paths and vector_paths)


def _extract_clip_operation_params(message: str, *, has_upload: bool) -> dict[str, Any]:
    del has_upload
    raster_paths = _extract_path_matches(RASTER_PATH_PATTERN, message)
    vector_paths = _extract_path_matches(VECTOR_PATH_PATTERN, message)

    params: dict[str, Any] = {"crop": True, "clip_crs": "EPSG:4326"}
    if raster_paths:
        params["source_path"] = raster_paths[0]
    if len(raster_paths) > 1:
        params["output_path"] = raster_paths[1]
    if vector_paths:
        params["clip_path"] = vector_paths[0]
    return params


def _collect_missing_fields(
    *,
    analysis_type: str,
    aoi_input: str | None,
    time_range: dict[str, str] | None,
    operation_params: dict[str, Any],
) -> list[str]:
    if analysis_type == "CLIP":
        missing: list[str] = []
        if not str(operation_params.get("source_path") or "").strip():
            missing.append("source_path")
        if not str(operation_params.get("clip_path") or "").strip():
            missing.append("clip_path")
        return missing

    missing_fields: list[str] = []
    if not aoi_input:
        missing_fields.append("aoi")
    if time_range is None:
        missing_fields.append("time_range")
    return missing_fields


def _extract_aoi(state: ParseState) -> None:
    cleaned = _remove_token_hints(state.remaining_text)
    aoi_candidate = _clean_aoi_text(cleaned)
    detected_bbox = parse_bbox_text(aoi_candidate) if aoi_candidate else None

    if state.has_upload and detected_bbox:
        state.aoi_input = _extract_explicit_bbox_text(aoi_candidate) or aoi_candidate
        state.aoi_source_type = "bbox"
        return

    if state.has_upload and (UPLOADED_AOI_HINT_PATTERN.search(state.original_text) or not aoi_candidate):
        state.aoi_input = "uploaded_aoi"
        state.aoi_source_type = "file_upload"
        return

    if _is_plausible_aoi_text(aoi_candidate):
        state.aoi_input = _extract_explicit_bbox_text(aoi_candidate) or aoi_candidate
        state.aoi_source_type = _infer_aoi_source_type(
            state.aoi_input,
            has_upload=False,
            detected_bbox=detected_bbox,
        )


def _build_parse_state(message: str, *, has_upload: bool, include_default_outputs: bool) -> ParseState:
    normalized = _normalize_message(message)
    time_range, remaining = _extract_time_range(normalized)
    state = ParseState(
        original_text=normalized,
        remaining_text=remaining,
        has_upload=has_upload,
        time_range=time_range,
        requested_dataset=_detect_requested_dataset(normalized),
        preferred_output=_detect_preferred_output(normalized, include_defaults=include_default_outputs),
        user_priority=_detect_user_priority(normalized),
    )
    if _is_clip_request(normalized):
        state.analysis_type = "CLIP"
        state.operation_params = _extract_clip_operation_params(normalized, has_upload=has_upload)
        if include_default_outputs and "geotiff" not in state.preferred_output:
            state.preferred_output.insert(0, "geotiff")
        state.requested_dataset = None
        state.time_range = None
        return state
    _extract_aoi(state)
    return state


def is_followup_request(message: str) -> bool:
    return FOLLOWUP_HINT_PATTERN.search(_normalize_message(message)) is not None


def _build_parsed_spec_from_state(state: ParseState) -> ParsedTaskSpec:
    missing_fields = _collect_missing_fields(
        analysis_type=state.analysis_type,
        aoi_input=state.aoi_input,
        time_range=state.time_range,
        operation_params=state.operation_params,
    )

    return ParsedTaskSpec(
        aoi_input=state.aoi_input,
        aoi_source_type=state.aoi_source_type,
        time_range=state.time_range,
        requested_dataset=state.requested_dataset,
        analysis_type=state.analysis_type,
        preferred_output=state.preferred_output,
        user_priority=state.user_priority,
        operation_params=state.operation_params,
        need_confirmation=bool(missing_fields),
        missing_fields=missing_fields,
        created_from=date.today().isoformat(),
    )


def _parse_task_message_legacy(message: str, has_upload: bool) -> ParsedTaskSpec:
    state = _build_parse_state(message, has_upload=has_upload, include_default_outputs=True)
    return _build_parsed_spec_from_state(state)


def parse_followup_override_message(message: str, has_upload: bool) -> dict[str, object]:
    state = _build_parse_state(message, has_upload=has_upload, include_default_outputs=False)
    override: dict[str, object] = {}

    if state.time_range is not None:
        override["time_range"] = state.time_range
    if state.requested_dataset is not None:
        override["requested_dataset"] = state.requested_dataset
    if state.preferred_output:
        override["preferred_output"] = state.preferred_output
    if state.user_priority != "balanced":
        override["user_priority"] = state.user_priority
    if state.aoi_input:
        override["aoi_input"] = state.aoi_input
        override["aoi_source_type"] = state.aoi_source_type

    return override


@lru_cache
def _load_parser_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "parser.md"
    return prompt_path.read_text(encoding="utf-8")


def _build_parser_user_prompt(message: str, has_upload: bool) -> str:
    payload = {
        "task": "parse_gis_request",
        "language": "zh-CN",
        "message": message,
        "has_upload": has_upload,
        "allowed_aoi_source_type": ["bbox", "file_upload", "admin_name", "place_alias"],
        "allowed_dataset": ["sentinel2", "landsat89"],
        "allowed_analysis_type": ["ndvi", "ndwi", "band_math", "filter", "slope_aspect", "buffer", "clip"],
        "allowed_output": ["png_map", "geotiff", "methods_text"],
        "required_time_range_format": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
        "required_fields_hint": {
            "clip": ["analysis_type", "operation_params.source_path", "operation_params.clip_path"],
            "other": ["aoi_input", "aoi_source_type", "time_range"],
        },
        "required_fields": [
            "aoi_input",
            "aoi_source_type",
            "time_range",
            "requested_dataset",
            "analysis_type",
            "preferred_output",
            "user_priority",
            "operation_params",
            "need_confirmation",
            "missing_fields",
            "clarification_message",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_parser_repair_user_prompt(
    *,
    base_prompt: str,
    previous_json: dict[str, Any],
    validation_errors: list[dict[str, Any]],
) -> str:
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        return str(value)

    repair_payload = {
        "task": "repair_invalid_json_output",
        "instructions": "上一次 JSON 未通过后端 schema 校验。请仅返回修复后的 JSON，不要输出解释。",
        "original_prompt": base_prompt,
        "previous_json": previous_json,
        "validation_errors": _json_safe(validation_errors),
    }
    return json.dumps(repair_payload, ensure_ascii=False, indent=2)


def _build_parser_failure_spec(message: str, *, has_upload: bool, reason: str) -> ParsedTaskSpec:
    normalized = _normalize_message(message)
    if _is_clip_request(normalized):
        operation_params = _extract_clip_operation_params(normalized, has_upload=has_upload)
        missing_fields = _collect_missing_fields(
            analysis_type="CLIP",
            aoi_input=None,
            time_range=None,
            operation_params=operation_params,
        )
        return ParsedTaskSpec(
            aoi_input=None,
            aoi_source_type=None,
            time_range=None,
            requested_dataset=None,
            analysis_type="CLIP",
            preferred_output=["geotiff"],
            user_priority="balanced",
            operation_params=operation_params,
            need_confirmation=bool(missing_fields),
            missing_fields=missing_fields,
            clarification_message="任务解析失败，请补充源栅格与裁剪范围路径后重试。",
            created_from=f"llm_parse_failed:{reason[:120]}",
        )

    missing_fields = ["time_range"]
    if not has_upload:
        missing_fields.insert(0, "aoi")
    return ParsedTaskSpec(
        aoi_input="uploaded_aoi" if has_upload else None,
        aoi_source_type="file_upload" if has_upload else None,
        time_range=None,
        requested_dataset=None,
        analysis_type="NDVI",
        preferred_output=["png_map", "methods_text"],
        user_priority="balanced",
        need_confirmation=True,
        missing_fields=missing_fields,
        clarification_message=(
            "任务解析失败，请补充 AOI 与时间范围后重试。"
            if not has_upload
            else "任务解析失败，请补充时间范围后重试。"
        ),
        created_from=f"llm_parse_failed:{reason[:120]}",
    )


def _coerce_llm_parser_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        return {}

    payload = dict(raw_payload)
    preferred_output = payload.get("preferred_output")
    if isinstance(preferred_output, str):
        payload["preferred_output"] = [preferred_output]
    elif preferred_output is None:
        payload["preferred_output"] = ["png_map", "methods_text"]

    user_priority = payload.get("user_priority")
    if user_priority is None:
        payload["user_priority"] = "balanced"

    missing_fields = payload.get("missing_fields")
    if isinstance(missing_fields, str):
        payload["missing_fields"] = [missing_fields]
    elif missing_fields is None:
        payload["missing_fields"] = []

    if payload.get("operation_params") is None:
        payload["operation_params"] = {}
    return payload


def _normalize_llm_parsed_spec(payload: LLMParsedSpec, *, has_upload: bool, message: str) -> ParsedTaskSpec:
    aoi_input = payload.aoi_input
    aoi_source_type = payload.aoi_source_type
    time_range = payload.time_range.model_dump() if payload.time_range is not None else None
    requested_dataset = payload.requested_dataset
    analysis_type = payload.analysis_type or "NDVI"
    operation_params = payload.operation_params or {}
    preferred_output = list(dict.fromkeys(payload.preferred_output or ["png_map", "methods_text"]))
    user_priority = payload.user_priority
    need_confirmation = bool(payload.need_confirmation)
    missing_fields = list(payload.missing_fields)

    if analysis_type == "CLIP":
        inferred_operation_params = _extract_clip_operation_params(_normalize_message(message), has_upload=has_upload)
        operation_params = {**inferred_operation_params, **operation_params}

    if has_upload and not aoi_input and analysis_type != "CLIP":
        aoi_input = "uploaded_aoi"
        aoi_source_type = "file_upload"

    if aoi_input and not aoi_source_type:
        detected_bbox = parse_bbox_text(aoi_input)
        aoi_source_type = _infer_aoi_source_type(
            aoi_input,
            has_upload=has_upload,
            detected_bbox=detected_bbox,
        )

    missing_fields.extend(
        _collect_missing_fields(
            analysis_type=analysis_type,
            aoi_input=aoi_input,
            time_range=time_range,
            operation_params=operation_params,
        )
    )
    missing_fields = list(dict.fromkeys(missing_fields))
    need_confirmation = need_confirmation or bool(missing_fields)

    return ParsedTaskSpec(
        aoi_input=aoi_input,
        aoi_source_type=aoi_source_type,
        time_range=time_range,
        requested_dataset=requested_dataset,
        analysis_type=analysis_type,
        preferred_output=preferred_output,
        user_priority=user_priority,
        operation_params=operation_params,
        need_confirmation=need_confirmation,
        missing_fields=missing_fields,
        clarification_message=payload.clarification_message,
        created_from=f"llm:{date.today().isoformat()}",
    )


def _parse_task_message_with_llm(
    message: str,
    *,
    has_upload: bool,
    task_id: str | None = None,
    db_session: Session | None = None,
) -> ParsedTaskSpec:
    settings = get_settings()
    parser_retries = max(0, settings.llm_parser_schema_retries)
    client = LLMClient(settings)
    system_prompt = _load_parser_system_prompt()
    base_user_prompt = _build_parser_user_prompt(message, has_upload)
    user_prompt = base_user_prompt
    attempt = 0
    last_error = "unknown"

    while attempt <= parser_retries:
        response = client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            phase="parse",
            task_id=task_id,
            db_session=db_session,
        )
        try:
            coerced_payload = _coerce_llm_parser_payload(response.content_json)
            payload = LLMParsedSpec.model_validate(coerced_payload)
            return _normalize_llm_parsed_spec(payload, has_upload=has_upload, message=message)
        except ValidationError as exc:
            last_error = "schema_validation_failed"
            if attempt >= parser_retries:
                break
            attempt += 1
            user_prompt = _build_parser_repair_user_prompt(
                base_prompt=base_user_prompt,
                previous_json=coerced_payload if "coerced_payload" in locals() else {},
                validation_errors=exc.errors(),
            )

    return _build_parser_failure_spec(message, has_upload=has_upload, reason=last_error)


def parse_task_message(
    message: str,
    has_upload: bool,
    *,
    task_id: str | None = None,
    db_session: Session | None = None,
) -> ParsedTaskSpec:
    settings = get_settings()
    if not settings.llm_parser_enabled:
        return _parse_task_message_legacy(message, has_upload)

    try:
        return _parse_task_message_with_llm(
            message,
            has_upload=has_upload,
            task_id=task_id,
            db_session=db_session,
        )
    except LLMClientError as exc:
        logger.warning("parser.llm_client_failed detail=%s", exc.detail)
        if settings.llm_parser_legacy_fallback:
            return _parse_task_message_legacy(message, has_upload)
        return _build_parser_failure_spec(message, has_upload=has_upload, reason="llm_client_error")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("parser.llm_unexpected_error")
        if settings.llm_parser_legacy_fallback:
            return _parse_task_message_legacy(message, has_upload)
        return _build_parser_failure_spec(message, has_upload=has_upload, reason=repr(exc))


def extract_parser_failure_error(parsed: ParsedTaskSpec) -> tuple[str | None, str | None]:
    created_from = parsed.created_from or ""
    if not created_from.startswith("llm_parse_failed:"):
        return None, None

    reason = created_from.split(":", 1)[1]
    if reason.startswith("schema_validation_failed"):
        return (
            ErrorCode.TASK_LLM_PARSER_SCHEMA_VALIDATION_FAILED,
            "Parser LLM 输出未通过 schema 校验，任务进入待澄清状态。",
        )
    return (
        ErrorCode.TASK_LLM_PARSER_FAILED,
        f"Parser LLM 处理失败（{reason[:120]}），任务进入待澄清状态。",
    )
