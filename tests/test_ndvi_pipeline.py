from __future__ import annotations

import numpy as np
from geoalchemy2.elements import WKTElement
from rasterio.transform import from_bounds

from packages.domain.services.ndvi_pipeline import (
    TargetGrid,
    _build_aoi_mask,
    _estimate_output_shape,
    _landsat_clear_mask,
    _sentinel_clear_mask,
)
from packages.domain.models import AOIRecord


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
