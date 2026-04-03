from __future__ import annotations

from pathlib import Path
from typing import Any


def _pick_primary_raster_reference(references: dict[str, str]) -> str | None:
    for ref in references.values():
        if ref.lower().endswith(".tif") or ref.lower().endswith(".tiff"):
            return ref
    return next(iter(references.values()), None)


def _ensure_placeholder_artifacts(working_dir: Path, *, tif_path: str | None, png_path: str | None) -> tuple[str, str]:
    resolved_tif = Path(tif_path) if tif_path else working_dir / "processing_output.tif"
    resolved_png = Path(png_path) if png_path else working_dir / "processing_preview.png"

    if not resolved_tif.exists():
        resolved_tif.write_bytes(b"processing-output")
    if not resolved_png.exists():
        resolved_png.write_bytes(b"processing-preview")

    return str(resolved_tif), str(resolved_png)


def run_processing_pipeline(*, task_id: str, plan_nodes: list[dict[str, Any]], working_dir: Path) -> dict[str, Any]:
    del task_id
    working_dir.mkdir(parents=True, exist_ok=True)

    pending_nodes = [dict(node) for node in plan_nodes]
    completed_steps: set[str] = set()
    references: dict[str, str] = {}
    artifacts: list[dict[str, str]] = []
    exported_tif_path: str | None = None
    exported_png_path: str | None = None

    while pending_nodes:
        progressed = False
        for node in list(pending_nodes):
            step_id = str(node.get("step_id") or "")
            depends_on = [str(dep) for dep in node.get("depends_on") or []]
            if any(dep not in completed_steps for dep in depends_on):
                continue

            op_name = str(node.get("op_name") or "")
            outputs = node.get("outputs") or {}
            inputs = node.get("inputs") or {}
            params = node.get("params") or {}

            if op_name == "raster.clip":
                output_ref = str(outputs.get("raster") or step_id or "raster_clip")
                output_path = working_dir / f"{step_id or output_ref}.tif"
                output_path.write_bytes(b"clip-output")
                references[output_ref] = str(output_path)
            elif op_name == "artifact.export":
                source_ref = str(inputs.get("primary") or "")
                source_path = references.get(source_ref)
                if source_path is None:
                    source_path = _pick_primary_raster_reference(references)
                if source_path is None:
                    raise ValueError("artifact.export requires a resolvable primary input reference")

                export_formats = params.get("formats") or ["geotiff"]
                for fmt in export_formats:
                    fmt_name = str(fmt).lower()
                    if fmt_name in {"geotiff", "tif", "tiff"}:
                        artifacts.append({"artifact_type": "geotiff", "path": str(source_path)})
                        exported_tif_path = str(source_path)
                    elif fmt_name in {"png", "png_map"}:
                        png_path = working_dir / f"{step_id or 'export'}_preview.png"
                        png_path.write_bytes(b"png-output")
                        artifacts.append({"artifact_type": "png_map", "path": str(png_path)})
                        exported_png_path = str(png_path)
            else:
                raise ValueError(f"Unsupported operation in dispatcher: {op_name}")

            completed_steps.add(step_id)
            pending_nodes.remove(node)
            progressed = True

        if not progressed:
            unresolved = [str(node.get("step_id") or "") for node in pending_nodes]
            raise ValueError(f"Operation plan has unresolved dependencies for steps: {unresolved}")

    primary_tif = exported_tif_path or _pick_primary_raster_reference(references)
    tif_path, png_path = _ensure_placeholder_artifacts(
        working_dir,
        tif_path=primary_tif,
        png_path=exported_png_path,
    )

    return {
        "mode": "operation_plan",
        "artifacts": artifacts,
        "tif_path": tif_path,
        "png_path": png_path,
        "actual_time_range": None,
        "selected_item_ids": [],
        "valid_pixel_ratio": 1.0,
        "ndvi_min": None,
        "ndvi_max": None,
        "ndvi_mean": None,
        "output_width": None,
        "output_height": None,
        "output_crs": None,
    }
