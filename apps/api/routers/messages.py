from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from packages.domain.database import SessionLocal
from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.orchestrator import create_message
from packages.schemas.common import ErrorResponse
from packages.schemas.message import MessageCreateRequest, MessageCreateResponse

router = APIRouter(tags=["messages"])


def _sse_event(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_message_response(
    *,
    payload: MessageCreateRequest,
) -> Generator[str, None, None]:
    # Immediate acknowledgement so the client can render an in-progress state
    # without waiting for the full orchestration chain to finish.
    yield ": connected\n\n"
    yield _sse_event("ready", {"accepted": True})

    event_queue: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue()

    def _worker() -> None:
        emitted_delta = False

        def _on_delta(text: str) -> None:
            nonlocal emitted_delta
            if not text:
                return
            emitted_delta = True
            event_queue.put(("delta", {"text": text}))

        try:
            with SessionLocal() as worker_db:
                response = create_message(
                    db=worker_db,
                    payload=payload,
                    chat_stream_handler=_on_delta,
                )
        except AppError as exc:
            event_queue.put(
                (
                    "error",
                    {
                        "code": exc.error_code,
                        "message": exc.message,
                        "detail": exc.detail,
                    },
                )
            )
            event_queue.put(("done", {"ok": False}))
            return
        except Exception:
            event_queue.put(
                (
                    "error",
                    {
                        "code": ErrorCode.INTERNAL_ERROR,
                        "message": "Internal server error.",
                    },
                )
            )
            event_queue.put(("done", {"ok": False}))
            return

        # Backward compatibility: if no live delta was emitted, still provide
        # one assistant delta for chat consumers.
        if response.assistant_message and not emitted_delta:
            event_queue.put(("delta", {"text": response.assistant_message}))
        event_queue.put(("message", response.model_dump(mode="json")))
        event_queue.put(("done", {"ok": True}))

    worker_thread = threading.Thread(target=_worker, daemon=True)
    worker_thread.start()

    while True:
        try:
            event_name, event_payload = event_queue.get(timeout=0.5)
        except queue.Empty:
            # Keep stream active through long-running task/planning phases.
            yield ": keep-alive\n\n"
            if not worker_thread.is_alive() and event_queue.empty():
                yield _sse_event(
                    "error",
                    {
                        "code": ErrorCode.INTERNAL_ERROR,
                        "message": "Message stream ended unexpectedly.",
                    },
                )
                yield _sse_event("done", {"ok": False})
                return
            continue

        yield _sse_event(event_name, event_payload)
        if event_name == "done":
            return


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
        _stream_message_response(payload=payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
