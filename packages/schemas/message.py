from typing import Literal

from pydantic import BaseModel, Field, model_validator

from packages.schemas.understanding import ResponseMode, UnderstandingResponse


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
    response_mode: ResponseMode | None = None
    understanding: UnderstandingResponse | None = None
    response_payload: dict[str, object] | None = None
    awaiting_task_confirmation: bool = False
    need_clarification: bool = False
    need_approval: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "MessageCreateResponse":
        if self.mode == "task":
            if not self.task_id:
                raise ValueError("task_id is required when mode is 'task'")
            if not self.task_status:
                raise ValueError("task_status is required when mode is 'task'")
        return self
