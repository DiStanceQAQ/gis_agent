from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ParsedTaskSpec(BaseModel):
    aoi_input: str | None = None
    aoi_source_type: str | None = None
    time_range: dict[str, str] | None = None
    analysis_type: str = "NDVI"
    preferred_output: list[str] = Field(default_factory=lambda: ["png_map", "methods_text"])
    user_priority: str = "balanced"
    need_confirmation: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
    created_from: str | None = None


class RecommendationResponse(BaseModel):
    primary_dataset: str
    backup_dataset: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    reason: str
    risk_note: str | None = None


class CandidateResponse(BaseModel):
    dataset_name: str
    scene_count: int
    coverage_ratio: float
    effective_pixel_ratio_estimate: float
    spatial_resolution: int
    temporal_density_note: str
    suitability_score: float
    recommendation_rank: int


class TaskStepResponse(BaseModel):
    step_name: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    detail: dict[str, Any] | None = None


class ArtifactResponse(BaseModel):
    artifact_id: str
    artifact_type: str
    mime_type: str
    size_bytes: int
    download_url: str


class TaskDetailResponse(BaseModel):
    task_id: str
    status: str
    current_step: str | None = None
    analysis_type: str
    task_spec: dict[str, Any] | None = None
    recommendation: RecommendationResponse | None = None
    candidates: list[CandidateResponse] = Field(default_factory=list)
    steps: list[TaskStepResponse] = Field(default_factory=list)
    artifacts: list[ArtifactResponse] = Field(default_factory=list)
    summary_text: str | None = None
    methods_text: str | None = None
    png_preview_url: str | None = None
    requested_time_range: dict[str, str] | None = None
    actual_time_range: dict[str, str] | None = None
    error_message: str | None = None
    clarification_message: str | None = None


class RerunTaskRequest(BaseModel):
    override: dict[str, Any] | None = None
