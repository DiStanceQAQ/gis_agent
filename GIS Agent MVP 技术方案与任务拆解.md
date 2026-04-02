# GIS Agent MVP 技术方案与任务拆解

> 文档类型：技术方案 + 开发拆解  
> 对应产品文档：`GIS Agent MVP PRD.md`  
> 当前版本：v2.2（LangGraph 迁移版）  
> 更新时间：2026-04-03

---

## 1. 技术目标

本版技术目标是把现有工程升级为“LLM 主导 Plan-and-Execute”的 GIS Agent，并将后端编排统一迁移到 LangGraph：

1. 任务解析、规划、推荐由 LLM 生成结构化结果。  
2. 执行由 LangGraph 状态图驱动，支持通用遥感/地理处理任务。  
3. 全链路可观测：计划、步骤、事件、产物、错误、LLM 调用元数据。  
4. 前端工作台完整呈现执行过程与结果，而非聊天气泡体验。

---

## 2. 架构总览

### 2.1 逻辑分层

```text
Web GIS Workspace (React + OpenLayers)
-> FastAPI (Session / Task / Artifact APIs)
-> Orchestrator (create task + enqueue)
-> Celery Worker
-> LangGraph Runner (single runtime kernel)
   -> parse_request
   -> plan_task
   -> normalize_aoi
   -> search_candidates
   -> recommend_dataset
   -> run_processing
   -> publish_artifacts
   -> finalize
-> PostgreSQL + PostGIS
-> MinIO/S3
```

### 2.2 执行图（StateGraph）

一次任务内主流程：

1. `parse_request`：自然语言 -> `ParsedTaskSpec`。  
2. `plan_task`：结构化任务 -> `TaskPlan`。  
3. `normalize_aoi`：标准化研究区。  
4. `search_candidates`：检索目录候选并做 QC 摘要。  
5. `recommend_dataset`：输出主备推荐与理由。  
6. `run_processing`：执行栅格/矢量处理。  
7. `publish_artifacts`：发布 GeoTIFF、PNG、方法说明、摘要。  
8. `finalize`：写入终态与任务收尾信息。

终态统一为：`success / failed / waiting_clarification`。

---

## 3. 模块拆解

| 模块 | 主要职责 | 关键文件 |
| --- | --- | --- |
| API Router | 会话、消息、任务、事件、产物接口 | `apps/api/routers/*.py` |
| Orchestrator | 任务创建、上下文继承、入队 | `packages/domain/services/orchestrator.py` |
| LLM Client | 统一模型调用、重试、超时、日志 | `packages/domain/services/llm_client.py` |
| Parser | LLM 解析输入为 `ParsedTaskSpec` | `packages/domain/services/parser.py` |
| Planner | LLM 生成 `TaskPlan` | `packages/domain/services/planner.py` |
| Graph Runtime | LangGraph 构图、节点运行与状态推进 | `packages/domain/services/graph/*` |
| Tool Registry | 工具白名单与契约 | `packages/domain/services/tool_registry.py` |
| AOI Service | AOI 解析、标准化、校验 | `packages/domain/services/aoi.py` |
| Catalog Service | STAC 检索与候选摘要 | `packages/domain/services/catalog.py` |
| Recommendation | LLM 推荐主备方案与理由 | `packages/domain/services/recommendation.py` |
| Geo Processing Engine | 通用地理处理（NDVI/NDWI/波段运算/滤波/坡度坡向/缓冲区） | `packages/domain/services/processing_pipeline.py`（可由现有 `ndvi_pipeline.py` 演进） |
| Artifact Service | 产物存储、下载与元数据 | `packages/domain/services/storage.py` |
| Task Event Service | 事件追加与查询 | `packages/domain/services/task_events.py` |

---

## 4. 接口设计（P0）

### 4.1 会话与任务

1. `POST /api/v1/sessions`  
用途：创建会话。

2. `GET /api/v1/sessions/{session_id}/tasks`  
用途：列出会话下最近任务。

3. `POST /api/v1/messages`  
用途：提交需求，创建任务。  
结果：返回 `task_id`。

4. `GET /api/v1/tasks/{task_id}`  
用途：返回任务详情（`task_spec`、`task_plan`、`task_steps`、`recommendation`、`artifacts`）。

5. `GET /api/v1/tasks/{task_id}/events`  
用途：返回任务事件时间线。

6. `POST /api/v1/tasks/{task_id}/rerun`  
用途：基于父任务上下文重跑。

### 4.2 文件与产物

1. `POST /api/v1/files/upload`  
用途：上传 AOI（`GeoJSON` / `shp` / `shp.zip`）。

2. `GET /api/v1/artifacts/{artifact_id}/download`  
用途：下载 GeoTIFF / PNG / Markdown。

### 4.3 健康检查

1. `GET /api/v1/health`  
用途：服务存活检查。

---

## 5. 数据模型与表结构（核心）

> 以下为目标结构，字段名可与实现轻微差异，但语义需一致。

### 5.1 `task_runs`

新增或重点关注字段：

1. `status`：`queued/running/success/failed/waiting_clarification`  
2. `current_step`：当前节点或步骤名  
3. `plan_json`：结构化任务计划  
4. `error_code` / `error_message`：失败与澄清原因  
5. `duration_seconds`：执行耗时

### 5.2 `task_steps`

1. 记录每个节点执行状态：`pending/running/success/failed/skipped`。  
2. `detail_json` 记录 observation 与执行细节。  
3. 严格校验状态转换顺序。

### 5.3 `task_events`

1. 记录任务和节点级事件：`task_started/tool_execution_started/tool_execution_completed/task_failed/...`。  
2. 事件与 `task_steps` 需要能互相校验时序。

### 5.4 `llm_call_logs`

建议字段：

1. `task_id`、`phase`、`model_name`、`request_id`  
2. `prompt_hash`、`input_tokens`、`output_tokens`、`total_tokens`  
3. `latency_ms`、`status`、`error_message`、`created_at`

---

## 6. LLM 与 Guardrails 设计

### 6.1 调用阶段

1. `parse`：输入消息 -> `ParsedTaskSpec`。  
2. `plan`：输入 `ParsedTaskSpec` -> `TaskPlan`。  
3. `recommend`：输入候选摘要 -> 推荐结果。

### 6.2 结构化约束

1. 所有阶段必须返回 JSON。  
2. 后端使用 Pydantic 严格校验。  
3. 校验失败触发 repair retry，超限即失败或进入澄清。  
4. 工具调用仅允许白名单。

### 6.3 运行保护

1. `max_steps`  
2. `max_tool_calls`  
3. `runtime_timeout_seconds`  
4. `retry_budget`  
5. `unknown_tool_reject`

---

## 7. 迁移策略（LangGraph）

### 7.1 文件级迁移

新增：

1. `packages/domain/services/graph/state.py`  
2. `packages/domain/services/graph/builder.py`  
3. `packages/domain/services/graph/runner.py`  
4. `packages/domain/services/graph/nodes/*.py`

改造：

1. `apps/worker/tasks.py` 调用 graph runner。  
2. `orchestrator.py` 保持创建任务与入队，不承载执行循环。  
3. `llm_client.py` 接入统一调用日志写入。

下线：

1. 自研 `agent_runtime.py` 主循环执行入口。  
2. 生产路径仅保留 LangGraph。

### 7.2 状态与错误细则

1. 三类终态：`success / failed / waiting_clarification`。  
2. 信息不足进入 `waiting_clarification`，不是失败。  
3. 节点异常统一映射错误码并写失败快照。  
4. 每个节点遵循“started -> completed/failed”事件写入。

---

## 8. 8 周排期（LangGraph 版）

### 第 1 周：依赖与可观测

1. LangGraph 依赖定版。  
2. `llm_call_logs` 迁移落地。  
3. 环境变量与 CI 安装稳定。

### 第 2 周：Schema 与 Prompt

1. 对齐 parser/planner/recommendation schema。  
2. Prompt 版本化规范补齐。

### 第 3 周：严格模式收口

1. Parser 严格模式收口。  
2. Planner 严格模式收口。

### 第 4 周：Graph 骨架

1. 建立 state/builder/runner。  
2. 打通最小图执行链路。

### 第 5 周：节点迁移

1. 迁移 AOI/catalog/recommend/process/publish 节点。  
2. 事件与步骤写入对齐。

### 第 6 周：下线旧循环与工具契约

1. 下线旧 runtime 主循环。  
2. 完成 AOI/Catalog 契约化与产物统一。

### 第 7 周：前端联调

1. 任务时间线与观察详情对齐。  
2. 工作台交互与追问重跑体验完善。

### 第 8 周：测试与验收

1. 样例任务全回归（成功/失败/澄清/fallback/rerun）。  
2. 性能与成本基线更新。  
3. 发布验收。

---

## 9. 验收清单（技术）

1. Worker 在默认链路下通过 LangGraph 跑通完整任务。  
2. 任务详情稳定返回 `task_spec/task_plan/task_steps/recommendation/artifacts`。  
3. 事件流可稳定展示 started/completed/failed。  
4. LLM 调用日志可追踪并可关联到任务。  
5. 回归测试、ruff、build、compileall 全通过。

---
