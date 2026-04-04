from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.domain.services.operation_registry import list_operation_specs
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


@pytest.mark.parametrize(
    "op_name",
    [
        "raster.mosaic",
        "raster.reclassify",
        "raster.mask",
        "raster.rasterize",
        "vector.clip",
        "vector.union",
        "vector.spatial_join",
        "vector.repair",
    ],
)
def test_operation_node_accepts_extended_registry_operations(op_name: str) -> None:
    node = OperationNode(
        step_id=f"step-{op_name.replace('.', '-')}",
        op_name=op_name,
        depends_on=[],
        inputs={},
        params={},
        outputs={},
        retry_policy={"max_retries": 0},
    )
    assert node.op_name == op_name


def test_operation_schema_operation_names_align_with_registry() -> None:
    schema_names = set(OperationNode.model_fields["op_name"].annotation.__args__)
    registry_names = {spec.op_name for spec in list_operation_specs()}
    assert schema_names == registry_names
