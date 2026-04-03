from packages.domain.database import Base
from packages.domain.models import LLMCallLogRecord


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
