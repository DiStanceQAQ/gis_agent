from datetime import datetime
from typing import Any

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
