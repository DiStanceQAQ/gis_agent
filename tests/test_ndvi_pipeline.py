from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from geoalchemy2.elements import WKTElement
from rasterio.transform import from_bounds

import packages.domain.services.ndvi_pipeline as ndvi_pipeline
from packages.domain.services.ndvi_pipeline import (
    TargetGrid,
    _build_aoi_mask,
    _estimate_output_shape,
    _landsat_clear_mask,
    _sentinel_clear_mask,
    run_real_ndvi_pipeline,
)
from packages.domain.models import AOIRecord
from packages.schemas.task import ParsedTaskSpec


TEST_BOUNDS = (116.1, 39.8, 116.5, 40.1)


def _write_test_raster(path: Path, data: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=str(data.dtype),
        crs="EPSG:4326",
        transform=from_bounds(*TEST_BOUNDS, width=data.shape[1], height=data.shape[0]),
    ) as dataset:
        dataset.write(data, 1)


def _make_test_aoi() -> AOIRecord:
    return AOIRecord(
        id="aoi_test",
        task_id="task_test",
        geom=WKTElement(
            "MULTIPOLYGON(((116.2 39.8,116.4 39.8,116.4 40.1,116.2 40.1,116.2 39.8)))",
            srid=4326,
        ),
        bbox_bounds_json=list(TEST_BOUNDS),
        is_valid=True,
    )


def _make_spec() -> ParsedTaskSpec:
    return ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
        preferred_output=["png_map", "geotiff", "methods_text"],
    )


def _artifact_path_factory(base_dir: Path):
    def _build_artifact_path(task_id: str, filename: str) -> str:
        task_dir = base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return str(task_dir / filename)

    return _build_artifact_path


def test_estimate_output_shape_respects_bounds() -> None:
    width, height = _estimate_output_shape(
        [116.1, 39.8, 116.5, 40.1],
        spatial_resolution=30,
        max_dimension=512,
    )

    assert 64 <= width <= 512
    assert 64 <= height <= 512
    assert width >= height


def test_landsat_clear_mask_filters_cloud_bits() -> None:
    qa = np.array([[0, 8], [16, 0]], dtype="uint16")
    red = np.array([[0.2, 0.2], [0.2, np.nan]], dtype="float32")
    nir = np.array([[0.3, 0.3], [0.3, 0.3]], dtype="float32")

    mask = _landsat_clear_mask(qa, red, nir)

    assert mask.tolist() == [[True, False], [False, False]]


def test_sentinel_clear_mask_filters_invalid_scl() -> None:
    scl = np.array([[4, 9], [3, 5]], dtype="uint8")
    red = np.array([[0.2, 0.2], [0.2, 0.2]], dtype="float32")
    nir = np.array([[0.3, 0.3], [0.3, 0.3]], dtype="float32")

    mask = _sentinel_clear_mask(scl, red, nir)

    assert mask.tolist() == [[True, False], [False, True]]


def test_build_aoi_mask_respects_polygon_shape() -> None:
    aoi = AOIRecord(
        id="aoi_test",
        task_id="task_test",
        geom=WKTElement(
            "MULTIPOLYGON(((116.2 39.8,116.4 39.8,116.4 40.1,116.2 40.1,116.2 39.8)))",
            srid=4326,
        ),
        bbox_bounds_json=[116.1, 39.8, 116.5, 40.1],
        is_valid=True,
    )
    grid = TargetGrid(
        width=4,
        height=3,
        bounds=(116.1, 39.8, 116.5, 40.1),
        crs="EPSG:4326",
        transform=from_bounds(116.1, 39.8, 116.5, 40.1, width=4, height=3),
    )

    mask = _build_aoi_mask(aoi, grid=grid)

    assert mask.shape == (3, 4)
    assert mask[:, 0].tolist() == [False, False, False]
    assert mask[:, 1].tolist() == [True, True, True]
    assert mask[:, 2].tolist() == [True, True, True]
    assert mask[:, 3].tolist() == [False, False, False]


def test_run_real_ndvi_pipeline_generates_outputs_from_local_landsat_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    red_scene_1 = np.full((4, 4), 14545, dtype="uint16")
    nir_scene_1 = np.full((4, 4), 21818, dtype="uint16")
    qa_scene_1 = np.zeros((4, 4), dtype="uint16")

    red_scene_2 = np.full((4, 4), 16364, dtype="uint16")
    nir_scene_2 = np.full((4, 4), 20000, dtype="uint16")
    qa_scene_2 = np.zeros((4, 4), dtype="uint16")
    qa_scene_2[1, 1] = 8

    _write_test_raster(tmp_path / "scene_1_red.tif", red_scene_1)
    _write_test_raster(tmp_path / "scene_1_nir.tif", nir_scene_1)
    _write_test_raster(tmp_path / "scene_1_qa.tif", qa_scene_1)
    _write_test_raster(tmp_path / "scene_2_red.tif", red_scene_2)
    _write_test_raster(tmp_path / "scene_2_nir.tif", nir_scene_2)
    _write_test_raster(tmp_path / "scene_2_qa.tif", qa_scene_2)

    items = [
        {
            "id": "landsat_scene_1",
            "properties": {"datetime": "2024-06-03T00:00:00Z"},
            "assets": {
                "red": {"href": str(tmp_path / "scene_1_red.tif")},
                "nir08": {"href": str(tmp_path / "scene_1_nir.tif")},
                "qa_pixel": {"href": str(tmp_path / "scene_1_qa.tif")},
            },
        },
        {
            "id": "landsat_scene_2",
            "properties": {"datetime": "2024-06-19T00:00:00Z"},
            "assets": {
                "red": {"href": str(tmp_path / "scene_2_red.tif")},
                "nir08": {"href": str(tmp_path / "scene_2_nir.tif")},
                "qa_pixel": {"href": str(tmp_path / "scene_2_qa.tif")},
            },
        },
    ]

    grid = TargetGrid(
        width=4,
        height=4,
        bounds=TEST_BOUNDS,
        crs="EPSG:4326",
        transform=from_bounds(*TEST_BOUNDS, width=4, height=4),
    )

    monkeypatch.setattr(
        ndvi_pipeline,
        "search_items_for_dataset",
        lambda spec, aoi, *, dataset_name, max_items=None: items,
    )
    monkeypatch.setattr(ndvi_pipeline, "_resolve_target_grid", lambda **kwargs: grid)
    monkeypatch.setattr(ndvi_pipeline, "_signed_asset_href", lambda href: href)
    monkeypatch.setattr(
        ndvi_pipeline,
        "build_artifact_path",
        _artifact_path_factory(tmp_path / "artifacts"),
    )

    result = run_real_ndvi_pipeline(
        task_id="task_real_ndvi",
        dataset_name="landsat89",
        spec=_make_spec(),
        aoi=_make_test_aoi(),
    )

    assert result.mode == "real"
    assert result.selected_item_ids == ["landsat_scene_1", "landsat_scene_2"]
    assert result.actual_time_range == {"start": "2024-06-03", "end": "2024-06-19"}
    assert result.output_width == 4
    assert result.output_height == 4
    assert result.output_crs == "EPSG:4326"
    assert result.valid_pixel_ratio == 0.5
    assert 0.24 <= result.ndvi_min <= 0.26
    assert 0.3 <= result.ndvi_max <= 0.35
    assert 0.23 <= result.ndvi_mean <= 0.27
    assert "真实 NDVI 合成已完成" in result.summary_text
    assert "实际参与合成的影像数量为 2 景" in result.methods_text
    assert Path(result.tif_path).exists()
    assert Path(result.png_path).exists()

    with rasterio.open(result.tif_path) as dataset:
        tif = dataset.read(1)
        assert dataset.width == 4
        assert dataset.height == 4
        assert str(dataset.crs) == "EPSG:4326"
        assert dataset.nodata == -9999.0
        assert np.all(tif[:, 0] == dataset.nodata)
        assert np.all(tif[:, 3] == dataset.nodata)
        assert np.all(np.isfinite(tif[:, 1:3]))
        assert 0.3 <= float(tif[1, 1]) <= 0.35
        assert 0.24 <= float(tif[1, 2]) <= 0.26
