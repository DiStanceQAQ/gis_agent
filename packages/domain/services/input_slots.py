from __future__ import annotations

from typing import Any


def classify_uploaded_inputs(files: list[Any]) -> dict[str, str]:
    slots: dict[str, str] = {}
    for file in files:
        if file.file_type == "raster_tiff" and "input_raster_file_id" not in slots:
            slots["input_raster_file_id"] = file.id
        if file.file_type in {"geojson", "shp_zip", "vector_gpkg"} and "input_vector_file_id" not in slots:
            slots["input_vector_file_id"] = file.id
    return slots
