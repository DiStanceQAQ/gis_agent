from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from packages.schemas.analysis import AnalysisType, normalize_analysis_type, normalize_operation_params
from packages.schemas.operation_plan import OperationPlan


class ParsedTaskSpec(BaseModel):
    aoi_input: str | None = None
    aoi_source_type: str | None = None
    time_range: dict[str, str] | None = None
    requested_dataset: str | None = None
    analysis_type: AnalysisType = "NDVI"
    preferred_output: list[str] = Field(default_factory=lambda: ["png_map", "methods_text"])
    user_priority: str = "balanced"
    operation_params: dict[str, Any] = Field(default_factory=dict)
    need_confirmation: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
    created_from: str | None = None

    @field_validator("analysis_type", mode="before")
    @classmethod
    def validate_analysis_type(cls, value: Any) -> AnalysisType:
        return normalize_analysis_type(value)

    @model_validator(mode="after")
    def validate_operation_params(self) -> "ParsedTaskSpec":
        self.operation_params = normalize_operation_params(self.analysis_type, self.operation_params)
        return self


class RecommendationResponse(BaseModel):
    primary_dataset: str
    backup_dataset: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    reason: str
    risk_note: str | None = None
    confidence: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class CandidateResponse(BaseModel):
    dataset_name: str
    collection_id: str
    scene_count: int
    coverage_ratio: float
    effective_pixel_ratio_estimate: float
    cloud_metric_summary: dict[str, float] | None = None
    spatial_resolution: int
    temporal_density_note: str
    suitability_score: float
    recommendation_rank: int
    summary_json: dict[str, Any] | None = None


class TaskStepResponse(BaseModel):
    step_name: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    observation: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


class TaskPlanStepResponse(BaseModel):
    step_name: str
    tool_name: str
    title: str
    purpose: str
    status: str
    reasoning: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    detail: dict[str, Any] | None = None


class TaskPlanResponse(BaseModel):
    version: str
    mode: str
    status: str
    objective: str
    reasoning_summary: str
    missing_fields: list[str] = Field(default_factory=list)
    steps: list[TaskPlanStepResponse] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class TaskEventResponse(BaseModel):
    event_id: int
    event_type: str
    step_name: str | None = None
    status: str | None = None
    created_at: datetime
    detail: dict[str, Any] | None = None


class TaskEventsResponse(BaseModel):
    task_id: str
    status: str
    current_step: str | None = None
    next_cursor: int
    events: list[TaskEventResponse] = Field(default_factory=list)


class ArtifactResponse(BaseModel):
    artifact_id: str
    artifact_type: str
    mime_type: str
    size_bytes: int
    download_url: str


class TaskDetailResponse(BaseModel):
    task_id: str
    parent_task_id: str | None = None
    status: str
    current_step: str | None = None
    analysis_type: str
    created_at: datetime | None = None
    task_spec: dict[str, Any] | None = None
    task_plan: TaskPlanResponse | None = None
    operation_plan: OperationPlan | None = None
    recommendation: RecommendationResponse | None = None
    candidates: list[CandidateResponse] = Field(default_factory=list)
    steps: list[TaskStepResponse] = Field(default_factory=list)
    artifacts: list[ArtifactResponse] = Field(default_factory=list)
    summary_text: str | None = None
    methods_text: str | None = None
    png_preview_url: str | None = None
    aoi_name: str | None = None
    aoi_bbox_bounds: list[float] | None = None
    aoi_area_km2: float | None = None
    requested_time_range: dict[str, str] | None = None
    actual_time_range: dict[str, str] | None = None
    error_code: str | None = None
    error_message: str | None = None
    clarification_message: str | None = None


class RerunTaskRequest(BaseModel):
    override: dict[str, Any] | None = None


class TaskPlanPatchRequest(BaseModel):
    operation_plan: OperationPlan


class TaskPlanApproveRequest(BaseModel):
    approved_version: int = Field(ge=1)
