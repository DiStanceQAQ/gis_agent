from __future__ import annotations

from packages.schemas.operation_plan import OperationNode, OperationPlan, OperationPlanStatus


def build_default_operation_plan(
    *,
    version: int = 1,
    status: OperationPlanStatus = "draft",
    missing_fields: list[str] | None = None,
) -> OperationPlan:
    nodes = [
        OperationNode(
            step_id="step_clip_default",
            op_name="raster.clip",
            depends_on=[],
            inputs={},
            params={"crop": True},
            outputs={"raster": "raster_default"},
            retry_policy={"max_retries": 1},
        ),
        OperationNode(
            step_id="step_export_default",
            op_name="artifact.export",
            depends_on=["step_clip_default"],
            inputs={"primary": "raster_default"},
            params={"formats": ["geotiff", "png_map"]},
            outputs={"artifact": "artifact_default"},
            retry_policy={"max_retries": 0},
        ),
    ]
    return OperationPlan(
        version=version,
        status=status,
        missing_fields=list(missing_fields or []),
        nodes=nodes,
    )
