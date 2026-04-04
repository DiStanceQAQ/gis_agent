from __future__ import annotations

from sqlalchemy.orm import Session

from packages.domain.database import SessionLocal
from packages.domain.logging import get_logger
from packages.domain.models import LLMCallLogRecord

logger = get_logger(__name__)


def write_llm_call_log(
    *,
    task_id: str | None,
    phase: str,
    model_name: str,
    request_id: str | None,
    prompt_hash: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    latency_ms: int,
    status: str,
    error_message: str | None,
    db_session: Session | None = None,
) -> None:
    record = LLMCallLogRecord(
        task_id=task_id,
        phase=phase,
        model_name=model_name,
        request_id=request_id,
        prompt_hash=prompt_hash,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        status=status,
        error_message=error_message,
    )
    try:
        if db_session is not None:
            with db_session.begin_nested():
                db_session.add(record)
                db_session.flush()
            return
        with SessionLocal() as db:
            db.add(record)
            db.commit()
    except Exception:
        # Telemetry must not block runtime calls.
        logger.warning("llm_call_logs.write_failed", exc_info=True)
