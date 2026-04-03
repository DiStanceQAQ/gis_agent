from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


AllowedOperationName = Literal[
    "input.upload_raster",
    "input.upload_vector",
    "raster.clip",
    "raster.reproject",
    "raster.resample",
    "raster.band_math",
    "raster.zonal_stats",
    "vector.buffer",
    "artifact.export",
]

OperationPlanStatus = Literal["draft", "validated", "approved"]


class OperationNode(BaseModel):
    step_id: str = Field(min_length=1)
    op_name: AllowedOperationName
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    retry_policy: dict[str, int] = Field(default_factory=lambda: {"max_retries": 0})


class OperationPlan(BaseModel):
    version: int = Field(ge=1)
    status: OperationPlanStatus
    missing_fields: list[str] = Field(default_factory=list)
    nodes: list[OperationNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "OperationPlan":
        step_ids = [node.step_id for node in self.nodes]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("operation plan nodes must have unique step_id values")
        return self
