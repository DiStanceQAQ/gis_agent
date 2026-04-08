from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from packages.schemas.operation_plan import AllowedOperationName
from packages.schemas.analysis import (
    AnalysisType,
    normalize_analysis_type,
    normalize_operation_params,
)


AllowedToolName = Literal[
    "planner.build",
    "aoi.normalize",
    "catalog.search",
    "recommendation.rank",
    "processing.run",
    "raster.compute",
    "terrain.derive",
    "vector.buffer",
    "artifacts.publish",
]

AllowedStepName = Literal[
    "plan_task",
    "normalize_aoi",
    "search_candidates",
    "recommend_dataset",
    "run_processing_pipeline",
    "generate_outputs",
]

ReactDecision = Literal["continue", "skip", "fail"]


class LLMTimeRange(BaseModel):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def validate_iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class LLMParsedSpec(BaseModel):
    aoi_input: str | None = None
    aoi_source_type: Literal["bbox", "file_upload", "admin_name", "place_alias"] | None = None
    time_range: LLMTimeRange | None = None
    requested_dataset: Literal["sentinel2", "landsat89"] | None = None
    analysis_type: AnalysisType = "WORKFLOW"
    preferred_output: list[str] = Field(default_factory=lambda: ["png_map", "methods_text"])
    user_priority: Literal["balanced", "spatial", "temporal"] = "balanced"
    need_confirmation: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
    operation_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("analysis_type", mode="before")
    @classmethod
    def validate_analysis_type(cls, value: Any) -> AnalysisType:
        return normalize_analysis_type(value)

    @model_validator(mode="after")
    def validate_operation_params(self) -> "LLMParsedSpec":
        self.operation_params = normalize_operation_params(
            self.analysis_type, self.operation_params
        )
        return self


class LLMPlanStep(BaseModel):
    step_name: AllowedStepName
    tool_name: AllowedToolName
    title: str
    purpose: str
    reasoning: str | None = None
    depends_on: list[AllowedStepName] = Field(default_factory=list)
    input_payload: dict[str, Any] = Field(default_factory=dict)


class LLMTaskPlan(BaseModel):
    version: str = "agent-v2"
    mode: str = "llm_plan_execute_gis_workspace"
    objective: str
    reasoning_summary: str
    missing_fields: list[str] = Field(default_factory=list)
    steps: list[LLMPlanStep]
    operation_plan_nodes: list["LLMOperationNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_steps(self) -> "LLMTaskPlan":
        step_names = [item.step_name for item in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("task plan steps must have unique step_name values")
        return self


class LLMOperationNode(BaseModel):
    step_id: str = Field(min_length=1)
    op_name: AllowedOperationName
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    retry_policy: dict[str, int] = Field(default_factory=lambda: {"max_retries": 0})

    @model_validator(mode="after")
    def validate_step_id_not_blank(self) -> "LLMOperationNode":
        if not self.step_id.strip():
            raise ValueError("operation node step_id must not be blank")
        return self


class LLMReactStepDecision(BaseModel):
    decision: ReactDecision
    function_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reasoning_summary: str

    @model_validator(mode="after")
    def validate_function_requirements(self) -> "LLMReactStepDecision":
        if self.decision == "continue" and not self.function_name:
            raise ValueError("function_name is required when decision=continue")
        return self


class LLMRecommendation(BaseModel):
    primary_dataset: str
    backup_dataset: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    reason: str
    risk_note: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
