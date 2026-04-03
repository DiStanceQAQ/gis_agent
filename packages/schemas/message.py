from pydantic import BaseModel, Field


class MessageCreateRequest(BaseModel):
    session_id: str
    content: str = Field(min_length=1)
    file_ids: list[str] = Field(default_factory=list)


class MessageCreateResponse(BaseModel):
    message_id: str
    task_id: str
    task_status: str
    need_clarification: bool
    need_approval: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
