from __future__ import annotations

import json
from collections.abc import Generator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.orchestrator import create_message
from packages.schemas.common import ErrorResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse

router = APIRouter(tags=["messages"])


def _sse_event(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_message_response(
    *,
    db: Session,
    payload: MessageCreateRequest,
) -> Generator[str, None, None]:
    # Immediate acknowledgement so the client can render an in-progress state
    # without waiting for the full orchestration chain to finish.
    yield ": connected\n\n"
    yield _sse_event("ready", {"accepted": True})

    try:
        response = create_message(db=db, payload=payload)
    except AppError as exc:
        db.rollback()
        yield _sse_event(
            "error",
            {
                "code": exc.error_code,
                "message": exc.message,
                "detail": exc.detail,
            },
        )
        yield _sse_event("done", {"ok": False})
        return
    except Exception:
        db.rollback()
        yield _sse_event(
            "error",
            {
                "code": ErrorCode.INTERNAL_ERROR,
                "message": "Internal server error.",
            },
        )
        yield _sse_event("done", {"ok": False})
        return

    # Keep delta event for forward compatibility. We no longer synthesize
    # fake chunked output from a completed response.
    if response.assistant_message:
        yield _sse_event("delta", {"text": response.assistant_message})
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
    if not stream:
        return create_message(db=db, payload=payload)

    return StreamingResponse(
        _stream_message_response(db=db, payload=payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
