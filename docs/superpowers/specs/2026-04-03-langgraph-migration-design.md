# GIS Agent LangGraph 迁移设计（后端主链）

> 文档类型：设计规格（Brainstorming Spec）  
> 日期：2026-04-03  
> 状态：Approved (Design)  
> 适用范围：后端编排主链（Parser / Planner / Runtime / Events / Logs）

---

## 1. 背景与目标

当前工程已经具备 LLM Parser、LLM Planner、LLM Recommendation、任务状态机和工具执行循环，但执行编排仍是自研 runtime 循环。  
本次设计目标是把后端执行编排统一迁移到 **LangGraph StateGraph**，作为唯一编排内核。

本次目标：

1. 以 LangGraph 统一任务执行图，不再维护自研 runtime 主循环。  
2. 保持现有任务 API 契约，允许向后兼容新增字段。  
3. 保留现有 parser/planner/recommendation 的结构化能力与错误码体系。  
4. 新增并落库 LLM 调用日志，形成可追踪主链。  

---

## 2. 已确认设计决策

1. **架构决策**：最终迁移到 LangGraph，作为唯一编排实现。  
2. **过渡策略**：不做长期双运行时；迁移完成后只保留 LangGraph 路径。  
3. **模式决策**：采用严格模式，parser/planner 默认不走 legacy fallback。  
4. **数据演进**：接受数据库迁移，新增 `llm_call_logs`。  
5. **API 兼容**：允许新增任务详情字段，不做破坏性改名或删字段。  

---

## 3. 目标架构

```text
Web GIS Workspace (React + OpenLayers)
-> FastAPI (Session / Task / Artifact APIs)
-> Orchestrator (create task + enqueue)
-> Celery Worker
-> LangGraph Runner (single runtime kernel)
   -> parse_request node (LLM parser)
   -> plan_task node (LLM planner)
   -> normalize_aoi node
   -> search_candidates node
   -> recommend_dataset node (LLM recommendation)
   -> run_processing node
   -> publish_artifacts node
   -> finalize node
-> PostgreSQL + PostGIS
-> MinIO/S3
```

关键原则：

1. `orchestrator` 负责任务创建与入队，不承担执行编排。  
2. 节点函数只做单一职责，状态流转由图控制。  
3. 节点执行可观测事件和任务步骤写入策略统一。  

---

## 4. Graph State 设计

建议定义统一 `GISAgentState`（TypedDict 或 Pydantic 均可，推荐 TypedDict + 最小校验）：

1. `task_id`, `session_id`, `user_message`  
2. `parsed_spec`（结构化解析结果）  
3. `task_plan`（结构化计划）  
4. `context`（aoi/candidates/recommendation/pipeline_outputs）  
5. `control`（current_step/retry_count/tool_calls）  
6. `error`（error_code/error_message/error_detail）  

约束：

1. 节点仅修改自己负责的 state 字段。  
2. 不在 state 内传递 SQLAlchemy 对象，避免隐式引用。  
3. 进入终态前必须把 `task_runs` 关键状态落库。  

---

## 5. 图节点与条件分支

推荐主图：

1. `parse_request`
2. `plan_task`
3. `normalize_aoi`
4. `search_candidates`
5. `recommend_dataset`
6. `run_processing`
7. `publish_artifacts`
8. `finalize`

条件边：

1. `parse_request` 后：  
   - `need_confirmation=true` -> `waiting_clarification` 终态  
   - 否则 -> `plan_task`
2. `plan_task` 后：  
   - 缺字段或计划失败 -> `waiting_clarification`  
   - 否则 -> `normalize_aoi`
3. 任意节点异常 -> `failed` 终态  
4. 全部节点完成 -> `success` 终态  

---

## 6. 错误处理与状态机细则

统一终态：

1. `success`
2. `failed`
3. `waiting_clarification`

错误分层：

1. 可恢复错误：有限重试（例如网络超时、schema repair）。  
2. 业务错误：unknown tool、非法状态转换、超步数/超时，直接失败。  
3. 信息不足：进入 `waiting_clarification`，不是 `failed`。  

写入规范：

1. 节点开始写 `tool_execution_started`。  
2. 节点成功写 `tool_execution_completed`。  
3. 失败路径写 `task_failed` 与失败快照。  
4. `task_steps` 状态遵循 `pending -> running -> success/failed/skipped`。  

运行保护：

1. `agent_max_steps`  
2. `agent_max_tool_calls`  
3. `agent_runtime_timeout_seconds`  

---

## 7. 数据与可观测设计

### 7.1 `llm_call_logs` 新增

新增模型与迁移，字段建议：

1. `id`（主键）
2. `task_id`（可空，便于独立调试调用）
3. `phase`（parse/plan/recommend）
4. `model_name`
5. `request_id`
6. `prompt_hash`
7. `input_tokens`, `output_tokens`, `total_tokens`
8. `latency_ms`
9. `status`（success/failed）
10. `error_message`
11. `created_at`

### 7.2 日志写入策略

1. parser/planner/recommendation 的 LLM 调用统一走包装器。  
2. 无论成功失败都写一条日志。  
3. 日志写入失败不能阻断任务主链（记录 warning 并继续）。  

---

## 8. 文件级迁移拆分

新增：

1. `packages/domain/services/graph/state.py`
2. `packages/domain/services/graph/builder.py`
3. `packages/domain/services/graph/runner.py`
4. `packages/domain/services/graph/nodes/*.py`

改造：

1. `apps/worker/tasks.py`：执行入口改为 graph runner。  
2. `packages/domain/services/orchestrator.py`：保持任务创建与入队，不承载 runtime loop。  
3. `packages/domain/services/llm_client.py`：接入统一调用日志写入。  
4. `packages/domain/models.py` + `infra/migrations/versions/*`：新增 `llm_call_logs`。  

清理：

1. 下线 `packages/domain/services/agent_runtime.py` 的主循环路径。  
2. 可复用函数迁移到 `graph/nodes` 或公用 service 模块。  

---

## 9. 验收标准

1. Worker 仅通过 LangGraph 执行任务。  
2. 成功/失败/澄清三类终态稳定可复现。  
3. `task_steps` 与 `task_events` 顺序和状态一致。  
4. parser/planner/recommendation 的每次 LLM 调用可在 `llm_call_logs` 查询。  
5. 样例回归覆盖成功、失败、澄清、fallback、rerun 全通过。  

---

## 10. 非目标

1. 本轮不做前端大改。  
2. 本轮不引入额外 Agent 框架并存。  
3. 本轮不做与 LangGraph 无关的大规模重构。  

