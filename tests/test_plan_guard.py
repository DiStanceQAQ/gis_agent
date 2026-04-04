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
