from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.schemas.operation_plan import OperationNode, OperationPlan


def test_operation_plan_requires_unique_step_ids() -> None:
    with pytest.raises(ValidationError):
        OperationPlan(
            version=1,
            status="draft",
            missing_fields=[],
            nodes=[
                OperationNode(
                    step_id="step-1",
                    op_name="input.upload_raster",
                    depends_on=[],
                    inputs={"source": "a.tif"},
                    params={},
                    outputs={"artifact_id": "artifact-1"},
                    retry_policy={"max_attempts": 1},
                ),
                OperationNode(
                    step_id="step-1",
                    op_name="raster.clip",
                    depends_on=["step-1"],
                    inputs={"source": "a.tif"},
                    params={"bbox": [0, 0, 1, 1]},
                    outputs={"artifact_id": "artifact-2"},
                    retry_policy={"max_attempts": 2},
                ),
            ],
        )


def test_operation_node_rejects_empty_step_id() -> None:
    with pytest.raises(ValidationError):
        OperationNode(
            step_id="",
            op_name="raster.clip",
            depends_on=[],
            inputs={"source": "a.tif"},
            params={"bbox": [0, 0, 1, 1]},
            outputs={"artifact_id": "artifact-clip"},
            retry_policy={"max_retries": 1},
        )


def test_operation_node_rejects_invalid_value_types() -> None:
    with pytest.raises(ValidationError):
        OperationNode(
            step_id="step-clip",
            op_name="raster.clip",
            depends_on=[],
            inputs={"source": 1},
            params={"bbox": [0, 0, 1, 1]},
            outputs={"artifact_id": 2},
            retry_policy={"max_retries": "oops"},
        )


def test_operation_node_accepts_registered_name_shape() -> None:
    node = OperationNode(
        step_id="step-clip",
        op_name="raster.clip",
        depends_on=[],
        inputs={"source": "a.tif"},
        params={"bbox": [0, 0, 1, 1]},
        outputs={"artifact_id": "artifact-clip"},
        retry_policy={"max_attempts": 2},
    )

    assert node.op_name == "raster.clip"
