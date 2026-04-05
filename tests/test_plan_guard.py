import pytest

from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.plan_guard import validate_operation_plan
from packages.schemas.operation_plan import OperationNode, OperationPlan


def test_validate_operation_plan_rejects_cycle() -> None:
    plan = OperationPlan(
        version=1,
        status="draft",
        nodes=[
            OperationNode(step_id="a", op_name="raster.clip", depends_on=["b"]),
            OperationNode(step_id="b", op_name="artifact.export", depends_on=["a"]),
        ],
    )
    with pytest.raises(AppError) as exc:
        validate_operation_plan(plan)
    assert exc.value.error_code == ErrorCode.PLAN_DEPENDENCY_CYCLE


def test_validate_operation_plan_rejects_empty_nodes() -> None:
    plan = OperationPlan(
        version=1,
        status="draft",
        nodes=[],
    )
    with pytest.raises(AppError) as exc:
        validate_operation_plan(plan)
    assert exc.value.error_code == ErrorCode.PLAN_SCHEMA_INVALID


def test_validate_operation_plan_rejects_input_type_mismatch() -> None:
    plan = OperationPlan(
        version=1,
        status="draft",
        nodes=[
            OperationNode(
                step_id="vector_1",
                op_name="vector.buffer",
                depends_on=[],
                inputs={},
                params={"source_path": "/tmp/source.geojson", "distance_m": 100},
                outputs={"vector": "v_buffer"},
            ),
            OperationNode(
                step_id="clip_1",
                op_name="raster.clip",
                depends_on=["vector_1"],
                inputs={"raster": "v_buffer", "aoi": "v_buffer"},
                params={"crop": True},
                outputs={"raster": "r_clip"},
            ),
        ],
    )
    with pytest.raises(AppError) as exc:
        validate_operation_plan(plan)
    assert exc.value.error_code == ErrorCode.PLAN_SCHEMA_INVALID
    assert exc.value.detail is not None
    assert exc.value.detail.get("reason") == "input_type_mismatch"


def test_validate_operation_plan_rejects_unresolved_input_reference() -> None:
    plan = OperationPlan(
        version=1,
        status="draft",
        nodes=[
            OperationNode(
                step_id="clip_1",
                op_name="raster.clip",
                depends_on=[],
                inputs={},
                params={"source_path": "/tmp/source.tif", "clip_path": "/tmp/aoi.geojson"},
                outputs={"raster": "r_clip"},
            ),
            OperationNode(
                step_id="export_1",
                op_name="artifact.export",
                depends_on=["clip_1"],
                inputs={"primary": "missing_ref"},
                params={"formats": ["geotiff"]},
                outputs={"artifact": "a_out"},
            ),
        ],
    )
    with pytest.raises(AppError) as exc:
        validate_operation_plan(plan)
    assert exc.value.error_code == ErrorCode.PLAN_SCHEMA_INVALID
    assert exc.value.detail is not None
    assert exc.value.detail.get("reason") == "input_ref_unresolved"
