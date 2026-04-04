from typing import Literal

from pydantic import BaseModel, Field


class MessageCreateRequest(BaseModel):
    session_id: str
    content: str = Field(min_length=1)
    file_ids: list[str] = Field(default_factory=list)


class MessageCreateResponse(BaseModel):
    message_id: str
    mode: Literal["chat", "task"] = "task"
    task_id: str | None = None
    task_status: str | None = None
    assistant_message: str | None = None
    intent: str | None = None
    intent_confidence: float | None = None
    awaiting_task_confirmation: bool = False
    need_clarification: bool = False
    need_approval: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
