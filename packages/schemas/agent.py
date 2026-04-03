from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from packages.schemas.analysis import AnalysisType, normalize_analysis_type, normalize_operation_params


AllowedToolName = Literal[
    "planner.build",
    "aoi.normalize",
    "catalog.search",
    "recommendation.rank",
    "ndvi.run",
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
    "run_ndvi_pipeline",
    "run_processing_pipeline",
    "generate_outputs",
]


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
    analysis_type: AnalysisType = "NDVI"
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
        self.operation_params = normalize_operation_params(self.analysis_type, self.operation_params)
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

    @model_validator(mode="after")
    def validate_steps(self) -> "LLMTaskPlan":
        step_names = [item.step_name for item in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("task plan steps must have unique step_name values")
        return self


class LLMRecommendation(BaseModel):
    primary_dataset: str
    backup_dataset: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    reason: str
    risk_note: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
