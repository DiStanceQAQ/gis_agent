from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ConfidenceLevel = Literal["low", "medium", "high"]
ResponseMode = Literal[
    "execute_now",
    "confirm_understanding",
    "ask_missing_fields",
    "show_revision",
    "chat_reply",
]


class ConfidenceEvidence(BaseModel):
    source: str
    weight: float
    detail: str


class FieldConfidence(BaseModel):
    score: float
    level: ConfidenceLevel
    evidence: list[ConfidenceEvidence] = Field(default_factory=list)


class RevisionSummary(BaseModel):
    revision_id: str
    revision_number: int
    change_type: str
    created_at: datetime | None = None
    understanding_summary: str | None = None
    execution_blocked: bool = False
    execution_blocked_reason: str | None = None
    field_confidences: dict[str, FieldConfidence] = Field(default_factory=dict)
    ranked_candidates: dict[str, list[dict[str, object]]] = Field(default_factory=dict)


class UnderstandingResponse(BaseModel):
    intent: str
    intent_confidence: float
    understanding_summary: str | None = None
    revision_id: str | None = None
    revision_number: int | None = None
    editable_fields: list[str] = Field(default_factory=list)
    field_confidences: dict[str, FieldConfidence] = Field(default_factory=dict)
    field_evidence: dict[str, list[str]] = Field(default_factory=dict)
    ranked_candidates: dict[str, list[dict[str, object]]] = Field(default_factory=dict)
