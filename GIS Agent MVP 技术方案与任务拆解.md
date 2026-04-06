# GIS Agent MVP 技术方案与任务拆解（后端优先版）

> 文档类型：技术方案 + 开发拆解  
> 对应产品文档：`GIS Agent MVP PRD.md`  
> 当前版本：v2.5（Manual Approval + Reject + ReAct FC）  
> 更新时间：2026-04-04

---

## 1. 技术目标

本版只关注后端，目标是把系统收敛成“人工审批驱动、逐步 ReAct 执行、可审计可追责”的 GIS Agent Demo：

1. 解析、规划、推荐由 LLM 输出结构化结果。  
2. 执行前必须人工审批，支持修改后审批和拒绝执行。  
3. 执行阶段每一步都采用 ReAct + Function Calling。  
4. 运行时由 LangGraph 统一驱动，状态机与事件流一致。  
5. LLM 调用日志与任务全链路绑定。

---

## 2. 架构总览（后端）

```text
API (FastAPI)
-> Orchestrator (Create Task + Approval Gate + Queue)
-> Worker (Celery)
-> LangGraph Runner (single runtime kernel)
   -> parse_task
   -> plan_task
   -> await_approval
   -> step_react_loop (per step)
   -> generate_outputs
   -> finalize
-> PostgreSQL/PostGIS (task/event/step/llm logs)
-> Object Storage (MinIO/S3/Local)
```

终态统一：`success` / `failed` / `waiting_clarification` / `cancelled`。

---

## 3. 目标态与当前态差距

### 3.1 目标态（本轮要达到）

1. 所有任务默认 `awaiting_approval`。  
2. 用户可批准、编辑后批准、拒绝执行。  
3. 执行中每个步骤都有 ReAct 决策和 Function Calling 记录。  
4. `OperationPlan` 非空且合法。  
5. 日志可按 `task_id` 查询 parse/plan/recommend/react_step 全链路。

### 3.2 当前主要差距（需补齐）

1. 任务默认执行策略与“审批必选”口径不一致。  
2. 缺少标准化拒绝执行接口与事件闭环。  
3. 当前步骤执行仍偏确定性调度，未形成严格 step-level ReAct loop。  
4. 空计划成功风险与白名单对齐仍需收口。  
5. LLM 日志 task 关联在 parser/planner 阶段不完整。

---

## 4. 关键设计决策

### 4.1 执行策略（强制审批）

1. 默认执行策略固定为 `manual_approval`。  
2. 创建任务后状态为 `awaiting_approval`。  
3. 只有批准后可入队执行。  
4. 拒绝后状态为 `cancelled`，流程结束。

### 4.2 审批与拒绝动作

新增审批决策动作：

1. `approve`：按当前版本执行。  
2. `edit_and_approve`：提交新版本后执行。  
3. `reject`：拒绝执行并写入原因。

事件要求：

1. `task_plan_approved`  
2. `task_plan_rejected`  
3. `task_cancelled`

### 4.3 步骤执行模型（ReAct + Function Calling）

对每个计划步骤执行统一 loop：

1. 读取步骤上下文（目标、依赖产物、历史 observation、预算）。  
2. LLM 输出结构化决策：`continue/skip/fail` + `function_call`。  
3. 后端执行 function call（仅白名单工具）。  
4. 写 observation 并回填到该步骤上下文。  
5. 达到终止条件后结束该步骤，进入下一步。

边界保护：

1. 单步最大 ReAct 回合数。  
2. 单步超时。  
3. 工具调用失败重试预算。  
4. 非法 function call 直接失败并记录错误码。

### 4.4 Operation Plan 真实性约束

1. `nodes` 不能为空。  
2. 依赖图可拓扑排序。  
3. `op_name` 必须来自统一 operation registry。  
4. 校验失败不可进入执行态。

### 4.5 审计闭环

1. 步骤事件：`tool_execution_started/completed/failed`。  
2. ReAct 事件：`step_react_decision`、`step_react_observation`。  
3. 决策事件：`task_plan_approved/rejected`。  
4. 失败字段：`failed_step/error_code/error_message`。  
5. LLM 日志：`parse/plan/recommend/react_step` 全量绑定 `task_id`。

---

## 5. 模块级实现方案

| 模块 | 改造要点 | 关键文件 |
| --- | --- | --- |
| Orchestrator | 任务创建默认审批态；支持 approve/reject；拒绝后不可执行 | `packages/domain/services/orchestrator.py` |
| Task APIs | 新增 reject/cancel 接口；审批契约收口 | `apps/api/routers/tasks.py` `packages/schemas/task.py` |
| ReAct Step Agent | 新增每步 ReAct 决策与函数调用逻辑 | `packages/domain/services/graph/nodes.py` `packages/domain/services/graph/runtime_helpers.py` |
| Tool Calling | 函数调用白名单校验与执行 | `packages/domain/services/tool_registry.py` |
| Plan Guard | 非空节点、依赖合法、op_name 合法 | `packages/domain/services/plan_guard.py` |
| Operation Schema | `AllowedOperationName` 与 operation registry 对齐 | `packages/schemas/operation_plan.py` `packages/domain/services/operation_registry.py` |
| LLM Logs | parse/plan/recommend/react_step task 级关联 | `packages/domain/services/llm_client.py` `packages/domain/services/llm_call_logs.py` |
| Error Model | 增补 reject/react/function-call 相关错误码 | `packages/domain/errors.py` |

---

## 6. API 与契约（后端）

### 6.1 任务创建

`POST /api/v1/messages`：

1. 创建任务后返回 `awaiting_approval`。  
2. 响应包含 `need_approval=true`。  
3. 返回可编辑 `operation_plan`。

### 6.2 计划编辑、批准、拒绝

1. `PATCH /api/v1/tasks/{task_id}/plan`：更新草稿计划。  
2. `POST /api/v1/tasks/{task_id}/approve`：批准并入队。  
3. `POST /api/v1/tasks/{task_id}/reject`：拒绝并取消任务。  

`reject` 请求体建议：

1. `reason: str`（可选但推荐）。  
2. `operator: str`（可选，Demo 可空）。

### 6.3 任务详情与事件

1. `GET /api/v1/tasks/{task_id}`：返回计划、步骤、状态、错误、拒绝信息。  
2. `GET /api/v1/tasks/{task_id}/events`：返回审批、ReAct、工具执行、失败事件。

---

## 7. 数据模型调整建议

### 7.1 `task_runs`

新增或强化字段：

1. `status` 增加 `cancelled`。  
2. `rejected_reason`（可选）。  
3. `rejected_at`（可选）。  
4. `fallback_used`、`error_code`、`error_message`。

### 7.2 `task_steps`

1. `detail_json` 存储 ReAct 决策摘要与 observation。  
2. 可选增加 `react_turn_count`。

### 7.3 `llm_call_logs`

1. `phase` 扩展为 `parse/plan/recommend/react_step`。  
2. 所有 phase 必须能关联 `task_id`。

---

## 8. Guardrails 与错误语义

### 8.1 运行保护

1. 全局：`agent_max_steps`、`agent_max_tool_calls`、`agent_runtime_timeout_seconds`。  
2. 单步：`max_react_turns_per_step`、`react_step_timeout_seconds`。

### 8.2 错误语义（关键）

1. 未审批执行：`plan_approval_required`。  
2. 用户拒绝：`task_cancelled_by_user`（或等价错误语义）。  
3. 空计划：`plan_schema_invalid`（或专用 `plan_nodes_empty`）。  
4. 非法 function call：`task_runtime_unknown_tool`。  
5. ReAct 超预算：`task_runtime_max_tool_calls_exceeded` / `task_runtime_timeout`。

---

## 9. 开发阶段拆解（后端）

### Phase A：审批必选与拒绝路径（P0）

1. 创建任务默认 `awaiting_approval`。  
2. 增加 reject/cancel 接口与状态流。  
3. 审批/拒绝事件回写与详情展示字段打通。

### Phase B：Step ReAct + Function Calling（P0）

1. 新增 step-level ReAct loop。  
2. 接入函数调用白名单执行。  
3. 写入 ReAct 决策与 observation 事件。

### Phase C：计划真实性收口（P0）

1. 非空 `operation_plan` 约束。  
2. schema 白名单与 registry 对齐。  
3. 去掉空计划成功路径。

### Phase D：审计与回归（P0）

1. parse/plan/recommend/react_step 日志 task 关联。  
2. 增补审批拒绝与 ReAct 失败路径测试。  
3. 核心回归全绿。

---

## 10. 验收清单（技术）

1. 新任务默认进入 `awaiting_approval`。  
2. 批准前不执行；批准后执行。  
3. 拒绝后状态 `cancelled` 且不可执行。  
4. 每一步都有 ReAct 决策与 Function Calling 记录。  
5. 空 `operation_plan` 无法成功。  
6. LLM 日志可按 task_id 拉取 parse/plan/recommend/react_step 全链路。  
7. 后端 CI（ruff + pytest + compile）通过。

