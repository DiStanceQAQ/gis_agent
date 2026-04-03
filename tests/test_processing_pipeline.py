from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from packages.domain.services.processing_pipeline import run_processing_pipeline


def _create_test_raster(path: Path) -> None:
    data = np.arange(64, dtype="float32").reshape((8, 8))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=8,
        width=8,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(116.0, 40.0, 0.01, 0.01),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(data, 1)


def test_run_processing_pipeline_executes_clip_then_export(tmp_path) -> None:
    plan_nodes = [
        {
            "step_id": "clip1",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"crop": True},
            "outputs": {"raster": "r1"},
        },
        {
            "step_id": "export1",
            "op_name": "artifact.export",
            "depends_on": ["clip1"],
            "inputs": {"primary": "r1"},
            "params": {"formats": ["geotiff"]},
            "outputs": {"artifact": "a1"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_x", plan_nodes=plan_nodes, working_dir=tmp_path)
    assert "artifacts" in outputs


def test_run_processing_pipeline_empty_plan_still_returns_valid_raster(tmp_path) -> None:
    outputs = run_processing_pipeline(task_id="task_empty", plan_nodes=[], working_dir=tmp_path)

    tif_path = Path(outputs["tif_path"])
    assert tif_path.exists()
    with rasterio.open(tif_path) as dataset:
        assert dataset.width > 0
        assert dataset.height > 0
    assert outputs["valid_pixel_ratio"] is not None


def test_run_processing_pipeline_supports_phase1_raster_ops(tmp_path) -> None:
    source_raster = tmp_path / "source.tif"
    _create_test_raster(source_raster)

    plan_nodes = [
        {
            "step_id": "clip1",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_raster), "crop": True},
            "outputs": {"raster": "r_clip"},
        },
        {
            "step_id": "reproject1",
            "op_name": "raster.reproject",
            "depends_on": ["clip1"],
            "inputs": {"raster": "r_clip"},
            "params": {"target_crs": "EPSG:3857"},
            "outputs": {"raster": "r_reproject"},
        },
        {
            "step_id": "resample1",
            "op_name": "raster.resample",
            "depends_on": ["reproject1"],
            "inputs": {"raster": "r_reproject"},
            "params": {"scale": 0.5},
            "outputs": {"raster": "r_resample"},
        },
        {
            "step_id": "band1",
            "op_name": "raster.band_math",
            "depends_on": ["resample1"],
            "inputs": {"raster": "r_resample"},
            "params": {"expression": "b1 * 2"},
            "outputs": {"raster": "r_math"},
        },
        {
            "step_id": "zonal1",
            "op_name": "raster.zonal_stats",
            "depends_on": ["band1"],
            "inputs": {"raster": "r_math"},
            "params": {"stats": ["mean", "min", "max"]},
            "outputs": {"table": "t_stats"},
        },
        {
            "step_id": "export_csv",
            "op_name": "artifact.export",
            "depends_on": ["zonal1"],
            "inputs": {"primary": "t_stats"},
            "params": {"formats": ["csv"]},
            "outputs": {"artifact": "a_csv"},
        },
        {
            "step_id": "export_map",
            "op_name": "artifact.export",
            "depends_on": ["band1"],
            "inputs": {"primary": "r_math"},
            "params": {"formats": ["geotiff", "png_map"]},
            "outputs": {"artifact": "a_map"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_ops", plan_nodes=plan_nodes, working_dir=tmp_path)

    artifact_types = {item["artifact_type"] for item in outputs["artifacts"]}
    assert {"geotiff", "png_map", "csv"} <= artifact_types
    assert outputs["output_crs"] == "EPSG:3857"
    assert outputs["output_width"] is not None and outputs["output_width"] > 0
    assert outputs["output_height"] is not None and outputs["output_height"] > 0
    assert outputs["valid_pixel_ratio"] is not None and outputs["valid_pixel_ratio"] > 0
