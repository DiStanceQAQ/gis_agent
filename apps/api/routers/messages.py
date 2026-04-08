from __future__ import annotations

import json
from collections.abc import Generator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.services.orchestrator import create_message
from packages.schemas.common import ErrorResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse

router = APIRouter(tags=["messages"])


def _sse_event(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _iter_text_chunks(text: str, *, chunk_size: int = 8) -> Generator[str, None, None]:
    normalized = text.strip()
    if not normalized:
        return
    for index in range(0, len(normalized), chunk_size):
        yield normalized[index : index + chunk_size]


def _stream_message_response(response: MessageCreateResponse) -> Generator[str, None, None]:
    if response.assistant_message:
        for chunk in _iter_text_chunks(response.assistant_message):
            yield _sse_event("delta", {"text": chunk})

    yield _sse_event("message", response.model_dump(mode="json"))
    yield _sse_event("done", {"ok": True})


@router.post(
    "/messages",
    response_model=MessageCreateResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def create_message_endpoint(
    payload: MessageCreateRequest,
    stream: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> MessageCreateResponse | StreamingResponse:
    response = create_message(db=db, payload=payload)
    if not stream:
        return response

    return StreamingResponse(
        _stream_message_response(response),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
