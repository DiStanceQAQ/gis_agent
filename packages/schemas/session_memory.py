from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SessionMemoryEventPayload(BaseModel):
    event_type: str
    message_id: str | None = None
    revision_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class SessionStateSnapshotPayload(BaseModel):
    lineage_root_id: str | None = None
    active_revision_id: str | None = None
    active_summary_id: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    history_features: dict[str, Any] = Field(default_factory=dict)


class SessionMemorySummaryPayload(BaseModel):
    summary_type: str
    summary_text: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    source_event_id: str | None = None


class LineageLinkPayload(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    link_type: str
    weight: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
