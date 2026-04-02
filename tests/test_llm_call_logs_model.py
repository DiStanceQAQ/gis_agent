from packages.domain import models  # noqa: F401
from packages.domain.database import Base


def test_llm_call_logs_table_is_registered() -> None:
    table = Base.metadata.tables["llm_call_logs"]

    assert "id" in table.columns
    assert "task_id" in table.columns
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
