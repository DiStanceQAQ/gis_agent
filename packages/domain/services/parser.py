from __future__ import annotations

import re
from calendar import monthrange
from datetime import date

from packages.domain.services.aoi import parse_bbox_text
from packages.schemas.task import ParsedTaskSpec


SEASON_MONTHS = {
    "春季": (3, 5),
    "夏季": (6, 8),
    "秋季": (9, 11),
}


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
        return f"{year:04d}-12-01", f"{year + 1:04d}-02-28"
    months = SEASON_MONTHS.get(season)
    if months is None:
        return None
    start_month, end_month = months
    end_day = monthrange(year, end_month)[1]
    return f"{year:04d}-{start_month:02d}-01", f"{year:04d}-{end_month:02d}-{end_day:02d}"


def parse_task_message(message: str, has_upload: bool) -> ParsedTaskSpec:
    text = message.strip()
    analysis_type = "NDVI" if ("NDVI" in text.upper() or "植被" in text) else "NDVI"
    preferred_output = ["png_map", "methods_text"]

    if "GeoTIFF".lower() in text.lower() or "tif" in text.lower():
        preferred_output.append("geotiff")
    if "地图" in text or "图" in text:
        if "png_map" not in preferred_output:
            preferred_output.append("png_map")
    if "方法" in text or "说明" in text:
        if "methods_text" not in preferred_output:
            preferred_output.append("methods_text")

    user_priority = "balanced"
    if any(keyword in text for keyword in ["细节", "地块", "局部差异"]):
        user_priority = "spatial"
    if any(keyword in text for keyword in ["多期", "连续性", "跨多年"]):
        user_priority = "temporal"

    time_range = None
    range_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*(?:月)?\s*[到至-]\s*(\d{1,2})\s*月", text)
    if range_match:
        year = int(range_match.group(1))
        start_month = int(range_match.group(2))
        end_month = int(range_match.group(3))
        end_day = monthrange(year, end_month)[1]
        time_range = {
            "start": f"{year:04d}-{start_month:02d}-01",
            "end": f"{year:04d}-{end_month:02d}-{end_day:02d}",
        }
    if time_range is None:
        month_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
        if month_match:
            year = int(month_match.group(1))
            month = int(month_match.group(2))
            start, end = _month_range(year, month)
            time_range = {"start": start, "end": end}
    if time_range is None:
        season_match = re.search(r"(\d{4})\s*年\s*(春季|夏季|秋季|冬季)", text)
        if season_match:
            year = int(season_match.group(1))
            season = season_match.group(2)
            season_range = _season_range(year, season)
            if season_range:
                time_range = {"start": season_range[0], "end": season_range[1]}

    cleaned = text
    cleaned = re.sub(r"帮我(?:计算|算|看)?", "", cleaned)
    cleaned = re.sub(r"计算|分析|帮我", "", cleaned)
    cleaned = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*(?:月)?\s*[到至-]\s*\d{1,2}\s*月", "", cleaned)
    cleaned = re.sub(r"\d{4}\s*年\s*(?:春季|夏季|秋季|冬季)", "", cleaned)
    cleaned = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月", "", cleaned)
    cleaned = cleaned.replace("的", " ").replace("并导出", " ")
    cleaned = re.sub(r"NDVI|植被情况|植被长势", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"地图和|地图|GeoTIFF|方法说明|结果解释|输出|导出|一张图|图和",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    aoi_input = re.sub(r"\s+", " ", cleaned).strip(" ，,。")

    detected_bbox = parse_bbox_text(aoi_input) if aoi_input else None

    if has_upload and not aoi_input:
        aoi_input = "uploaded_aoi"

    aoi_source_type = _infer_aoi_source_type(
        aoi_input,
        has_upload=has_upload,
        detected_bbox=detected_bbox,
    )

    missing_fields: list[str] = []
    if not aoi_input:
        missing_fields.append("aoi")
    if time_range is None:
        missing_fields.append("time_range")

    need_confirmation = bool(missing_fields)

    return ParsedTaskSpec(
        aoi_input=aoi_input or None,
        aoi_source_type=aoi_source_type,
        time_range=time_range,
        analysis_type=analysis_type,
        preferred_output=list(dict.fromkeys(preferred_output)),
        user_priority=user_priority,
        need_confirmation=need_confirmation,
        missing_fields=missing_fields,
        created_from=date.today().isoformat(),
    )
