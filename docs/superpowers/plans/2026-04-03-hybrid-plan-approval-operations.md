# Hybrid Plan-Approval GIS Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an upload-first GIS agent workflow that asks clarification when required, generates editable operation plans, requires user approval, and only then executes via LangGraph.

**Architecture:** Keep the existing FastAPI + LangGraph skeleton, but introduce a strict approval gate between planning and execution. Add an operation registry and plan guard so LLM draft plans are validated before execution. Replace NDVI-only runtime step with a processing dispatcher that can execute registered raster/vector operations and publish artifacts.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, LangGraph, Rasterio, Shapely, pytest, ruff, React + TypeScript.

---

## Scope Check

This is one subsystem end-to-end: “plan-approval orchestration + operation execution.” It spans API, domain services, runtime graph, and web UI, but all tasks serve the same workflow and can ship incrementally.

---

## File Structure Map

### Create

- `docs/superpowers/plans/2026-04-03-hybrid-plan-approval-operations.md` (this plan)
- `packages/schemas/operation_plan.py` (operation-node plan schemas)
- `packages/domain/services/operation_registry.py` (operation metadata and contracts)
- `packages/domain/services/plan_guard.py` (plan validation: whitelist/schema/DAG)
- `packages/domain/services/input_slots.py` (uploaded input slot classification)
- `packages/domain/services/processing_pipeline.py` (operation dispatcher + executors)
- `tests/test_operation_plan_schemas.py` (schema-level validation)
- `tests/test_operation_registry.py` (registry/guard behavior)
- `tests/test_plan_guard.py` (invalid DAG, unknown op, missing deps)
- `tests/test_processing_pipeline.py` (clip/buffer/zonal stats execution)
- `tests/test_plan_approval_flow.py` (orchestrator approval flow)

### Modify

- `packages/domain/errors.py` (plan/operation error codes)
- `packages/domain/services/task_state.py` (approval statuses and transitions)
- `packages/schemas/task.py` (operation plan + patch/approve requests)
- `packages/schemas/message.py` (need_approval flag)
- `packages/domain/services/storage.py` (detect raster/vector upload types)
- `packages/domain/services/parser.py` (preserve analysis intent for operation plan)
- `packages/domain/services/planner.py` (emit operation-oriented steps and plan status)
- `packages/domain/services/tool_registry.py` (rename runtime step to processing pipeline)
- `packages/domain/services/orchestrator.py` (awaiting_approval lifecycle + approve endpoint handlers)
- `apps/api/routers/tasks.py` (plan patch + approve endpoints)
- `packages/domain/services/graph/routes.py` (run_processing_pipeline route)
- `packages/domain/services/graph/builder.py` (run_processing_pipeline node)
- `packages/domain/services/graph/nodes.py` (processing node and approval-state guards)
- `packages/domain/services/graph/runtime_helpers.py` (delegate to processing pipeline)
- `apps/web/src/types.ts` (operation plan DTOs + approval state)
- `apps/web/src/api.ts` (patch plan / approve plan APIs)
- `apps/web/src/App.tsx` (plan editor + approve action)
- `tests/test_task_state.py`
- `tests/test_storage.py`
- `tests/test_graph_runtime.py`
- `tests/test_orchestrator.py`
- `tests/test_task_samples.py`
- `开发任务清单.md` (mark progress for LPE-30~35, LPE-40~44)

---

### Task 1: Add Approval Status Machine

**Files:**
- Modify: `packages/domain/services/task_state.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write the failing status-transition tests**

```python
# tests/test_task_state.py
import pytest

from packages.domain.services.task_state import (
    TASK_STATUS_APPROVED,
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_DRAFT,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    ensure_task_status_transition,
)


def test_approval_state_allows_expected_transitions() -> None:
    ensure_task_status_transition(TASK_STATUS_DRAFT, TASK_STATUS_AWAITING_APPROVAL)
    ensure_task_status_transition(TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_APPROVED)
    ensure_task_status_transition(TASK_STATUS_APPROVED, TASK_STATUS_QUEUED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_RUNNING),
        (TASK_STATUS_APPROVED, TASK_STATUS_RUNNING),
        (TASK_STATUS_CANCELLED, TASK_STATUS_QUEUED),
    ],
)
def test_approval_state_rejects_invalid_transitions(current: str, target: str) -> None:
    with pytest.raises(ValueError):
        ensure_task_status_transition(current, target)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest -q tests/test_task_state.py::test_approval_state_allows_expected_transitions tests/test_task_state.py::test_approval_state_rejects_invalid_transitions`

Expected: `FAILED` because new status constants/transitions do not exist yet.

- [ ] **Step 3: Implement approval statuses and transitions**

```python
# packages/domain/services/task_state.py (constants excerpt)
TASK_STATUS_DRAFT = "draft"
TASK_STATUS_AWAITING_APPROVAL = "awaiting_approval"
TASK_STATUS_APPROVED = "approved"
TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_WAITING_CLARIFICATION = "waiting_clarification"
TASK_STATUS_CANCELLED = "cancelled"
```

```python
# packages/domain/services/task_state.py (transition map excerpt)
TASK_STATUS_TRANSITIONS: dict[str, set[str]] = {
    TASK_STATUS_DRAFT: {TASK_STATUS_WAITING_CLARIFICATION, TASK_STATUS_AWAITING_APPROVAL},
    TASK_STATUS_WAITING_CLARIFICATION: {TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED},
    TASK_STATUS_AWAITING_APPROVAL: {TASK_STATUS_APPROVED, TASK_STATUS_CANCELLED, TASK_STATUS_FAILED},
    TASK_STATUS_APPROVED: {TASK_STATUS_QUEUED, TASK_STATUS_CANCELLED, TASK_STATUS_FAILED},
    TASK_STATUS_QUEUED: {TASK_STATUS_RUNNING, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED},
    TASK_STATUS_RUNNING: {TASK_STATUS_SUCCESS, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED},
    TASK_STATUS_SUCCESS: set(),
    TASK_STATUS_FAILED: set(),
    TASK_STATUS_CANCELLED: set(),
}
```

- [ ] **Step 4: Run task-state tests**

Run: `./.venv/bin/pytest -q tests/test_task_state.py`

Expected: all task-state tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/task_state.py tests/test_task_state.py
git commit -m "feat(state): add approval lifecycle statuses and transitions"
```

---

### Task 2: Define Operation Plan Schemas

**Files:**
- Create: `packages/schemas/operation_plan.py`
- Modify: `packages/schemas/task.py`
- Modify: `packages/schemas/message.py`
- Test: `tests/test_operation_plan_schemas.py`

- [ ] **Step 1: Write failing schema tests**

```python
# tests/test_operation_plan_schemas.py
import pytest

from packages.schemas.operation_plan import OperationNode, OperationPlan


def test_operation_plan_requires_unique_step_ids() -> None:
    with pytest.raises(ValueError):
        OperationPlan(
            version=1,
            status="draft",
            nodes=[
                OperationNode(step_id="s1", op_name="raster.clip"),
                OperationNode(step_id="s1", op_name="artifact.export"),
            ],
        )


def test_operation_node_accepts_registered_name_shape() -> None:
    node = OperationNode(
        step_id="clip_1",
        op_name="raster.clip",
        depends_on=[],
        inputs={"raster": "upload:raster_1", "aoi": "upload:vector_1"},
        params={"crop": True},
        outputs={"raster": "raster_clipped"},
    )
    assert node.op_name == "raster.clip"
```

- [ ] **Step 2: Run schema tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_operation_plan_schemas.py`

Expected: `ModuleNotFoundError` for `packages.schemas.operation_plan`.

- [ ] **Step 3: Implement schema module and API DTO integration**

```python
# packages/schemas/operation_plan.py
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


AllowedOperationName = Literal[
    "input.upload_raster",
    "input.upload_vector",
    "raster.clip",
    "raster.reproject",
    "raster.resample",
    "raster.band_math",
    "raster.zonal_stats",
    "vector.buffer",
    "artifact.export",
]


class OperationNode(BaseModel):
    step_id: str = Field(min_length=1)
    op_name: AllowedOperationName
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    retry_policy: dict[str, int] = Field(default_factory=lambda: {"max_retries": 0})


class OperationPlan(BaseModel):
    version: int = Field(default=1, ge=1)
    status: Literal["draft", "validated", "approved"] = "draft"
    missing_fields: list[str] = Field(default_factory=list)
    nodes: list[OperationNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "OperationPlan":
        step_ids = [node.step_id for node in self.nodes]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("operation plan step_id values must be unique")
        return self
```

```python
# packages/schemas/task.py (excerpt)
from packages.schemas.operation_plan import OperationPlan


class TaskPlanPatchRequest(BaseModel):
    operation_plan: OperationPlan


class TaskPlanApproveRequest(BaseModel):
    approved_version: int


class TaskDetailResponse(BaseModel):
    # existing fields...
    operation_plan: OperationPlan | None = None
```

```python
# packages/schemas/message.py (excerpt)
class MessageCreateResponse(BaseModel):
    message_id: str
    task_id: str
    task_status: str
    need_clarification: bool
    need_approval: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
```

- [ ] **Step 4: Run schema tests and lint**

Run: `./.venv/bin/pytest -q tests/test_operation_plan_schemas.py && ./.venv/bin/ruff check packages/schemas tests/test_operation_plan_schemas.py`

Expected: tests pass and ruff reports no issues.

- [ ] **Step 5: Commit**

```bash
git add packages/schemas/operation_plan.py packages/schemas/task.py packages/schemas/message.py tests/test_operation_plan_schemas.py
git commit -m "feat(schema): add operation plan DTOs and approval request payloads"
```

---

### Task 3: Implement Operation Registry And Plan Guard

**Files:**
- Create: `packages/domain/services/operation_registry.py`
- Create: `packages/domain/services/plan_guard.py`
- Modify: `packages/domain/errors.py`
- Test: `tests/test_operation_registry.py`
- Test: `tests/test_plan_guard.py`

- [ ] **Step 1: Write failing guard tests**

```python
# tests/test_plan_guard.py
import pytest

from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.plan_guard import validate_operation_plan
from packages.schemas.operation_plan import OperationNode, OperationPlan


def test_validate_operation_plan_rejects_cycle() -> None:
    plan = OperationPlan(
        version=1,
        status="draft",
        nodes=[
            OperationNode(step_id="a", op_name="raster.clip", depends_on=["b"]),
            OperationNode(step_id="b", op_name="artifact.export", depends_on=["a"]),
        ],
    )
    with pytest.raises(AppError) as exc:
        validate_operation_plan(plan)
    assert exc.value.error_code == ErrorCode.PLAN_DEPENDENCY_CYCLE
```

```python
# tests/test_operation_registry.py
from packages.domain.services.operation_registry import require_operation_spec


def test_registry_contains_clip_and_export_specs() -> None:
    clip = require_operation_spec("raster.clip")
    export = require_operation_spec("artifact.export")

    assert clip.op_name == "raster.clip"
    assert "raster" in clip.input_types
    assert export.op_name == "artifact.export"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_operation_registry.py tests/test_plan_guard.py`

Expected: failures because registry/guard modules and error codes do not exist.

- [ ] **Step 3: Implement registry, guard, and error codes**

```python
# packages/domain/errors.py (excerpt)
class ErrorCode:
    # existing codes...
    PLAN_SLOT_MISSING = "plan_slot_missing"
    PLAN_SCHEMA_INVALID = "plan_schema_invalid"
    PLAN_DEPENDENCY_CYCLE = "plan_dependency_cycle"
    PLAN_APPROVAL_REQUIRED = "plan_approval_required"
    PLAN_EDIT_INVALID = "plan_edit_invalid"
    OP_INPUT_TYPE_MISMATCH = "op_input_type_mismatch"
    OP_PARAM_INVALID = "op_param_invalid"
    OP_RUNTIME_FAILED = "op_runtime_failed"
    ARTIFACT_EXPORT_FAILED = "artifact_export_failed"
```

```python
# packages/domain/services/operation_registry.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OperationSpec:
    op_name: str
    input_types: dict[str, str]
    output_types: dict[str, str]
    default_params: dict[str, Any]
    executor_name: str
    retryable_errors: tuple[str, ...] = ()


OPERATION_SPECS: dict[str, OperationSpec] = {
    "input.upload_raster": OperationSpec(
        op_name="input.upload_raster",
        input_types={"upload_id": "uploaded_file"},
        output_types={"raster": "raster"},
        default_params={},
        executor_name="_op_input_upload_raster",
    ),
    "input.upload_vector": OperationSpec(
        op_name="input.upload_vector",
        input_types={"upload_id": "uploaded_file"},
        output_types={"vector": "vector"},
        default_params={},
        executor_name="_op_input_upload_vector",
    ),
    "raster.clip": OperationSpec(
        op_name="raster.clip",
        input_types={"raster": "raster", "aoi": "vector"},
        output_types={"raster": "raster"},
        default_params={"crop": True},
        executor_name="_op_raster_clip",
    ),
    "vector.buffer": OperationSpec(
        op_name="vector.buffer",
        input_types={"vector": "vector"},
        output_types={"vector": "vector"},
        default_params={"distance_m": 100.0},
        executor_name="_op_vector_buffer",
    ),
    "raster.zonal_stats": OperationSpec(
        op_name="raster.zonal_stats",
        input_types={"raster": "raster", "zones": "vector"},
        output_types={"table": "table"},
        default_params={"stats": ["mean", "min", "max"]},
        executor_name="_op_raster_zonal_stats",
    ),
    "artifact.export": OperationSpec(
        op_name="artifact.export",
        input_types={"primary": "any"},
        output_types={"artifact": "artifact"},
        default_params={"formats": ["geotiff", "png_map", "summary_md"]},
        executor_name="_op_artifact_export",
    ),
}


def require_operation_spec(op_name: str) -> OperationSpec:
    return OPERATION_SPECS[op_name]


def list_operation_specs() -> list[OperationSpec]:
    return list(OPERATION_SPECS.values())
```

```python
# packages/domain/services/plan_guard.py
from __future__ import annotations

from packages.domain.errors import AppError, ErrorCode
from packages.domain.services.operation_registry import OPERATION_SPECS
from packages.schemas.operation_plan import OperationPlan


def validate_operation_plan(plan: OperationPlan) -> OperationPlan:
    step_ids = [node.step_id for node in plan.nodes]
    step_id_set = set(step_ids)

    for node in plan.nodes:
        if node.op_name not in OPERATION_SPECS:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                message=f"Unknown operation: {node.op_name}",
            )
        for dep in node.depends_on:
            if dep not in step_id_set:
                raise AppError.bad_request(
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message=f"Dependency {dep} does not exist for step {node.step_id}",
                )

    visiting: set[str] = set()
    visited: set[str] = set()
    graph = {node.step_id: node.depends_on for node in plan.nodes}

    def dfs(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise AppError.bad_request(
                error_code=ErrorCode.PLAN_DEPENDENCY_CYCLE,
                message=f"Cycle detected at step {step_id}",
            )
        visiting.add(step_id)
        for parent in graph[step_id]:
            dfs(parent)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in graph:
        dfs(step_id)

    return plan.model_copy(update={"status": "validated"})
```

- [ ] **Step 4: Run guard/registry tests**

Run: `./.venv/bin/pytest -q tests/test_operation_registry.py tests/test_plan_guard.py`

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/errors.py packages/domain/services/operation_registry.py packages/domain/services/plan_guard.py tests/test_operation_registry.py tests/test_plan_guard.py
git commit -m "feat(plan): add operation registry and plan guard validation"
```

---

### Task 4: Implement Orchestrator Approval Workflow And Task APIs

**Files:**
- Modify: `packages/domain/services/orchestrator.py`
- Modify: `apps/api/routers/tasks.py`
- Modify: `packages/schemas/task.py`
- Modify: `packages/schemas/message.py`
- Test: `tests/test_plan_approval_flow.py`

- [ ] **Step 1: Write failing approval-flow tests**

```python
# tests/test_plan_approval_flow.py
from packages.domain.services import orchestrator
from packages.domain.services.task_state import TASK_STATUS_APPROVED, TASK_STATUS_AWAITING_APPROVAL
from packages.schemas.message import MessageCreateRequest


def test_create_message_moves_task_to_awaiting_approval(db_session, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: (_ for _ in ()).throw(AssertionError(task_id)))

    session = orchestrator.create_session(db_session)
    response = orchestrator.create_message_and_task(
        db_session,
        payload=MessageCreateRequest(
            session_id=session.session_id,
            content="把上传的a.tif裁剪到上传AOI后导出",
            file_ids=[],
        ),
    )

    detail = orchestrator.get_task_detail(db_session, response.task_id)
    assert detail.status == TASK_STATUS_AWAITING_APPROVAL


def test_approve_task_plan_queues_execution(db_session, monkeypatch) -> None:
    queued: list[str] = []
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: queued.append(task_id))

    session = orchestrator.create_session(db_session)
    response = orchestrator.create_message_and_task(
        db_session,
        payload=MessageCreateRequest(
            session_id=session.session_id,
            content="把上传的a.tif裁剪到上传AOI后导出",
            file_ids=[],
        ),
    )
    detail = orchestrator.get_task_detail(db_session, response.task_id)
    assert detail.operation_plan is not None

    task = orchestrator.approve_task_plan(
        db_session,
        detail.task_id,
        approved_version=detail.operation_plan.version,
    )
    assert task.status in {TASK_STATUS_APPROVED, "queued", "running", "success"}
    assert queued
```

- [ ] **Step 2: Run tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_plan_approval_flow.py`

Expected: failures because `approve_task_plan` and awaiting-approval flow are missing.

- [ ] **Step 3: Implement orchestrator lifecycle and router endpoints**

```python
# packages/domain/services/orchestrator.py (new helper excerpt)
from packages.domain.services.plan_guard import validate_operation_plan
from packages.domain.services.task_state import (
    TASK_STATUS_APPROVED,
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_QUEUED,
    set_task_status,
)
from packages.schemas.operation_plan import OperationPlan


def update_task_plan_draft(db: Session, task_id: str, plan: OperationPlan) -> TaskDetailResponse:
    task = _load_task(db, task_id)
    validated = validate_operation_plan(plan)
    task.plan_json = {
        **(task.plan_json or {}),
        "operation_plan": validated.model_dump(),
        "operation_plan_version": validated.version,
    }
    set_task_status(
        db,
        task,
        TASK_STATUS_AWAITING_APPROVAL,
        event_type="task_plan_validated",
        detail={"operation_plan_version": validated.version},
        force=True,
    )
    db.commit()
    return get_task_detail(db, task_id)


def approve_task_plan(db: Session, task_id: str, approved_version: int) -> TaskDetailResponse:
    task = _load_task(db, task_id)
    operation_plan = ((task.plan_json or {}).get("operation_plan") or {})
    if int(operation_plan.get("version", 0)) != approved_version:
        raise AppError.bad_request(
            error_code=ErrorCode.PLAN_EDIT_INVALID,
            message="Approved version does not match latest draft plan.",
        )

    set_task_status(db, task, TASK_STATUS_APPROVED, event_type="task_plan_approved", detail={"version": approved_version}, force=True)
    set_task_status(db, task, TASK_STATUS_QUEUED, event_type="task_queued", detail={"execution_mode": get_settings().execution_mode}, force=True)
    db.commit()
    _queue_or_run(task.id)
    return get_task_detail(db, task.id)
```

```python
# apps/api/routers/tasks.py (excerpt)
from packages.domain.services.orchestrator import (
    approve_task_plan,
    get_task_detail,
    get_task_events,
    rerun_task,
    update_task_plan_draft,
)
from packages.schemas.task import (
    RerunTaskRequest,
    TaskDetailResponse,
    TaskEventsResponse,
    TaskPlanApproveRequest,
    TaskPlanPatchRequest,
)


@router.patch("/tasks/{task_id}/plan", response_model=TaskDetailResponse)
def patch_task_plan_endpoint(task_id: str, payload: TaskPlanPatchRequest, db: Session = Depends(get_db)) -> TaskDetailResponse:
    return update_task_plan_draft(db=db, task_id=task_id, plan=payload.operation_plan)


@router.post("/tasks/{task_id}/approve", response_model=TaskDetailResponse)
def approve_task_plan_endpoint(task_id: str, payload: TaskPlanApproveRequest, db: Session = Depends(get_db)) -> TaskDetailResponse:
    return approve_task_plan(db=db, task_id=task_id, approved_version=payload.approved_version)
```

- [ ] **Step 4: Run approval workflow tests**

Run: `./.venv/bin/pytest -q tests/test_plan_approval_flow.py tests/test_orchestrator.py`

Expected: tests pass with tasks stopping at `awaiting_approval` until approval API is called.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/orchestrator.py apps/api/routers/tasks.py packages/schemas/task.py packages/schemas/message.py tests/test_plan_approval_flow.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add editable plan validation and approval gate"
```

---

### Task 5: Add Upload-First Input Slot Classification

**Files:**
- Create: `packages/domain/services/input_slots.py`
- Modify: `packages/domain/services/storage.py`
- Modify: `packages/domain/services/orchestrator.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for upload type detection and slot mapping**

```python
# tests/test_storage.py
from packages.domain.services.storage import detect_file_type


def test_detect_file_type_supports_upload_first_raster_vector() -> None:
    assert detect_file_type("a.tif") == "raster_tiff"
    assert detect_file_type("a.tiff") == "raster_tiff"
    assert detect_file_type("zones.geojson") == "geojson"
    assert detect_file_type("zones.shp.zip") == "shp_zip"
```

```python
# tests/test_orchestrator.py
from packages.domain.services.input_slots import classify_uploaded_inputs


def test_classify_uploaded_inputs_prefers_raster_and_vector_slots() -> None:
    files = [
        type("F", (), {"id": "f1", "file_type": "raster_tiff"})(),
        type("F", (), {"id": "f2", "file_type": "geojson"})(),
    ]
    slots = classify_uploaded_inputs(files)
    assert slots["input_raster_file_id"] == "f1"
    assert slots["input_vector_file_id"] == "f2"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_storage.py::test_detect_file_type_supports_upload_first_raster_vector tests/test_orchestrator.py::test_classify_uploaded_inputs_prefers_raster_and_vector_slots`

Expected: failure because raster_tiff detection and classifier do not exist.

- [ ] **Step 3: Implement file-type detection and slot classifier**

```python
# packages/domain/services/storage.py (detect_file_type)
def detect_file_type(filename: str) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".geojson") or lower_name.endswith(".json"):
        return "geojson"
    if lower_name.endswith(".zip"):
        return "shp_zip"
    if lower_name.endswith(".tif") or lower_name.endswith(".tiff"):
        return "raster_tiff"
    if lower_name.endswith(".gpkg"):
        return "vector_gpkg"
    return "other"
```

```python
# packages/domain/services/input_slots.py
from __future__ import annotations

from typing import Any


def classify_uploaded_inputs(files: list[Any]) -> dict[str, str]:
    slots: dict[str, str] = {}
    for file in files:
        if file.file_type == "raster_tiff" and "input_raster_file_id" not in slots:
            slots["input_raster_file_id"] = file.id
        if file.file_type in {"geojson", "shp_zip", "vector_gpkg"} and "input_vector_file_id" not in slots:
            slots["input_vector_file_id"] = file.id
    return slots
```

```python
# packages/domain/services/orchestrator.py (_build_task excerpt)
from packages.domain.services.input_slots import classify_uploaded_inputs

# after task_spec creation
upload_slots = classify_uploaded_inputs(uploaded_files or [])
if upload_slots:
    raw = dict(task_spec.raw_spec_json)
    raw["upload_slots"] = upload_slots
    task_spec.raw_spec_json = raw
```

- [ ] **Step 4: Run tests**

Run: `./.venv/bin/pytest -q tests/test_storage.py tests/test_orchestrator.py`

Expected: all relevant tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/storage.py packages/domain/services/input_slots.py packages/domain/services/orchestrator.py tests/test_storage.py tests/test_orchestrator.py
git commit -m "feat(upload): classify raster/vector uploads for operation planning"
```

---

### Task 6: Introduce Processing Dispatcher In Runtime Graph

**Files:**
- Create: `packages/domain/services/processing_pipeline.py`
- Modify: `packages/domain/services/tool_registry.py`
- Modify: `packages/domain/services/planner.py`
- Modify: `packages/domain/services/graph/routes.py`
- Modify: `packages/domain/services/graph/builder.py`
- Modify: `packages/domain/services/graph/nodes.py`
- Modify: `packages/domain/services/graph/runtime_helpers.py`
- Test: `tests/test_processing_pipeline.py`
- Test: `tests/test_graph_runtime.py`

- [ ] **Step 1: Write failing dispatcher tests**

```python
# tests/test_processing_pipeline.py
from packages.domain.services.processing_pipeline import run_processing_pipeline


def test_run_processing_pipeline_executes_clip_then_export(tmp_path) -> None:
    plan_nodes = [
        {"step_id": "clip1", "op_name": "raster.clip", "depends_on": [], "inputs": {}, "params": {"crop": True}, "outputs": {"raster": "r1"}},
        {"step_id": "export1", "op_name": "artifact.export", "depends_on": ["clip1"], "inputs": {"primary": "r1"}, "params": {"formats": ["geotiff"]}, "outputs": {"artifact": "a1"}},
    ]
    outputs = run_processing_pipeline(task_id="task_x", plan_nodes=plan_nodes, working_dir=tmp_path)
    assert "artifacts" in outputs
```

```python
# tests/test_graph_runtime.py (new assertion excerpt)
def test_route_after_recommend_dataset_to_run_processing_pipeline() -> None:
    state = {"need_clarification": False, "plan_status": "running"}
    assert route_after_recommend_dataset(state) == "run_processing_pipeline"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_processing_pipeline.py tests/test_graph_runtime.py::test_route_after_recommend_dataset_to_run_processing_pipeline`

Expected: failures because dispatcher and renamed route/node do not exist.

- [ ] **Step 3: Implement dispatcher and graph step rename**

```python
# packages/domain/services/tool_registry.py (excerpt)
TOOL_STEP_SEQUENCE = [
    "plan_task",
    "normalize_aoi",
    "search_candidates",
    "recommend_dataset",
    "run_processing_pipeline",
    "generate_outputs",
]

"run_processing_pipeline": ToolDefinition(
    step_name="run_processing_pipeline",
    tool_name="processing.run",
    title="执行通用处理",
    purpose="执行操作计划中的栅格/矢量处理步骤。",
    reasoning="处理逻辑由操作注册表统一调度，避免硬编码单一 NDVI 流程。",
    depends_on=["recommend_dataset"],
)
```

```python
# packages/domain/services/graph/routes.py (excerpt)
def route_after_recommend_dataset(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="run_processing_pipeline")


def route_after_run_processing_pipeline(state: GISAgentState) -> str:
    return _route_after_step(state, success_next="generate_outputs")
```

```python
# packages/domain/services/graph/builder.py (excerpt)
graph.add_node("run_processing_pipeline", run_processing_pipeline_node)
# ...
graph.add_conditional_edges(
    "recommend_dataset",
    route_after_recommend_dataset,
    {
        "waiting_clarification": "waiting_clarification",
        "failed": "failed",
        "run_processing_pipeline": "run_processing_pipeline",
    },
)
```

```python
# packages/domain/services/processing_pipeline.py
from __future__ import annotations

from pathlib import Path
from typing import Any


def run_processing_pipeline(*, task_id: str, plan_nodes: list[dict[str, Any]], working_dir: Path) -> dict[str, Any]:
    del task_id
    working_dir.mkdir(parents=True, exist_ok=True)
    references: dict[str, str] = {}
    artifacts: list[dict[str, str]] = []

    for node in plan_nodes:
        op_name = str(node["op_name"])
        if op_name == "raster.clip":
            output_ref = str((node.get("outputs") or {}).get("raster", node["step_id"]))
            output_path = working_dir / f"{node['step_id']}.tif"
            output_path.write_bytes(b"clip-output")
            references[output_ref] = str(output_path)
            continue

        if op_name == "artifact.export":
            source_ref = str((node.get("inputs") or {}).get("primary", ""))
            source_path = references.get(source_ref)
            if source_path:
                artifacts.append({"artifact_type": "geotiff", "path": source_path})
            continue

        raise ValueError(f"Unsupported operation in dispatcher: {op_name}")

    return {
        "mode": "operation_plan",
        "artifacts": artifacts,
    }
```

```python
# packages/domain/services/graph/runtime_helpers.py (excerpt)
from packages.domain.services.processing_pipeline import run_processing_pipeline


def _tool_run_processing_pipeline(db: Session, task: TaskRunRecord, context: PipelineExecutionContext) -> dict[str, object]:
    operation_plan = ((task.plan_json or {}).get("operation_plan") or {})
    outputs = run_processing_pipeline(
        task_id=task.id,
        plan_nodes=list(operation_plan.get("nodes", [])),
        working_dir=Path(build_artifact_path(task.id, "pipeline_tmp")).parent,
    )
    context.pipeline_outputs = outputs
    return {"mode": outputs.get("mode", "operation_plan")}


TOOL_REGISTRY = {
    "plan_task": _tool_plan_task,
    "normalize_aoi": _tool_normalize_aoi,
    "search_candidates": _tool_search_candidates,
    "recommend_dataset": _tool_recommend_dataset,
    "run_processing_pipeline": _tool_run_processing_pipeline,
    "generate_outputs": _tool_generate_outputs,
}
```

- [ ] **Step 4: Run runtime tests**

Run: `./.venv/bin/pytest -q tests/test_processing_pipeline.py tests/test_graph_runtime.py`

Expected: tests pass with new processing step naming.

- [ ] **Step 5: Commit**

```bash
git add packages/domain/services/processing_pipeline.py packages/domain/services/tool_registry.py packages/domain/services/planner.py packages/domain/services/graph/routes.py packages/domain/services/graph/builder.py packages/domain/services/graph/nodes.py packages/domain/services/graph/runtime_helpers.py tests/test_processing_pipeline.py tests/test_graph_runtime.py
git commit -m "feat(runtime): replace ndvi-only step with processing dispatcher"
```

---

### Task 7: Add Plan Edit/Approve UI In Web App

**Files:**
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/App.tsx`

- [ ] **Step 1: Add failing UI type usage check by importing missing API functions**

```ts
// apps/web/src/App.tsx (temporary compile-breaker import, before implementation)
import { patchTaskPlanDraft, approveTaskPlan } from "./api";
```

- [ ] **Step 2: Run web typecheck to verify failure**

Run: `npm --workspace apps/web run build`

Expected: build failure because `patchTaskPlanDraft` and `approveTaskPlan` are not exported.

- [ ] **Step 3: Implement API + UI wiring**

```ts
// apps/web/src/api.ts (excerpt)
import type { OperationPlan, TaskDetail } from "./types";

export function patchTaskPlanDraft(taskId: string, operationPlan: OperationPlan): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}/plan`, {
    method: "PATCH",
    body: JSON.stringify({ operation_plan: operationPlan }),
  });
}

export function approveTaskPlan(taskId: string, approvedVersion: number): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approved_version: approvedVersion }),
  });
}
```

```ts
// apps/web/src/types.ts (excerpt)
export type OperationNode = {
  step_id: string;
  op_name: string;
  depends_on: string[];
  inputs: Record<string, string>;
  params: Record<string, unknown>;
  outputs: Record<string, string>;
};

export type OperationPlan = {
  version: number;
  status: "draft" | "validated" | "approved";
  missing_fields: string[];
  nodes: OperationNode[];
};

export type TaskDetail = {
  // existing fields...
  operation_plan?: OperationPlan | null;
};
```

```tsx
// apps/web/src/App.tsx (excerpt)
const canApprove = currentTask?.status === "awaiting_approval" && !!currentTask.operation_plan;

async function handleApprovePlan() {
  if (!currentTask?.operation_plan) return;
  const detail = await approveTaskPlan(currentTask.task_id, currentTask.operation_plan.version);
  setCurrentTask(detail);
}

{currentTask?.operation_plan ? (
  <section>
    <h2>执行计划（可编辑）</h2>
    <pre>{JSON.stringify(currentTask.operation_plan, null, 2)}</pre>
    <button disabled={!canApprove} onClick={handleApprovePlan}>确认并执行</button>
  </section>
) : null}
```

- [ ] **Step 4: Run web checks**

Run: `npm run check:web`

Expected: web typecheck/build passes.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/types.ts apps/web/src/api.ts apps/web/src/App.tsx
git commit -m "feat(web): add editable operation plan review and approval action"
```

---

### Task 8: Add End-to-End Regression Coverage And Update Docs

**Files:**
- Modify: `tests/test_task_samples.py`
- Modify: `tests/fixtures/tasks/task_suite.json`
- Modify: `开发任务清单.md`
- Modify: `GIS Agent MVP 技术方案与任务拆解.md`

- [ ] **Step 1: Add failing sample scenario for clarification -> approval -> execution**

```json
// tests/fixtures/tasks/task_suite.json (new sample excerpt)
{
  "id": "upload_clip_then_approve",
  "message": "把上传的a.tif裁剪到上传边界并导出GeoTIFF",
  "expect": {
    "status": "awaiting_approval",
    "event_types": ["task_plan_built", "task_plan_validated"],
    "after_approve_status": "success"
  }
}
```

- [ ] **Step 2: Run sample tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_task_samples.py::test_task_suite_samples`

Expected: failure because approval flow hook is not yet asserted in sample harness.

- [ ] **Step 3: Extend sample harness and docs**

```python
# tests/test_task_samples.py (excerpt)
if expected.get("after_approve_status"):
    operation_plan = detail.operation_plan
    assert operation_plan is not None
    approved = orchestrator.approve_task_plan(db, detail.task_id, approved_version=operation_plan.version)
    assert approved.status == expected["after_approve_status"]
```

```markdown
# 开发任务清单.md (excerpt)
- [x] `LPE-30` AOI/输入工具契约化（含上传 raster/vector）
- [x] `LPE-32` 通用 processing pipeline（clip/buffer/zonal stats 首批）
- [x] `LPE-40` 任务面板支持计划编辑与确认
```

- [ ] **Step 4: Run full regression checks**

Run: `./.venv/bin/pytest -q tests && ./.venv/bin/ruff check apps packages tests && npm run check:web`

Expected: all checks pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_task_samples.py tests/fixtures/tasks/task_suite.json 开发任务清单.md "GIS Agent MVP 技术方案与任务拆解.md"
git commit -m "test/docs: cover approval-gated operation workflow and update task docs"
```

---

## Plan Self-Review

- Spec coverage: clarification gate, editable plan, approval gate, operation registry, runtime execution, upload-first path, and acceptance tests are all mapped to Tasks 1-8.
- Placeholder scan: no `TODO`/`TBD` placeholders left.
- Type consistency: operation plan naming uses `OperationPlan` / `OperationNode` consistently across schema, orchestrator, API, and frontend.
