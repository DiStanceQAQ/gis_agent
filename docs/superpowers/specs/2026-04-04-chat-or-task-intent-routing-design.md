# Chat Or Task Intent Routing Design

## Goal

在同一个 `POST /api/v1/messages` 入口下，实现“普通对话”和“任务执行”双模式：
- 用户普通聊天时，系统给出 assistant 回复并保留会话上下文。
- 用户表达执行意图时，系统进入现有任务链路（parse/plan -> awaiting_approval -> approve/reject -> ReAct + Function Calling）。

## Scope (Backend MVP)

### In Scope

1. 在消息入口增加意图识别：`chat | task | ambiguous`。
2. assistant 聊天回复落库（`messages.role=assistant`）。
3. 任务创建新增“确认门槛”：
   - 高置信 task 意图但未确认执行：先回复“可创建任务，请确认执行”。
   - 用户在后续消息出现确认词（如“开始执行/按这个执行”）后，才真正创建 task。
4. 保持现有审批与执行链路不变。

### Out of Scope

1. 前端 UI 改造（后端先提供数据契约）。
2. 多代理记忆系统、长期知识库。
3. 复杂会话状态机（本期不新增 session 状态字段）。

## Decisions (Confirmed)

1. 同一 `/messages` 自动路由。
2. 意图不确定时先按 `chat` 处理并澄清。
3. 聊天回复走 LLM。
4. 高置信执行意图先确认再落任务。
5. 任务确认仅靠用户文本（不新增确认 API）。
6. assistant 回复入库并可回放。

## Architecture

### New Components

1. `intent.py`（新服务）
   - 负责意图识别与置信度输出。
   - 输出结构：
     - `intent`: `chat | task | ambiguous`
     - `confidence`: `0~1`
     - `reason`

2. `chat.py`（新服务）
   - 负责普通对话回复生成（LLM）。
   - 基于最近 N 条会话消息构造 prompt。

3. `confirmation.py`（可并入 intent.py）
   - 识别“确认执行”关键词与短语。

### Existing Components Reused

1. `orchestrator.create_message_and_task`：继续作为任务创建主链。
2. `approve/reject/patch plan`：保持不变。
3. 任务运行时、ReAct、Function Calling：保持不变。

## API Contract Changes

### Request

`POST /api/v1/messages` 请求体保持不变。

### Response

`MessageCreateResponse` 扩展为统一响应（向后兼容）：
- 现有字段保留：`task_id/task_status/need_approval/...`
- 新增：
  - `mode`: `chat | task`
  - `assistant_message`: `str | null`
  - `intent`: `chat | task | ambiguous | null`
  - `intent_confidence`: `float | null`
  - `awaiting_task_confirmation`: `bool`

约定：
1. `mode=chat` 时，`task_id` 可为空，返回 assistant 文本。
2. `mode=task` 时，沿用现有任务字段。

## Data Model

无需新表，复用 `messages`：
1. 用户输入：`role=user`。
2. assistant 回复：`role=assistant`，`linked_task_id` 为空或关联后续 task。

## Flow

### A. 普通聊天

1. 用户消息入库。
2. 意图识别结果为 `chat` 或低置信 `ambiguous`。
3. 生成 assistant 回复并入库。
4. 返回 `mode=chat`。

### B. 执行意图（未确认）

1. 用户消息入库。
2. 意图识别为高置信 `task`。
3. 若消息未命中确认词：
   - assistant 回复“我可以按该方案创建任务，回复‘开始执行’确认”。
   - 不创建 task。
   - 返回 `mode=chat` + `awaiting_task_confirmation=true`。

### C. 执行意图（已确认）

1. 用户消息入库。
2. 命中确认词，并且会话中存在最近一次可执行草案语境。
3. 调用现有 task 创建链路。
4. 返回 `mode=task`，任务进入 `awaiting_approval`（或 `waiting_clarification`）。

## Error Handling

1. 意图识别 LLM 失败：降级 `ambiguous`，走 chat 澄清。
2. 聊天 LLM 失败：返回固定友好回复，并记录失败日志。
3. 确认消息缺少可执行上下文：assistant 提示“请先描述目标或方案”。
4. 任何新增能力不影响现有 task API 行为。

## Observability

1. LLM 调用日志新增 phase：`intent`、`chat`。
2. 关键事件：
   - `message_intent_classified`
   - `task_confirmation_requested`
   - `task_confirmation_received`

## Test Plan

1. `test_messages_intent_routing.py`（新）
   - chat 消息 -> `mode=chat` + assistant 落库。
   - task 高置信但未确认 -> 不建 task，返回确认提示。
   - 确认词消息 -> 创建 task 并进入审批链。
   - ambiguous -> chat 澄清。

2. 回归
   - 现有审批、改 plan、拒绝、ReAct/FC 测试全部保持通过。

## Migration & Rollout

1. 先后端开关：`GIS_AGENT_INTENT_ROUTER_ENABLED=true`。
2. 默认开启；若出现异常可回滚到旧行为（直接任务创建）。

## Risks

1. 意图误判导致“该聊天却想执行”或反之。
   - 通过“高置信仍先确认执行”降低误触发风险。
2. 确认语句识别覆盖不足。
   - 初版规则词典 + 后续日志驱动迭代。
