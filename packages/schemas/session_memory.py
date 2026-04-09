from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SessionMemoryEventPayload(BaseModel):
    event_type: str
    message_id: str | None = None
    task_id: str | None = None
    revision_id: str | None = None
    event_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class SessionStateSnapshotPayload(BaseModel):
    active_task_id: str | None = None
    active_revision_id: str | None = None
    active_revision_number: int | None = None
    active_understanding_id: str | None = None
    open_missing_fields: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    latest_summary_id: str | None = None
    field_history_rollup: dict[str, Any] = Field(default_factory=dict)
    user_preference_profile: dict[str, Any] = Field(default_factory=dict)
    risk_profile: dict[str, Any] = Field(default_factory=dict)
    snapshot_version: int = 1


class SessionMemorySummaryPayload(BaseModel):
    summary_kind: str
    summary_text: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    source_event_range: dict[str, Any] = Field(default_factory=dict)


class LineageLinkPayload(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    link_type: str
    weight: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
