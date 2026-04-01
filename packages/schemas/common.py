from typing import Any

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    detail: dict[str, Any] | list[Any] | str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
