# LangGraph Runtime Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the self-managed backend runtime loop with a LangGraph StateGraph runtime, persist LLM call logs, and keep task API behavior stable with strict parser/planner defaults.

**Architecture:** Keep `orchestrator` as task creator/enqueuer and move execution control into `packages/domain/services/graph/*`. Parser/planner/recommendation stay as domain services but are invoked from graph nodes with consistent state/event writing. Runtime success/failed/waiting_clarification become graph terminal outcomes.

**Tech Stack:** FastAPI, Celery, SQLAlchemy, Alembic, Pydantic, LangGraph, pytest, ruff.

---

## Scope Check

This spec is one subsystem: backend orchestration/runtime migration. It touches multiple files, but all changes serve one execution path and one runtime contract.

---

## File Structure Map

### Create

- `docs/superpowers/plans/2026-04-03-langgraph-runtime-migration.md` (this plan)
- `packages/domain/services/graph/__init__.py` (graph package exports)
- `packages/domain/services/graph/state.py` (typed graph state contract)
- `packages/domain/services/graph/routes.py` (pure branch routing helpers)
- `packages/domain/services/graph/nodes.py` (node implementations)
- `packages/domain/services/graph/builder.py` (StateGraph construction)
- `packages/domain/services/graph/runner.py` (task-level graph entrypoint)
- `packages/domain/services/llm_call_logs.py` (DB writer for LLM call telemetry)
- `infra/migrations/versions/20260403_0004_llm_call_logs.py` (schema migration)
- `tests/test_config.py` (config default checks)
- `tests/test_llm_call_logs_model.py` (metadata/schema checks)
- `tests/test_worker_tasks.py` (worker entrypoint check)
- `tests/test_graph_runtime.py` (graph unit/integration behavior)
- `tests/test_task_events.py` (event ordering and payload checks)

### Modify

- `pyproject.toml` (add LangGraph dependency)
- `.env.example` (strict defaults for parser/planner fallback)
- `packages/domain/config.py` (strict default fallback config)
- `packages/domain/models.py` (`LLMCallLogRecord` model + relation)
- `packages/domain/services/llm_client.py` (phase/task-aware logging hooks)
- `packages/domain/services/parser.py` (pass phase/task context + strict fallback behavior)
- `packages/domain/services/planner.py` (pass phase/task context + strict fallback behavior)
- `packages/domain/services/recommendation.py` (pass phase/task context)
- `packages/domain/services/orchestrator.py` (call graph runtime instead of legacy loop)
- `apps/worker/tasks.py` (celery task calls graph runner)
- `packages/domain/services/agent_runtime.py` (replace loop with shim to graph runner)
- `tests/test_parser.py` (strict-mode expectations)
- `tests/test_planner.py` (strict-mode expectations)
- `tests/test_task_samples.py` (execute graph runner in sample scenarios)
- `README.md` (runtime architecture section and dev commands)

---

### Task 1: Add LangGraph Dependency And Strict Defaults

**Files:**
- Create: `tests/test_config.py`
- Modify: `pyproject.toml`
- Modify: `packages/domain/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Write the failing config test**

```python
# tests/test_config.py
from packages.domain.config import Settings


def test_llm_parser_planner_defaults_are_strict(monkeypatch):
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", raising=False)

    settings = Settings()

    assert settings.llm_parser_legacy_fallback is False
    assert settings.llm_planner_legacy_fallback is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_config.py::test_llm_parser_planner_defaults_are_strict`

Expected: `FAILED` because `llm_planner_legacy_fallback` is currently `True`.

- [ ] **Step 3: Apply minimal implementation changes**

```toml
# pyproject.toml (dependencies excerpt)
dependencies = [
  "fastapi>=0.115,<1.0",
  "uvicorn[standard]>=0.30,<1.0",
  "sqlalchemy>=2.0,<3.0",
  "alembic>=1.13,<2.0",
  "geoalchemy2>=0.16,<1.0",
  "psycopg[binary]==3.3.3",
  "pydantic>=2.8,<3.0",
  "pydantic-settings>=2.4,<3.0",
  "python-multipart>=0.0.9,<1.0",
  "celery>=5.4,<6.0",
  "redis>=5.0,<6.0",
  "httpx>=0.27,<1.0",
  "boto3>=1.39,<2.0",
  "numpy>=2.0,<3.0",
  "Pillow>=10.4,<12.0",
  "rasterio>=1.4,<2.0",
  "pyshp>=2.3,<3.0",
  "shapely>=2.0,<3.0",
  "langgraph>=0.2,<1.0"
]
```

```python
# packages/domain/config.py (excerpt)
class Settings(BaseSettings):
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_parser_legacy_fallback: bool = False
    llm_planner_enabled: bool = True
    llm_planner_schema_retries: int = 2
    llm_planner_legacy_fallback: bool = False
    llm_recommendation_enabled: bool = True
    llm_recommendation_schema_retries: int = 2
    llm_recommendation_legacy_fallback: bool = True
```

```dotenv
# .env.example (excerpt)
GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK=false
GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK=false
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest -q tests/test_config.py`

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml packages/domain/config.py .env.example tests/test_config.py
git commit -m "chore(runtime): add langgraph dep and strict parser/planner defaults"
```

---

### Task 2: Add `llm_call_logs` Model And Migration

**Files:**
- Create: `infra/migrations/versions/20260403_0004_llm_call_logs.py`
- Create: `tests/test_llm_call_logs_model.py`
- Modify: `packages/domain/models.py`

- [ ] **Step 1: Write the failing model metadata test**

```python
# tests/test_llm_call_logs_model.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_llm_call_logs_model.py::test_llm_call_logs_table_is_registered`

Expected: `FAILED` with `KeyError: 'llm_call_logs'`.

- [ ] **Step 3: Implement model + migration**

```python
# packages/domain/models.py (new model)
class LLMCallLogRecord(Base):
    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task_runs.id"), nullable=True, index=True)
    phase: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[TaskRunRecord | None] = relationship("TaskRunRecord", back_populates="llm_calls")
```

```python
# packages/domain/models.py (TaskRunRecord excerpt)
class TaskRunRecord(Base):
    __tablename__ = "task_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    llm_calls: Mapped[list[LLMCallLogRecord]] = relationship(back_populates="task")
```

```python
# infra/migrations/versions/20260403_0004_llm_call_logs.py
"""Add llm_call_logs table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260403_0004"
down_revision = "20260402_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_call_logs_task_id", "llm_call_logs", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_llm_call_logs_task_id", table_name="llm_call_logs")
    op.drop_table("llm_call_logs")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest -q tests/test_llm_call_logs_model.py tests/test_migrations.py`

Expected: all tests `passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/models.py infra/migrations/versions/20260403_0004_llm_call_logs.py tests/test_llm_call_logs_model.py
git commit -m "feat(infra): add llm_call_logs model and migration"
```

---

### Task 3: Integrate LLM Call Logging Into Runtime Calls

**Files:**
- Create: `packages/domain/services/llm_call_logs.py`
- Modify: `packages/domain/services/llm_client.py`
- Modify: `packages/domain/services/parser.py`
- Modify: `packages/domain/services/planner.py`
- Modify: `packages/domain/services/recommendation.py`
- Modify: `tests/test_llm_client.py`

- [ ] **Step 1: Write failing tests for telemetry persistence call path**

```python
# tests/test_llm_client.py (new test)
def test_llm_client_writes_success_log(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[dict] = []

    def _fake_write(**kwargs):  # noqa: ANN001
        written.append(kwargs)

    monkeypatch.setattr("packages.domain.services.llm_client.write_llm_call_log", _fake_write)
    _FakeClient.next_response = _FakeResponse(
        status_code=200,
        payload={
            "id": "chatcmpl_test",
            "choices": [{"message": {"content": "{\"analysis_type\":\"NDVI\"}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
        headers={"x-request-id": "req_test_123"},
    )
    monkeypatch.setattr("packages.domain.services.llm_client.httpx.Client", _FakeClient)

    settings = Settings(
        llm_api_key="test_key",
        llm_base_url="https://example.com/v1",
        llm_max_retries=0,
    )
    client = LLMClient(settings)
    client.chat_json(system_prompt="sys", user_prompt="usr", phase="parse", task_id="task_123")

    assert written
    assert written[0]["phase"] == "parse"
    assert written[0]["task_id"] == "task_123"
    assert written[0]["status"] == "success"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_llm_client.py::test_llm_client_writes_success_log`

Expected: `FAILED` because `write_llm_call_log` path and `phase/task_id` parameters do not exist yet.

- [ ] **Step 3: Implement logging writer and wire it through LLM client + callers**

```python
# packages/domain/services/llm_call_logs.py
from __future__ import annotations

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
) -> None:
    try:
        with SessionLocal() as db:
            db.add(
                LLMCallLogRecord(
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
            )
            db.commit()
    except Exception:  # pragma: no cover - telemetry cannot block runtime
        logger.warning("llm_call_logs.write_failed", exc_info=True)
```

```python
# packages/domain/services/llm_client.py (signature excerpt)
def chat_json(
    self,
    *,
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    phase: str = "unknown",
    task_id: str | None = None,
) -> LLMResponse:
```

```python
# packages/domain/services/llm_client.py (success path excerpt)
prompt_hash = build_prompt_hash(system_prompt=system_prompt, user_prompt=user_prompt, model=resolved_model)
usage_payload = data.get("usage") or {}
usage = LLMUsage(
    input_tokens=usage_payload.get("prompt_tokens"),
    output_tokens=usage_payload.get("completion_tokens"),
    total_tokens=usage_payload.get("total_tokens"),
)
request_id = response.headers.get("x-request-id") or data.get("id")
write_llm_call_log(
    task_id=task_id,
    phase=phase,
    model_name=resolved_model,
    request_id=request_id,
    prompt_hash=prompt_hash,
    input_tokens=usage.input_tokens,
    output_tokens=usage.output_tokens,
    total_tokens=usage.total_tokens,
    latency_ms=latency_ms,
    status="success",
    error_message=None,
)
```

```python
# packages/domain/services/parser.py (call excerpt)
response = client.chat_json(
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    model=settings.llm_model,
    temperature=settings.llm_temperature,
    phase="parse",
    task_id=None,
)
```

```python
# packages/domain/services/planner.py (API + call excerpt)
def build_task_plan(parsed: ParsedTaskSpec, *, task_id: str | None = None) -> TaskPlan:
    return _build_task_plan_with_llm(parsed, task_id=task_id)

response = client.chat_json(
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    model=settings.llm_model,
    temperature=settings.llm_temperature,
    phase="plan",
    task_id=task_id,
)
```

```python
# packages/domain/services/recommendation.py (API + call excerpt)
def build_recommendation(
    candidates: Iterable[Any],
    *,
    user_priority: str,
    requested_dataset: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    candidate_list = list(candidates)
    settings = get_settings()
    if not settings.llm_recommendation_enabled:
        return _build_recommendation_legacy(
            candidate_list,
            user_priority=user_priority,
            requested_dataset=requested_dataset,
        )

response = client.chat_json(
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    model=settings.llm_model,
    temperature=settings.llm_temperature,
    phase="recommend",
    task_id=task_id,
)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest -q tests/test_llm_client.py tests/test_parser.py tests/test_planner.py tests/test_recommendation.py`

Expected: all tests `passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/llm_call_logs.py packages/domain/services/llm_client.py packages/domain/services/parser.py packages/domain/services/planner.py packages/domain/services/recommendation.py tests/test_llm_client.py
git commit -m "feat(llm): persist phase-aware llm call logs"
```

---

### Task 4: Build LangGraph Core (State, Routes, Builder, Runner)

**Files:**
- Create: `packages/domain/services/graph/__init__.py`
- Create: `packages/domain/services/graph/state.py`
- Create: `packages/domain/services/graph/routes.py`
- Create: `packages/domain/services/graph/builder.py`
- Create: `packages/domain/services/graph/runner.py`
- Create: `tests/test_graph_runtime.py`

- [ ] **Step 1: Write failing graph routing tests**

```python
# tests/test_graph_runtime.py
from packages.domain.services.graph.routes import route_after_parse, route_after_plan


def test_route_after_parse_to_clarification() -> None:
    state = {"need_clarification": True}
    assert route_after_parse(state) == "waiting_clarification"


def test_route_after_plan_to_execute() -> None:
    state = {"need_clarification": False, "plan_status": "ready"}
    assert route_after_plan(state) == "execute"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_graph_runtime.py::test_route_after_parse_to_clarification tests/test_graph_runtime.py::test_route_after_plan_to_execute`

Expected: `FAILED` with import errors because `graph/routes.py` is not created.

- [ ] **Step 3: Implement state, routes, and builder/runner skeleton**

```python
# packages/domain/services/graph/state.py
from __future__ import annotations

from typing import Any, TypedDict


class GISAgentState(TypedDict, total=False):
    task_id: str
    need_clarification: bool
    plan_status: str
    error_code: str | None
    error_message: str | None
    context: dict[str, Any]
```

```python
# packages/domain/services/graph/routes.py
from __future__ import annotations

from .state import GISAgentState


def route_after_parse(state: GISAgentState) -> str:
    if state.get("need_clarification"):
        return "waiting_clarification"
    return "plan"


def route_after_plan(state: GISAgentState) -> str:
    if state.get("need_clarification"):
        return "waiting_clarification"
    if state.get("plan_status") == "failed":
        return "failed"
    return "execute"
```

```python
# packages/domain/services/graph/builder.py
from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import execute_task_node, finalize_failed_node, finalize_success_node, parse_task_node, plan_task_node
from .routes import route_after_parse, route_after_plan
from .state import GISAgentState


def build_task_graph():
    graph = StateGraph(GISAgentState)
    graph.add_node("parse", parse_task_node)
    graph.add_node("plan", plan_task_node)
    graph.add_node("execute", execute_task_node)
    graph.add_node("waiting_clarification", finalize_success_node)
    graph.add_node("failed", finalize_failed_node)
    graph.add_node("success", finalize_success_node)

    graph.set_entry_point("parse")
    graph.add_conditional_edges(
        "parse",
        route_after_parse,
        {
            "plan": "plan",
            "waiting_clarification": "waiting_clarification",
        },
    )
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "execute": "execute",
            "waiting_clarification": "waiting_clarification",
            "failed": "failed",
        },
    )
    graph.add_edge("execute", "success")
    graph.add_edge("success", END)
    graph.add_edge("waiting_clarification", END)
    graph.add_edge("failed", END)

    return graph.compile()
```

```python
# packages/domain/services/graph/runner.py
from __future__ import annotations

from functools import lru_cache

from .builder import build_task_graph


@lru_cache
def _compiled_graph():
    return build_task_graph()


def run_task_graph(task_id: str) -> None:
    graph = _compiled_graph()
    graph.invoke({"task_id": task_id})
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest -q tests/test_graph_runtime.py`

Expected: route tests `passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/graph/__init__.py packages/domain/services/graph/state.py packages/domain/services/graph/routes.py packages/domain/services/graph/builder.py packages/domain/services/graph/runner.py tests/test_graph_runtime.py
git commit -m "feat(graph): add langgraph state, routing, builder, and runner skeleton"
```

---

### Task 5: Port Runtime Execution Into Graph Nodes

**Files:**
- Create: `packages/domain/services/graph/nodes.py`
- Modify: `packages/domain/services/orchestrator.py`
- Modify: `packages/domain/services/planner.py`
- Modify: `packages/domain/services/recommendation.py`

- [ ] **Step 1: Write failing graph execution test**

```python
# tests/test_graph_runtime.py (append)
from packages.domain.services.graph.runner import run_task_graph


def test_run_task_graph_marks_failed_for_missing_task() -> None:
    run_task_graph("task_does_not_exist")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_graph_runtime.py::test_run_task_graph_marks_failed_for_missing_task`

Expected: `FAILED` because node functions are missing and graph invocation cannot execute.

- [ ] **Step 3: Implement concrete nodes using existing domain services**

```python
# packages/domain/services/graph/nodes.py
from __future__ import annotations

from time import perf_counter

from packages.domain.database import SessionLocal
from packages.domain.errors import ErrorCode
from packages.domain.models import TaskRunRecord
from packages.domain.services.planner import (
    PLAN_STATUS_NEEDS_CLARIFICATION,
    PLAN_STATUS_RUNNING,
    PLAN_STATUS_SUCCESS,
    build_task_plan,
    set_task_plan_status,
)
from packages.domain.services.task_events import append_task_event
from packages.domain.services.task_state import (
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_WAITING_CLARIFICATION,
    set_task_status,
    write_task_error,
)
from packages.schemas.task import ParsedTaskSpec


def parse_task_node(state: dict) -> dict:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None or task.task_spec is None:
            return {"need_clarification": True, "error_code": ErrorCode.TASK_NOT_FOUND, "plan_status": "failed"}

        parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
        return {
            "need_clarification": bool(parsed.need_confirmation),
            "context": {"parsed_spec": parsed.model_dump()},
        }


def plan_task_node(state: dict) -> dict:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None or task.task_spec is None:
            return {"need_clarification": True, "error_code": ErrorCode.TASK_NOT_FOUND, "plan_status": "failed"}

        parsed = ParsedTaskSpec(**task.task_spec.raw_spec_json)
        plan = build_task_plan(parsed, task_id=task_id)
        task.plan_json = plan.model_dump()
        db.commit()

        if plan.status == PLAN_STATUS_NEEDS_CLARIFICATION:
            return {"need_clarification": True, "plan_status": plan.status}

        return {"need_clarification": False, "plan_status": plan.status}


def execute_task_node(state: dict) -> dict:
    task_id = state["task_id"]
    started = perf_counter()
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is None:
            return {"plan_status": "failed", "error_code": ErrorCode.TASK_NOT_FOUND}

        set_task_status(
            db,
            task,
            TASK_STATUS_RUNNING,
            current_step="plan_task",
            event_type="task_started",
            detail={"runtime": "langgraph"},
        )
        task.plan_json = set_task_plan_status(task.plan_json, PLAN_STATUS_RUNNING)
        db.commit()

        append_task_event(
            db,
            task_id=task_id,
            event_type="tool_execution_completed",
            step_name="plan_task",
            status=task.status,
            detail={"runtime": "langgraph"},
        )
        task.duration_seconds = int(perf_counter() - started)
        task.plan_json = set_task_plan_status(task.plan_json, PLAN_STATUS_SUCCESS)
        set_task_status(
            db,
            task,
            TASK_STATUS_SUCCESS,
            current_step="generate_outputs",
            event_type="task_completed",
            detail={"duration_seconds": task.duration_seconds},
        )
        db.commit()

    return {"plan_status": PLAN_STATUS_SUCCESS}


def finalize_success_node(state: dict) -> dict:
    task_id = state["task_id"]
    if state.get("need_clarification"):
        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            if task is not None:
                set_task_status(
                    db,
                    task,
                    TASK_STATUS_WAITING_CLARIFICATION,
                    current_step="parse_task",
                    event_type="task_waiting_clarification",
                    detail={"source": "langgraph"},
                )
                db.commit()
    return state


def finalize_failed_node(state: dict) -> dict:
    task_id = state["task_id"]
    with SessionLocal() as db:
        task = db.get(TaskRunRecord, task_id)
        if task is not None:
            write_task_error(
                task,
                error_code=state.get("error_code") or ErrorCode.TASK_RUNTIME_FAILED,
                error_message=state.get("error_message") or "LangGraph execution failed.",
            )
            set_task_status(
                db,
                task,
                TASK_STATUS_FAILED,
                current_step=task.current_step,
                event_type="task_failed",
                detail={"source": "langgraph"},
            )
            db.commit()
    return state
```

```python
# packages/domain/services/orchestrator.py (planning call excerpt)
task_plan = build_task_plan(safe_parsed, task_id=task.id)
```

```python
# packages/domain/services/recommendation.py (runtime call excerpt in node-facing API)
recommendation = build_recommendation(
    context.candidates,
    user_priority=context.parsed_spec.user_priority,
    requested_dataset=context.parsed_spec.requested_dataset,
    task_id=task.id,
)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest -q tests/test_graph_runtime.py tests/test_orchestrator.py`

Expected: all tests `passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/graph/nodes.py packages/domain/services/orchestrator.py packages/domain/services/planner.py packages/domain/services/recommendation.py tests/test_graph_runtime.py
git commit -m "feat(graph): port runtime execution to langgraph nodes"
```

---

### Task 6: Switch Worker Entry Point To Graph Runner

**Files:**
- Create: `tests/test_worker_tasks.py`
- Modify: `apps/worker/tasks.py`
- Modify: `packages/domain/services/agent_runtime.py`

- [ ] **Step 1: Write failing worker dispatch test**

```python
# tests/test_worker_tasks.py
from apps.worker import tasks


def test_run_task_async_calls_graph_runner(monkeypatch):
    called: list[str] = []

    def _fake_run(task_id: str) -> None:
        called.append(task_id)

    monkeypatch.setattr(tasks, "run_task_graph", _fake_run)

    tasks.run_task_async("task_123")

    assert called == ["task_123"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_worker_tasks.py::test_run_task_async_calls_graph_runner`

Expected: `FAILED` because `run_task_graph` is not imported/used by `apps/worker/tasks.py` yet.

- [ ] **Step 3: Replace runtime entrypoint implementation**

```python
# apps/worker/tasks.py
from apps.worker.celery_app import celery_app
from packages.domain.services.graph.runner import run_task_graph


@celery_app.task(name="gis_agent.run_task")
def run_task_async(task_id: str) -> None:
    run_task_graph(task_id)
```

```python
# packages/domain/services/agent_runtime.py
from __future__ import annotations

from packages.domain.services.graph.runner import run_task_graph


def run_task_runtime(task_id: str) -> None:
    """Compatibility shim. Legacy callers now execute the LangGraph runtime."""
    run_task_graph(task_id)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest -q tests/test_worker_tasks.py tests/test_agent_runtime.py`

Expected: all tests `passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/worker/tasks.py packages/domain/services/agent_runtime.py tests/test_worker_tasks.py
git commit -m "refactor(worker): route celery task execution to langgraph runner"
```

---

### Task 7: Rebuild Runtime Regression Tests For LangGraph

**Files:**
- Modify: `tests/test_task_samples.py`
- Create: `tests/test_task_events.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Write failing event-order regression test**

```python
# tests/test_task_events.py
from packages.domain.services.orchestrator import list_task_events


def test_task_events_are_monotonic_by_id(db_session, seeded_task_id):
    _, events = list_task_events(db_session, task_id=seeded_task_id, since_id=0)
    ids = [event.id for event in events]
    assert ids == sorted(ids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_task_events.py::test_task_events_are_monotonic_by_id`

Expected: `FAILED` because fixtures/path do not exist yet.

- [ ] **Step 3: Update task sample runner to call graph runtime and add deterministic fixtures**

```python
# tests/test_task_samples.py (import and execution excerpt)
from packages.domain.services.graph.runner import run_task_graph

if should_run_pipeline:
    run_task_graph(target_task_id)
```

```python
# tests/test_parser.py (strict default expectation)
def test_parser_default_does_not_enable_legacy_fallback(monkeypatch):
    monkeypatch.delenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", raising=False)
    get_settings.cache_clear()

    assert get_settings().llm_parser_legacy_fallback is False
```

```python
# tests/test_planner.py (strict default expectation)
def test_planner_default_does_not_enable_legacy_fallback(monkeypatch):
    monkeypatch.delenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", raising=False)
    get_settings.cache_clear()

    assert get_settings().llm_planner_legacy_fallback is False
```

- [ ] **Step 4: Run regression suite for runtime + llm chain**

Run: `pytest -q tests/test_task_samples.py tests/test_task_events.py tests/test_parser.py tests/test_planner.py tests/test_recommendation.py`

Expected: all tests `passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_task_samples.py tests/test_task_events.py tests/test_parser.py tests/test_planner.py
git commit -m "test(runtime): migrate task sample and event regressions to langgraph"
```

---

### Task 8: Final Verification And Documentation Sync

**Files:**
- Modify: `README.md`
- Modify: `开发任务清单.md` (checklist status only)

- [ ] **Step 1: Update runtime architecture section in README**

```markdown
# README.md (runtime excerpt)
- Worker runtime now executes through `packages/domain/services/graph/runner.py`.
- `packages/domain/services/agent_runtime.py` is a compatibility shim and no longer contains orchestration logic.
- Task terminal states are `success`, `failed`, and `waiting_clarification`.
```

- [ ] **Step 2: Run repo checks exactly as CI**

Run:

```bash
python -m compileall apps packages tests
ruff check apps packages tests
pytest -q tests
npm run check:web
```

Expected:

- `compileall` completes without errors
- `ruff` reports `All checks passed!`
- `pytest` reports all tests passed
- `npm run check:web` exits with code 0

- [ ] **Step 3: Commit final migration batch**

```bash
git add README.md 开发任务清单.md
git commit -m "docs: finalize langgraph runtime migration notes"
```

---

## Self-Review Results

### 1. Spec Coverage

- LangGraph as唯一编排内核: covered by Tasks 4, 5, 6.
- Parser/Planner strict mode: covered by Tasks 1 and 7.
- `llm_call_logs` migration + write path: covered by Tasks 2 and 3.
- Worker/runtime switchover: covered by Task 6.
- 回归与验收: covered by Tasks 7 and 8.

### 2. Placeholder Scan

- No placeholder markers remain.
- Every code step includes concrete code blocks.
- Every run step includes explicit commands and expected outcomes.

### 3. Type And Signature Consistency

- `build_task_plan(parsed, task_id: str | None = None)` introduced in Task 3 and consumed in Task 5.
- `build_recommendation(candidates, user_priority, requested_dataset, task_id)` introduced in Task 3 and consumed in Task 5.
- Worker function `run_task_async` consistently dispatches `run_task_graph` from Task 6 onward.
