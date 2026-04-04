from packages.domain.database import Base, SessionLocal
from packages.domain.models import LLMCallLogRecord, MessageRecord, SessionRecord, TaskRunRecord
from packages.domain.services.llm_call_logs import write_llm_call_log
from packages.domain.utils import make_id


def test_llm_call_logs_table_is_registered() -> None:
    table = LLMCallLogRecord.__table__

    assert table is Base.metadata.tables["llm_call_logs"]
    assert "id" in table.columns
    assert "task_id" in table.columns
    assert "created_at" in table.columns
    assert "phase" in table.columns
    assert "model_name" in table.columns
    assert "request_id" in table.columns
    assert "prompt_hash" in table.columns
    assert "input_tokens" in table.columns
    assert "output_tokens" in table.columns
    assert "total_tokens" in table.columns
    assert "latency_ms" in table.columns
    assert "status" in table.columns
    assert "error_message" in table.columns
    assert table.c.task_id.nullable is True
    assert any(
        fk.parent.name == "task_id" and fk.column.table.name == "task_runs" and fk.column.name == "id"
        for fk in table.foreign_keys
    )
    assert any(
        fk.parent.name == "task_id" and fk.ondelete == "CASCADE"
        for fk in table.foreign_keys
    )
    assert any(index.name == "ix_llm_call_logs_task_id" for index in table.indexes)


def test_write_llm_call_log_persists_and_is_queryable_by_task_id() -> None:
    session_id = make_id("ses")
    message_id = make_id("msg")
    task_id = make_id("task")

    with SessionLocal() as db:
        db.add(SessionRecord(id=session_id, status="active"))
        db.add(MessageRecord(id=message_id, session_id=session_id, role="user", content="audit"))
        db.add(TaskRunRecord(id=task_id, session_id=session_id, user_message_id=message_id, status="queued"))
        db.commit()

    try:
        write_llm_call_log(
            task_id=task_id,
            phase="recommend",
            model_name="gpt-test",
            request_id="req_success",
            prompt_hash="a" * 64,
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            latency_ms=123,
            status="success",
            error_message=None,
        )
        write_llm_call_log(
            task_id=task_id,
            phase="react_step",
            model_name="gpt-test",
            request_id="req_failed",
            prompt_hash="b" * 64,
            input_tokens=9,
            output_tokens=2,
            total_tokens=11,
            latency_ms=88,
            status="failed",
            error_message="schema_validation_failed",
        )

        with SessionLocal() as db:
            records = (
                db.query(LLMCallLogRecord)
                .filter(LLMCallLogRecord.task_id == task_id)
                .order_by(LLMCallLogRecord.id.asc())
                .all()
            )

        assert len(records) == 2
        assert [record.phase for record in records] == ["recommend", "react_step"]
        assert records[0].status == "success"
        assert records[1].status == "failed"
        assert records[1].error_message == "schema_validation_failed"
        assert records[0].model_name == "gpt-test"
        assert records[0].prompt_hash == "a" * 64
        assert records[0].request_id == "req_success"
    finally:
        with SessionLocal() as db:
            db.query(LLMCallLogRecord).filter(LLMCallLogRecord.task_id == task_id).delete(
                synchronize_session=False
            )
            db.query(TaskRunRecord).filter(TaskRunRecord.id == task_id).delete(synchronize_session=False)
            db.query(MessageRecord).filter(MessageRecord.id == message_id).delete(synchronize_session=False)
            db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
            db.commit()
