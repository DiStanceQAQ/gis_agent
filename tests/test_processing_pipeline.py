from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from packages.domain.services.processing_pipeline import run_processing_pipeline


def _create_test_raster(path: Path, *, offset: float = 0.0, mask_first_pixel: bool = False) -> None:
    data = np.arange(64, dtype="float32").reshape((8, 8)) + float(offset)
    if mask_first_pixel:
        data[0, 0] = -9999.0
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


def test_run_processing_pipeline_supports_phase2_terrain_and_mosaic_ops(tmp_path) -> None:
    source_a = tmp_path / "source_a.tif"
    source_b = tmp_path / "source_b.tif"
    _create_test_raster(source_a, mask_first_pixel=True)
    _create_test_raster(source_b, offset=100.0)

    plan_nodes = [
        {
            "step_id": "clip_a",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_a)},
            "outputs": {"raster": "r_a"},
        },
        {
            "step_id": "clip_b",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_b)},
            "outputs": {"raster": "r_b"},
        },
        {
            "step_id": "mosaic1",
            "op_name": "raster.mosaic",
            "depends_on": ["clip_a", "clip_b"],
            "inputs": {"rasters": ["r_a", "r_b"]},
            "params": {},
            "outputs": {"raster": "r_mosaic"},
        },
        {
            "step_id": "slope1",
            "op_name": "raster.terrain_slope",
            "depends_on": ["mosaic1"],
            "inputs": {"raster": "r_mosaic"},
            "params": {},
            "outputs": {"raster": "r_slope"},
        },
        {
            "step_id": "aspect1",
            "op_name": "raster.terrain_aspect",
            "depends_on": ["mosaic1"],
            "inputs": {"raster": "r_mosaic"},
            "params": {},
            "outputs": {"raster": "r_aspect"},
        },
        {
            "step_id": "hillshade1",
            "op_name": "raster.hillshade",
            "depends_on": ["mosaic1"],
            "inputs": {"raster": "r_mosaic"},
            "params": {"azimuth": 315.0, "altitude": 45.0},
            "outputs": {"raster": "r_hillshade"},
        },
        {
            "step_id": "export_hillshade",
            "op_name": "artifact.export",
            "depends_on": ["hillshade1", "aspect1", "slope1"],
            "inputs": {"primary": "r_hillshade"},
            "params": {"formats": ["geotiff", "png_map"]},
            "outputs": {"artifact": "a_hillshade"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_phase2", plan_nodes=plan_nodes, working_dir=tmp_path)

    artifact_types = {item["artifact_type"] for item in outputs["artifacts"]}
    assert {"geotiff", "png_map"} <= artifact_types
    assert outputs["output_crs"] == "EPSG:4326"
    assert outputs["output_width"] is not None and outputs["output_width"] > 0
    assert outputs["output_height"] is not None and outputs["output_height"] > 0


def test_run_processing_pipeline_supports_phase3_reclassify_mask_rasterize(tmp_path) -> None:
    source_raster = tmp_path / "source_phase3.tif"
    _create_test_raster(source_raster)

    clip_bbox = [116.0, 39.97, 116.02, 40.0]
    rasterize_bbox = [116.01, 39.96, 116.03, 39.99]
    plan_nodes = [
        {
            "step_id": "clip1",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_raster)},
            "outputs": {"raster": "r_clip"},
        },
        {
            "step_id": "reclass1",
            "op_name": "raster.reclassify",
            "depends_on": ["clip1"],
            "inputs": {"raster": "r_clip"},
            "params": {
                "rules": [
                    {"min": 0, "max": 20, "value": 1},
                    {"min": 20, "max": 40, "value": 2},
                    {"min": 40, "max": 64, "value": 3},
                ],
                "default_value": 0,
                "dtype": "float32",
            },
            "outputs": {"raster": "r_reclass"},
        },
        {
            "step_id": "mask1",
            "op_name": "raster.mask",
            "depends_on": ["reclass1"],
            "inputs": {"raster": "r_reclass"},
            "params": {"bbox": clip_bbox, "invert": False, "nodata": -9999},
            "outputs": {"raster": "r_mask"},
        },
        {
            "step_id": "rasterize1",
            "op_name": "raster.rasterize",
            "depends_on": ["mask1"],
            "inputs": {"raster": "r_mask"},
            "params": {
                "bbox": rasterize_bbox,
                "burn_value": 9,
                "dtype": "float32",
                "nodata": 0,
            },
            "outputs": {"raster": "r_zone"},
        },
        {
            "step_id": "export1",
            "op_name": "artifact.export",
            "depends_on": ["rasterize1"],
            "inputs": {"primary": "r_zone"},
            "params": {"formats": ["geotiff", "png_map"]},
            "outputs": {"artifact": "a_phase3"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_phase3", plan_nodes=plan_nodes, working_dir=tmp_path)

    artifact_types = {item["artifact_type"] for item in outputs["artifacts"]}
    assert {"geotiff", "png_map"} <= artifact_types
    assert outputs["output_crs"] == "EPSG:4326"
    assert outputs["output_width"] is not None and outputs["output_width"] > 0
    assert outputs["output_height"] is not None and outputs["output_height"] > 0
    assert outputs["valid_pixel_ratio"] is not None and outputs["valid_pixel_ratio"] > 0
