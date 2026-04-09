from datetime import datetime
from typing import Any

from pydantic import Field

from packages.schemas.common import ORMModel


class SessionResponse(ORMModel):
    session_id: str
    status: str


class SessionTaskItemResponse(ORMModel):
    task_id: str
    parent_task_id: str | None = None
    status: str
    current_step: str | None = None
    analysis_type: str
    created_at: datetime | None = None
    task_spec: dict[str, Any] | None = None


class SessionTasksResponse(ORMModel):
    session_id: str
    tasks: list[SessionTaskItemResponse]


class SessionMessageItemResponse(ORMModel):
    message_id: str
    role: str
    content: str
    linked_task_id: str | None = None
    created_at: datetime | None = None


class SessionMessagesResponse(ORMModel):
    session_id: str
    next_cursor: str | None = None
    messages: list[SessionMessageItemResponse]


class SessionMemoryLineageItemResponse(ORMModel):
    understanding_id: str
    message_id: str | None = None
    message_role: str | None = None
    message_excerpt: str | None = None
    intent: str
    response_mode: str | None = None
    derived_revision_id: str | None = None
    revision_number: int | None = None
    snapshot_id: str | None = None
    summary_id: str | None = None
    lineage_root_id: str | None = None
    created_at: datetime | None = None
    link_targets: list[dict[str, Any]] = Field(default_factory=list)


class SessionMemoryResponse(ORMModel):
    session_id: str
    snapshot: dict[str, Any] | None = None
    latest_summary: dict[str, Any] | None = None
    lineage: list[SessionMemoryLineageItemResponse] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
