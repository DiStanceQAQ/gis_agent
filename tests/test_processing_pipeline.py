from pathlib import Path

import json
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from packages.domain.services.processing_pipeline import (
    preflight_processing_pipeline_inputs,
    _resolve_raster_source,
    _resolve_vector_source,
    run_processing_pipeline,
)


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


def _write_test_geojson(path: Path, polygons: list[list[list[float]]]) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [polygon]},
            }
            for polygon in polygons
        ],
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_run_processing_pipeline_executes_clip_then_export(tmp_path) -> None:
    source_raster = tmp_path / "clip_source.tif"
    clip_vector = tmp_path / "clip_aoi.geojson"
    _create_test_raster(source_raster)
    _write_test_geojson(
        clip_vector,
        polygons=[
            [[116.0, 39.96], [116.03, 39.96], [116.03, 40.0], [116.0, 40.0], [116.0, 39.96]],
        ],
    )

    plan_nodes = [
        {
            "step_id": "clip1",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"crop": True, "source_path": str(source_raster), "clip_path": str(clip_vector)},
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


def test_run_processing_pipeline_empty_plan_fails_fast(tmp_path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        run_processing_pipeline(task_id="task_empty", plan_nodes=[], working_dir=tmp_path)


def test_run_processing_pipeline_supports_input_upload_nodes(tmp_path) -> None:
    source_raster = tmp_path / "upload_source.tif"
    clip_vector = tmp_path / "upload_clip.geojson"
    _create_test_raster(source_raster)
    _write_test_geojson(
        clip_vector,
        polygons=[
            [[116.0, 39.96], [116.03, 39.96], [116.03, 40.0], [116.0, 40.0], [116.0, 39.96]],
        ],
    )

    plan_nodes = [
        {
            "step_id": "upload_raster_1",
            "op_name": "input.upload_raster",
            "depends_on": [],
            "inputs": {"upload_id": "source_raster_upload_id"},
            "params": {"source_path": str(source_raster)},
            "outputs": {"raster": "r_uploaded"},
        },
        {
            "step_id": "upload_vector_1",
            "op_name": "input.upload_vector",
            "depends_on": [],
            "inputs": {"upload_id": "clip_vector_upload_id"},
            "params": {"source_path": str(clip_vector)},
            "outputs": {"vector": "v_uploaded"},
        },
        {
            "step_id": "clip_1",
            "op_name": "raster.clip",
            "depends_on": ["upload_raster_1", "upload_vector_1"],
            "inputs": {"raster": "r_uploaded", "aoi": "v_uploaded"},
            "params": {"crop": True},
            "outputs": {"raster": "r_clipped"},
        },
        {
            "step_id": "export_1",
            "op_name": "artifact.export",
            "depends_on": ["clip_1"],
            "inputs": {"primary": "r_clipped"},
            "params": {"formats": ["geotiff"]},
            "outputs": {"artifact": "a_out"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_upload_ops", plan_nodes=plan_nodes, working_dir=tmp_path)

    geotiff_artifacts = [item for item in outputs["artifacts"] if item["artifact_type"] == "geotiff"]
    assert geotiff_artifacts
    assert Path(geotiff_artifacts[0]["path"]).exists()


def test_artifact_export_geotiff_honors_output_path(tmp_path) -> None:
    source_raster = tmp_path / "source_export.tif"
    desired_output = tmp_path / "custom" / "xinjiang.tif"
    _create_test_raster(source_raster)

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
            "step_id": "export1",
            "op_name": "artifact.export",
            "depends_on": ["clip1"],
            "inputs": {"primary": "r_clip"},
            "params": {"formats": ["geotiff"], "output_path": str(desired_output)},
            "outputs": {"artifact": "a_out"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_export_path", plan_nodes=plan_nodes, working_dir=tmp_path)

    geotiff_artifacts = [item for item in outputs["artifacts"] if item["artifact_type"] == "geotiff"]
    assert geotiff_artifacts
    assert geotiff_artifacts[0]["path"] == str(desired_output)
    assert desired_output.exists()


def test_resolve_vector_source_accepts_clip_path_param(tmp_path) -> None:
    clip_vector = tmp_path / "clip_param.geojson"
    _write_test_geojson(
        clip_vector,
        polygons=[
            [[116.0, 39.96], [116.03, 39.96], [116.03, 40.0], [116.0, 40.0], [116.0, 39.96]],
        ],
    )
    node = {
        "step_id": "upload_vector_1",
        "inputs": {"upload_id": "uploaded_file"},
        "params": {"clip_path": str(clip_vector)},
    }

    resolved = _resolve_vector_source(node=node, references={}, working_dir=tmp_path)

    assert resolved == clip_vector


def test_resolve_raster_source_raises_when_no_usable_input(tmp_path) -> None:
    node = {
        "step_id": "upload_raster_1",
        "inputs": {"upload_id": "missing_upload"},
        "params": {},
    }

    with pytest.raises(ValueError, match="cannot resolve raster source"):
        _resolve_raster_source(node=node, references={}, working_dir=tmp_path)


def test_resolve_vector_source_raises_when_no_usable_input(tmp_path) -> None:
    node = {
        "step_id": "upload_vector_1",
        "inputs": {"upload_id": "missing_upload"},
        "params": {},
    }

    with pytest.raises(ValueError, match="cannot resolve vector source"):
        _resolve_vector_source(node=node, references={}, working_dir=tmp_path)


def test_preflight_processing_pipeline_inputs_rejects_missing_upload_source(tmp_path) -> None:
    plan_nodes = [
        {
            "step_id": "upload_vector_1",
            "op_name": "input.upload_vector",
            "depends_on": [],
            "inputs": {"upload_id": "missing_upload"},
            "params": {"source_path": str(tmp_path / "missing.geojson")},
            "outputs": {"vector": "v_uploaded"},
        },
    ]

    with pytest.raises(ValueError, match="cannot resolve vector source"):
        preflight_processing_pipeline_inputs(
            task_id="task_preflight_missing_vector",
            plan_nodes=plan_nodes,
            working_dir=tmp_path,
        )


def test_run_processing_pipeline_clip_with_invalid_explicit_path_fails(tmp_path) -> None:
    source_raster = tmp_path / "source_for_invalid_clip.tif"
    _create_test_raster(source_raster)

    plan_nodes = [
        {
            "step_id": "clip1",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {
                "source_path": str(source_raster),
                "clip_path": str(tmp_path / "missing_clip.geojson"),
            },
            "outputs": {"raster": "r_clip"},
        }
    ]

    with pytest.raises(ValueError, match="cannot resolve clip geometry"):
        run_processing_pipeline(task_id="task_invalid_clip_path", plan_nodes=plan_nodes, working_dir=tmp_path)


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


def test_run_processing_pipeline_supports_phase1_vector_ops(tmp_path) -> None:
    source_raster = tmp_path / "source_vector.tif"
    _create_test_raster(source_raster)

    source_vector_a = tmp_path / "source_a.geojson"
    source_vector_b = tmp_path / "source_b.geojson"
    _write_test_geojson(
        source_vector_a,
        [[[116.0, 39.95], [116.02, 39.95], [116.02, 39.99], [116.0, 39.99], [116.0, 39.95]]],
    )
    _write_test_geojson(
        source_vector_b,
        [[[116.01, 39.96], [116.03, 39.96], [116.03, 40.0], [116.01, 40.0], [116.01, 39.96]]],
    )

    plan_nodes = [
        {
            "step_id": "rclip",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_raster)},
            "outputs": {"raster": "r_clip"},
        },
        {
            "step_id": "vbuffer",
            "op_name": "vector.buffer",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_vector_a), "distance_m": 100},
            "outputs": {"vector": "v_buffer"},
        },
        {
            "step_id": "vclip",
            "op_name": "vector.clip",
            "depends_on": ["vbuffer"],
            "inputs": {"vector": "v_buffer"},
            "params": {"clip_bbox": [116.005, 39.955, 116.025, 39.995]},
            "outputs": {"vector": "v_clip"},
        },
        {
            "step_id": "vintersection",
            "op_name": "vector.intersection",
            "depends_on": ["vclip"],
            "inputs": {"vector": "v_clip", "overlay": str(source_vector_b)},
            "params": {},
            "outputs": {"vector": "v_inter"},
        },
        {
            "step_id": "vdissolve",
            "op_name": "vector.dissolve",
            "depends_on": ["vintersection"],
            "inputs": {"vector": "v_inter"},
            "params": {},
            "outputs": {"vector": "v_diss"},
        },
        {
            "step_id": "vreproject",
            "op_name": "vector.reproject",
            "depends_on": ["vdissolve"],
            "inputs": {"vector": "v_diss"},
            "params": {"target_crs": "EPSG:3857"},
            "outputs": {"vector": "v_proj"},
        },
        {
            "step_id": "rmask",
            "op_name": "raster.mask",
            "depends_on": ["rclip", "vdissolve"],
            "inputs": {"raster": "r_clip", "vector": "v_diss"},
            "params": {"nodata": -9999},
            "outputs": {"raster": "r_mask"},
        },
        {
            "step_id": "vexport",
            "op_name": "artifact.export",
            "depends_on": ["vreproject"],
            "inputs": {"primary": "v_proj"},
            "params": {"formats": ["geojson"]},
            "outputs": {"artifact": "a_vector"},
        },
        {
            "step_id": "rexport",
            "op_name": "artifact.export",
            "depends_on": ["rmask"],
            "inputs": {"primary": "r_mask"},
            "params": {"formats": ["geotiff"]},
            "outputs": {"artifact": "a_raster"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_vector_phase1", plan_nodes=plan_nodes, working_dir=tmp_path)

    artifact_types = {item["artifact_type"] for item in outputs["artifacts"]}
    assert {"geojson", "geotiff"} <= artifact_types
    geojson_paths = [
        Path(item["path"]) for item in outputs["artifacts"] if item["artifact_type"] == "geojson"
    ]
    assert geojson_paths
    exported_vector = json.loads(geojson_paths[0].read_text(encoding="utf-8"))
    assert exported_vector["type"] == "FeatureCollection"
    assert len(exported_vector["features"]) >= 1


def test_run_processing_pipeline_supports_phase2_and_phase3_vector_ops(tmp_path) -> None:
    source_raster = tmp_path / "source_vector_23.tif"
    _create_test_raster(source_raster)

    source_vector_a = tmp_path / "source_phase2_a.geojson"
    source_vector_b = tmp_path / "source_phase2_b.geojson"
    _write_test_geojson(
        source_vector_a,
        [[[116.0, 39.95], [116.025, 39.95], [116.025, 40.0], [116.0, 40.0], [116.0, 39.95]]],
    )
    _write_test_geojson(
        source_vector_b,
        [[[116.015, 39.96], [116.035, 39.96], [116.035, 40.01], [116.015, 40.01], [116.015, 39.96]]],
    )
    invalid_polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [116.0, 39.95],
                [116.02, 39.99],
                [116.0, 39.99],
                [116.02, 39.95],
                [116.0, 39.95],
            ]
        ],
    }

    plan_nodes = [
        {
            "step_id": "union1",
            "op_name": "vector.union",
            "depends_on": [],
            "inputs": {"vector": str(source_vector_a), "overlay": str(source_vector_b)},
            "params": {},
            "outputs": {"vector": "v_union"},
        },
        {
            "step_id": "erase1",
            "op_name": "vector.erase",
            "depends_on": ["union1"],
            "inputs": {"vector": "v_union", "overlay": str(source_vector_b)},
            "params": {},
            "outputs": {"vector": "v_erase"},
        },
        {
            "step_id": "simplify1",
            "op_name": "vector.simplify",
            "depends_on": ["erase1"],
            "inputs": {"vector": "v_erase"},
            "params": {"tolerance": 0.0001},
            "outputs": {"vector": "v_simple"},
        },
        {
            "step_id": "spjoin1",
            "op_name": "vector.spatial_join",
            "depends_on": ["simplify1"],
            "inputs": {"vector": "v_simple", "overlay": str(source_vector_b)},
            "params": {"predicate": "intersects"},
            "outputs": {"vector": "v_join"},
        },
        {
            "step_id": "repair1",
            "op_name": "vector.repair",
            "depends_on": [],
            "inputs": {},
            "params": {"geometry": invalid_polygon},
            "outputs": {"vector": "v_repair"},
        },
        {
            "step_id": "rasterize1",
            "op_name": "raster.rasterize",
            "depends_on": ["repair1"],
            "inputs": {"vector": "v_repair", "raster": str(source_raster)},
            "params": {"burn_value": 7, "nodata": 0, "dtype": "float32"},
            "outputs": {"raster": "r_repair_mask"},
        },
        {
            "step_id": "export_vector",
            "op_name": "artifact.export",
            "depends_on": ["spjoin1"],
            "inputs": {"primary": "v_join"},
            "params": {"formats": ["geojson", "gpkg", "shapefile"]},
            "outputs": {"artifact": "a_vector"},
        },
        {
            "step_id": "export_raster",
            "op_name": "artifact.export",
            "depends_on": ["rasterize1"],
            "inputs": {"primary": "r_repair_mask"},
            "params": {"formats": ["geotiff"]},
            "outputs": {"artifact": "a_raster"},
        },
    ]

    outputs = run_processing_pipeline(
        task_id="task_vector_phase23",
        plan_nodes=plan_nodes,
        working_dir=tmp_path,
    )

    artifact_types = {item["artifact_type"] for item in outputs["artifacts"]}
    assert {"geojson", "gpkg", "shapefile", "geotiff"} <= artifact_types

    geojson_path = next(Path(item["path"]) for item in outputs["artifacts"] if item["artifact_type"] == "geojson")
    geojson_payload = json.loads(geojson_path.read_text(encoding="utf-8"))
    assert geojson_payload["type"] == "FeatureCollection"
    assert geojson_payload["features"]
    assert "join_count" in geojson_payload["features"][0]["properties"]

    gpkg_path = next(Path(item["path"]) for item in outputs["artifacts"] if item["artifact_type"] == "gpkg")
    assert gpkg_path.exists()
    assert gpkg_path.read_bytes()[:16] == b"SQLite format 3\x00"

    shp_path = next(Path(item["path"]) for item in outputs["artifacts"] if item["artifact_type"] == "shapefile")
    assert shp_path.exists()
    assert shp_path.with_suffix(".dbf").exists()
    assert shp_path.with_suffix(".shx").exists()


def test_vector_union_raises_on_crs_mismatch(tmp_path) -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [116.0, 39.95],
                [116.02, 39.95],
                [116.02, 39.99],
                [116.0, 39.99],
                [116.0, 39.95],
            ]
        ],
    }
    plan_nodes = [
        {
            "step_id": "union_mismatch",
            "op_name": "vector.union",
            "depends_on": [],
            "inputs": {},
            "params": {
                "geometry": geometry,
                "source_crs": "EPSG:4326",
                "overlay_geometry": geometry,
                "overlay_crs": "EPSG:3857",
            },
            "outputs": {"vector": "v_out"},
        }
    ]

    with pytest.raises(ValueError, match="CRS mismatch"):
        run_processing_pipeline(task_id="task_vector_crs", plan_nodes=plan_nodes, working_dir=tmp_path)


def test_vector_simplify_requires_positive_tolerance(tmp_path) -> None:
    source_vector = tmp_path / "source_simplify.geojson"
    _write_test_geojson(
        source_vector,
        [[[116.0, 39.95], [116.03, 39.95], [116.03, 40.0], [116.0, 40.0], [116.0, 39.95]]],
    )
    plan_nodes = [
        {
            "step_id": "simplify_invalid",
            "op_name": "vector.simplify",
            "depends_on": [],
            "inputs": {"vector": str(source_vector)},
            "params": {"tolerance": 0},
            "outputs": {"vector": "v_out"},
        }
    ]

    with pytest.raises(ValueError, match="positive tolerance"):
        run_processing_pipeline(task_id="task_vector_simplify", plan_nodes=plan_nodes, working_dir=tmp_path)


def test_raster_clip_applies_bbox_crop(tmp_path) -> None:
    source_raster = tmp_path / "clip_source.tif"
    _create_test_raster(source_raster)

    plan_nodes = [
        {
            "step_id": "clip_bbox",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {
                "source_path": str(source_raster),
                "bbox": [116.0, 39.96, 116.04, 40.0],
                "crop": True,
            },
            "outputs": {"raster": "r_clip"},
        }
    ]

    outputs = run_processing_pipeline(task_id="task_clip_bbox", plan_nodes=plan_nodes, working_dir=tmp_path)
    clipped_path = Path(outputs["tif_path"])

    with rasterio.open(clipped_path) as dataset:
        assert dataset.width < 8
        assert dataset.height < 8
        assert dataset.bounds.left >= 116.0
        assert dataset.bounds.right <= 116.05 + 1e-6


def test_raster_zonal_stats_outputs_per_zone_rows(tmp_path) -> None:
    source_raster = tmp_path / "zonal_source.tif"
    _create_test_raster(source_raster)

    zones_geojson = tmp_path / "zones.geojson"
    _write_test_geojson(
        zones_geojson,
        [
            [[116.0, 39.92], [116.04, 39.92], [116.04, 40.0], [116.0, 40.0], [116.0, 39.92]],
            [[116.04, 39.92], [116.08, 39.92], [116.08, 40.0], [116.04, 40.0], [116.04, 39.92]],
        ],
    )

    plan_nodes = [
        {
            "step_id": "zonal_clip",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_raster)},
            "outputs": {"raster": "zonal_raster"},
        },
        {
            "step_id": "zonal_by_zone",
            "op_name": "raster.zonal_stats",
            "depends_on": ["zonal_clip"],
            "inputs": {"raster": "zonal_raster", "zones": str(zones_geojson)},
            "params": {"stats": ["mean", "min", "max"]},
            "outputs": {"table": "z_stats"},
        }
    ]

    run_processing_pipeline(task_id="task_zonal_rows", plan_nodes=plan_nodes, working_dir=tmp_path)
    table_path = tmp_path / "zonal_by_zone.csv"
    assert table_path.exists()

    rows = table_path.read_text(encoding="utf-8").strip().splitlines()
    assert rows[0] == "zone_id,mean,min,max"
    assert len(rows) == 3

    zone_1_values = [float(value) for value in rows[1].split(",")[1:]]
    zone_2_values = [float(value) for value in rows[2].split(",")[1:]]
    assert zone_1_values[0] != zone_2_values[0]


def test_artifact_export_png_map_is_valid_png(tmp_path) -> None:
    source_raster = tmp_path / "png_source.tif"
    _create_test_raster(source_raster)

    plan_nodes = [
        {
            "step_id": "clip_png",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"source_path": str(source_raster)},
            "outputs": {"raster": "r_png"},
        },
        {
            "step_id": "export_png",
            "op_name": "artifact.export",
            "depends_on": ["clip_png"],
            "inputs": {"primary": "r_png"},
            "params": {"formats": ["png_map"]},
            "outputs": {"artifact": "a_png"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_png_preview", plan_nodes=plan_nodes, working_dir=tmp_path)
    png_path = next(Path(item["path"]) for item in outputs["artifacts"] if item["artifact_type"] == "png_map")
    assert png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_run_processing_pipeline_vector_only_plan_supports_non_raster_terminal(tmp_path) -> None:
    source_vector = tmp_path / "vector_only.geojson"
    _write_test_geojson(
        source_vector,
        [[[116.0, 39.95], [116.02, 39.95], [116.02, 39.99], [116.0, 39.99], [116.0, 39.95]]],
    )
    plan_nodes = [
        {
            "step_id": "vector_only_buffer",
            "op_name": "vector.buffer",
            "depends_on": [],
            "inputs": {"vector": str(source_vector)},
            "params": {"distance_m": 100.0},
            "outputs": {"vector": "v_buf"},
        },
        {
            "step_id": "vector_only_export",
            "op_name": "artifact.export",
            "depends_on": ["vector_only_buffer"],
            "inputs": {"primary": "v_buf"},
            "params": {"formats": ["geojson"]},
            "outputs": {"artifact": "a_vec"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_vector_only", plan_nodes=plan_nodes, working_dir=tmp_path)
    assert any(item["artifact_type"] == "geojson" for item in outputs["artifacts"])
    assert outputs["tif_path"] is None
    assert outputs["png_path"] is None
    assert outputs["output_crs"] is None


def test_artifact_export_geotiff_requires_real_raster_input(tmp_path) -> None:
    source_vector = tmp_path / "geotiff_from_vector.geojson"
    _write_test_geojson(
        source_vector,
        [[[116.0, 39.95], [116.02, 39.95], [116.02, 39.99], [116.0, 39.99], [116.0, 39.95]]],
    )
    plan_nodes = [
        {
            "step_id": "vector_source",
            "op_name": "vector.buffer",
            "depends_on": [],
            "inputs": {"vector": str(source_vector)},
            "params": {"distance_m": 100.0},
            "outputs": {"vector": "v_only"},
        },
        {
            "step_id": "export_missing_raster",
            "op_name": "artifact.export",
            "depends_on": ["vector_source"],
            "inputs": {"primary": "v_only"},
            "params": {"formats": ["geotiff"]},
            "outputs": {"artifact": "a_missing"},
        }
    ]

    with pytest.raises(ValueError, match="requires a valid raster source"):
        run_processing_pipeline(task_id="task_missing_raster", plan_nodes=plan_nodes, working_dir=tmp_path)
