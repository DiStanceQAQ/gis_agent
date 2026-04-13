# GIS Agent 架构优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 GIS Agent 从“功能堆叠期”收敛为“边界清晰、审计闭环、可持续扩展”的架构状态。

**Architecture:** 核心策略是 (1) 拆分 orchestrator God Module 为 5 个职责单一模块 (2) 补齐 operation-node 级审计事件 (3) 收敛 state source-of-truth 减少 force=True (4) 对完整 plan 写入路径增加 schema 校验 (5) 文档化 ReAct 实际语义。

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy, LangGraph, Pydantic, Celery, PostgreSQL/PostGIS

---

## 关键执行守则（必须遵守）

1. **禁止循环依赖**
   - 新抽取模块 **不得** import `packages.domain.services.orchestrator`
   - `orchestrator.py` 只作为 facade / compatibility layer，允许它 import 子模块，但禁止子模块反向 import orchestrator
   - 若两个新模块需要共享 helper，抽到更底层模块，不要互相 import

2. **完整写入 vs 局部写入区分**
   - 只有“完整构造/替换 plan_json”的路径使用 schema 校验 helper
   - `set_task_plan_status()` / `set_task_plan_step_status()` 这类 status-only helper 继续使用，不强行接入全量 schema 校验

3. **Phase 2 不并行执行**
   - operation-node 审计增强在 orchestrator 拆分稳定后再做，避免两条重构线交叉增加回归面

4. **每完成一个抽取模块就验证 import 图**
   - 运行目标测试 + `lsp_diagnostics` + 最少一次模块 import smoke check

---

## 文件结构变更总览

### 新建文件

| 文件路径 | 职责 |
|---|---|
| `packages/domain/services/message_handler.py` | 消息接入、intent routing辅助、confirmation flow、chat fallback 辅助 |
| `packages/domain/services/task_lifecycle.py` | 任务创建、审批、拒绝、重跑、task detail 查询 |
| `packages/domain/services/session_queries.py` | session CRUD、message listing、memory 查询 |
| `packages/domain/services/upload_handler.py` | 文件上传、upload context 注入 operation plan |
| `packages/domain/services/revision_handler.py` | revision 创建、correction、activation 编排 |
| `packages/domain/services/plan_json_helpers.py` | plan_json 的完整写入/更新校验 helper |

### 修改文件

| 文件路径 | 变更内容 |
|---|---|
| `packages/domain/services/orchestrator.py` | 瘦身为 facade，只保留 `create_message()` 主入口和兼容导出 |
| `packages/domain/services/processing_pipeline.py` | 添加 per-operation-node 事件回调 |
| `packages/domain/services/graph/runtime_helpers.py` | 消费 pipeline 事件回调，写 operation_node 级 task events |
| `packages/domain/services/graph/nodes.py` | 减少 `force=True`，只保留 finalize/revision 例外 |
| `packages/domain/services/task_state.py` | 扩展状态转换表，覆盖合法 reentry 场景 |
| `apps/api/routers/tasks.py` | import 路径从 orchestrator 改为新模块 |
| `apps/api/routers/messages.py` | import 路径从 orchestrator 改为新模块 |
| `apps/api/routers/sessions.py` | import 路径从 orchestrator 改为新模块 |
| `apps/api/routers/files.py` | import 路径从 orchestrator 改为新模块 |
| `README.md` | 补充 ReAct 实际语义与审计粒度 |

---

## Phase 1: 拆分 orchestrator.py（最高优先级）

### Task 1.1: 抽取 session_queries.py

**Files:**
- Create: `packages/domain/services/session_queries.py`
- Modify: `packages/domain/services/orchestrator.py`
- Modify: `apps/api/routers/sessions.py`

提取函数：
- `create_session()`
- `list_session_tasks()`
- `list_session_messages()`
- `get_session_memory()`
- `_serialize_session_memory_snapshot()`
- `_serialize_session_memory_summary()`
- `_excerpt()`
- `_load_latest_session_memory_anchor()`

- [ ] 创建 `session_queries.py`，搬入上述函数
- [ ] 在 orchestrator.py 中删除这些函数，添加兼容导出
- [ ] 更新 router import
- [ ] 运行：`pytest tests/test_session_memory.py -x -q`
- [ ] 运行：`python -c "from packages.domain.services.session_queries import get_session_memory"`
- [ ] Commit: `refactor: extract session queries from orchestrator`

---

### Task 1.2: 抽取 upload_handler.py

**Files:**
- Create: `packages/domain/services/upload_handler.py`
- Modify: `packages/domain/services/orchestrator.py`
- Modify: `apps/api/routers/files.py`

提取：
- `create_uploaded_file()`
- `_load_uploaded_files_by_payload_order()`
- `_resolve_uploaded_storage_path()`
- `_select_uploaded_file()`
- `_inject_upload_context_into_operation_plan()`
- `_load_recent_session_uploads()`
- `_load_recent_session_file_ids()`
- `_resolve_chat_upload_files()`
- `_serialize_uploaded_files_for_chat()`
- `_build_upload_access_reply()`
- `_is_upload_access_question()`
- `_is_upload_status_followup()`
- `_history_mentions_upload_context()`
- 常量: `UPLOAD_REQUEST_HINT_PATTERN`, `UPLOAD_STATUS_FOLLOWUP_PATTERN`, `UPLOAD_ACCESS_QUESTION_PATTERN`

- [ ] 创建 `upload_handler.py`，搬入上述函数和常量
- [ ] 在 orchestrator.py 中删除这些函数，添加兼容导出
- [ ] 更新 router import
- [ ] 运行：`pytest tests/ -k "upload or file" -x -q`
- [ ] 运行：`python -c "from packages.domain.services.upload_handler import create_uploaded_file"`
- [ ] Commit: `refactor: extract upload handling from orchestrator`

---

### Task 1.3: 先抽取 revision_handler.py（提前，避免 message/task 循环）

**Files:**
- Create: `packages/domain/services/revision_handler.py`
- Modify: `packages/domain/services/orchestrator.py`

提取：
- `_apply_correction_revision()`
- `_build_revision_override_payload()`
- `_build_initial_revision_understanding()`

- [ ] 创建 `revision_handler.py`，搬入上述函数
- [ ] 保证 `revision_handler.py` 不 import orchestrator
- [ ] 在 orchestrator.py 中删除这些函数，添加兼容导出
- [ ] 运行：`pytest tests/test_task_revisions.py tests/test_orchestrator.py -x -q`
- [ ] 运行：`python -c "from packages.domain.services.revision_handler import _apply_correction_revision"`
- [ ] Commit: `refactor: extract revision handling from orchestrator`

---

### Task 1.4: 抽取 task_lifecycle.py

**Files:**
- Create: `packages/domain/services/task_lifecycle.py`
- Modify: `packages/domain/services/orchestrator.py`
- Modify: `apps/api/routers/tasks.py`

提取：
- `_build_task()`
- `_build_initial_operation_plan()`
- `_load_latest_session_task()`
- `_merge_task_spec()`
- `_resolve_followup_context()`
- `create_message_and_task()`
- `_load_task()`
- `get_task_detail()`
- `update_task_plan_draft()`
- `approve_task_plan()`
- `reject_task_plan()`
- `get_task_events()`
- `should_inherit_original_aoi()`
- `rerun_task()`
- `_queue_or_run()`
- `_validate_operation_plan_for_edit()`
- `_parse_iso_datetime()`
- `_build_revision_summary()`
- `_build_task_spec_raw_payload()`
- `_require_named_aoi_clarification()`

依赖规则：
- 允许 `task_lifecycle` import `upload_handler` 和 `revision_handler`
- 禁止 `task_lifecycle` import `message_handler`
- 禁止 `task_lifecycle` import `orchestrator`

- [ ] 创建 `task_lifecycle.py`，搬入上述函数
- [ ] 更新 router import
- [ ] 在 orchestrator.py 中删除这些函数，添加兼容导出
- [ ] 运行：`pytest tests/test_orchestrator.py tests/test_task_approval.py tests/test_task_rerun.py -x -q`
- [ ] 运行：`python -c "from packages.domain.services.task_lifecycle import approve_task_plan"`
- [ ] Commit: `refactor: extract task lifecycle from orchestrator`

---

### Task 1.5: 抽取 message_handler.py

**Files:**
- Create: `packages/domain/services/message_handler.py`
- Modify: `packages/domain/services/orchestrator.py`

提取：
- `_persist_message()`
- `_load_message_history()`
- `_create_chat_mode_response()`
- `_understanding_pipeline_enabled()`
- `_serialize_understanding_response()`
- `_attach_understanding_metadata()`
- `_persist_understanding_metadata()`
- `_finalize_response_with_understanding()`
- `_parse_candidate_request_content()`
- `_is_executable_followup_request()`
- `_load_latest_confirmation_candidate_message()`
- `_resolve_confirmation_source_context()`
- `_load_active_confirmation_prompt_message()`
- `_find_latest_executable_request_message()`
- `_build_confirmation_understanding()`
- `_build_confirmation_response_decision()`
- 常量: `TASK_CONFIRMATION_PROMPT`, `TASK_CONFIRMATION_MISSING_CONTEXT_PROMPT`, `TASK_CONFIRMATION_REQUIRES_REVISION_PROMPT`
- dataclass: `_ParsedTaskUnderstanding`, `_ConfirmationSourceContext`

依赖规则：
- 允许 `message_handler` import `task_lifecycle` 的公开函数（如 `create_message_and_task`）
- 禁止 `task_lifecycle` 反向 import `message_handler`
- 禁止 `message_handler` import `orchestrator`

- [ ] 创建 `message_handler.py`，搬入上述函数
- [ ] 在 orchestrator.py 中删除这些函数，添加兼容导出
- [ ] 运行：`pytest tests/test_orchestrator.py tests/test_understanding.py tests/test_response_policy.py -x -q`
- [ ] 运行：`python -c "from packages.domain.services.message_handler import _persist_message"`
- [ ] Commit: `refactor: extract message handling from orchestrator`

---

### Task 1.6: 瘦身 orchestrator.py 为 facade

**Files:**
- Modify: `packages/domain/services/orchestrator.py`

目标：
- orchestrator.py 仅保留 `create_message()` 主入口（必要时）和兼容导出
- 所有 API router 改为直接依赖拆分后的子模块，而非 orchestrator

- [ ] 清理 orchestrator.py，使其行数显著下降
- [ ] 检查全仓库 `orchestrator` import：确认无子模块反向依赖
- [ ] 运行：`pytest tests/ -x -q`
- [ ] 运行：`python -c "from packages.domain.services.orchestrator import create_message"`
- [ ] Commit: `refactor: slim orchestrator to facade with re-exports`

---

## Phase 2: Operation-Node 级审计事件（在 Phase 1 完成后执行）

### Task 2.1: 为 processing_pipeline 添加事件回调

**Files:**
- Modify: `packages/domain/services/processing_pipeline.py`

- [ ] 为 `run_processing_pipeline()` 添加可选 `on_event` 回调参数
- [ ] 在每个 operation node 执行前发出 `operation_node_started`
- [ ] 在成功后发出 `operation_node_completed`
- [ ] 在失败路径发出 `operation_node_failed`
- [ ] 运行：`pytest tests/test_processing_pipeline.py -x -q`
- [ ] Commit: `feat: add per-operation-node event callback to processing pipeline`

---

### Task 2.2: runtime_helpers 消费 pipeline 事件

**Files:**
- Modify: `packages/domain/services/graph/runtime_helpers.py`

- [ ] 在 `_tool_run_processing_pipeline()` 中注入 callback
- [ ] callback 将 operation-node 事件写入 `append_task_event()`
- [ ] detail 最少包含 `step_id`, `op_name`, `status`
- [ ] 运行：`pytest tests/test_graph_runner.py tests/test_processing_pipeline.py -x -q`
- [ ] 若前端/事件消费测试存在，补跑相关测试
- [ ] Commit: `feat: wire pipeline operation events into task audit trail`

---

## Phase 3: 收敛 force=True

### Task 3.1: 扩展状态转换表

**Files:**
- Modify: `packages/domain/services/task_state.py`

- [ ] 扩展 `TASK_STATUS_TRANSITIONS`，覆盖合法 reentry 场景：
  - `waiting_clarification -> running`
  - `awaiting_approval -> waiting_clarification`
  - `queued -> awaiting_approval`
  - `running -> waiting_clarification`
- [ ] 运行：`pytest tests/test_task_state.py -x -q`
- [ ] Commit: `fix: expand task status transitions to cover legitimate reentry paths`

---

### Task 3.2: 逐步移除非必要 force=True

**Files:**
- Modify: `packages/domain/services/graph/nodes.py`
- Modify: `packages/domain/services/graph/runtime_helpers.py`
- Modify: `packages/domain/services/revision_handler.py`

规则：
- 优先移除 runtime 正常路径上的 `force=True`
- 保留 finalize nodes / revision activation 的 `force=True`
- 对保留项添加注释说明其幂等或恢复语义

- [ ] 将 `_prepare_runtime_start()` 中可移除的 `force=True` 改为普通转换
- [ ] 将 runtime success/fail 正常路径上的 `force=True` 改为普通转换
- [ ] 给保留项添加注释
- [ ] 运行：`pytest tests/ -x -q`
- [ ] Commit: `fix: reduce force=True usage, document remaining cases`

---

## Phase 4: plan_json 完整写入校验

### Task 4.1: 添加 plan_json write helper（仅用于完整写入路径）

**Files:**
- Create: `packages/domain/services/plan_json_helpers.py`
- Modify: `packages/domain/services/graph/nodes.py`
- Modify: `packages/domain/services/task_lifecycle.py`

范围说明：
- 仅替换这些“完整 plan 对象写入”路径：
  - `_build_task()`
  - `approve_task_plan()`
  - `update_task_plan_draft()`
  - `plan_task_node()` 中构造完整 plan 的位置
- **不**替换：`set_task_plan_status()` / `set_task_plan_step_status()` 这类局部状态更新 helper

- [ ] 创建 `plan_json_helpers.py`
- [ ] 在完整 plan 写入路径使用 helper
- [ ] 保留 status-only helper 原样
- [ ] 运行：`pytest tests/ -x -q`
- [ ] Commit: `feat: add validated plan_json write helpers for full-plan writes`

---

## Phase 5: 文档化 ReAct 实际语义

### Task 5.1: 更新 README

**Files:**
- Modify: `README.md`

- [ ] 补充当前 ReAct 实现说明：固定图 + per-step go/no-go gate
- [ ] 明确说明 operation plan 在 `run_processing_pipeline` 内批量执行
- [ ] 补充审计粒度：LangGraph step 级 + operation-node 级
- [ ] Commit: `docs: document actual ReAct semantics and audit granularity`

---

## 执行顺序（修正版）

```text
Phase 1（严格串行）:
  1.1 session_queries
  1.2 upload_handler
  1.3 revision_handler
  1.4 task_lifecycle
  1.5 message_handler
  1.6 orchestrator facade

Phase 2（在 Phase 1 完成后）:
  2.1 pipeline callback
  2.2 wire events

Phase 3（在 Phase 2 完成后更安全）:
  3.1 expand transitions
  3.2 reduce force=True

Phase 4:
  4.1 plan_json helpers

Phase 5:
  5.1 docs
```

---

## 风险与回退策略

| 风险 | 缓解措施 |
|---|---|
| 新模块之间循环 import | 强制单向依赖规则；每步做 import smoke check |
| router 或测试仍依赖 orchestrator 私有 helper | Phase 1.6 前做全仓 import 搜索并修正 |
| pipeline 事件写库过于频繁 | 先实现 correctness；如有性能问题后续再 batch/采样 |
| plan_json helper 误接入局部写入路径 | 严格限制 helper 使用范围，只用于完整 plan 写入 |
| 状态转换调整导致隐藏回归 | 先扩展 transitions，再逐步减少 force=True |

---

## 验收标准

- [ ] orchestrator.py 显著瘦身（目标 < 500 行，理想 < 400 行）
- [ ] 所有现有测试通过
- [ ] processing pipeline 内每个 operation node 有独立 event
- [ ] `force=True` 使用收敛到少数合理例外
- [ ] 完整 plan 写入经过 schema 校验
- [ ] README 准确描述 ReAct 语义与审计粒度
