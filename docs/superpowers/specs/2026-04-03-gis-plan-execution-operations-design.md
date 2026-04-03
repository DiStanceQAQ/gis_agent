# GIS Agent 复杂目标分解与通用操作执行设计（上传优先）

日期：2026-04-03  
作者：Codex（与项目 owner 共创确认）

## 1. 背景与问题

当前系统已完成 LangGraph 主链迁移，并具备 Parser/Planner/Recommendation 的 LLM 化能力；但运行层仍以 `run_ndvi_pipeline` 为中心，属于“有限操作集”。

这与目标存在差距：

- 用户会给出模糊且复合的 GIS 目标（例如“把 a.tif 裁剪后统计并导出”）
- 系统应先澄清再拆解为基础操作，并按步骤执行
- 用户需要在执行前看到并可修改计划，然后确认执行

## 2. 目标与非目标

### 2.1 目标

1. 将复杂 GIS 目标拆解为“原子操作计划（DAG）”并可执行。
2. 采用“信息不全先追问 -> 生成计划 -> 用户可修改 -> 用户确认后执行”的闭环。
3. 第一阶段采用“上传优先”，支持上传栅格/矢量输入。
4. 建立操作注册表，后续新增操作只需“实现 handler + 注册配置”。

### 2.2 非目标（第一阶段不做）

1. 不追求一次性覆盖所有 GIS 操作。
2. 不在第一阶段优先建设在线 STAC 任务入口（保留兼容，不作为主入口）。
3. 不做全自由 LLM 直接调任意工具执行（必须经过校验门禁）。

## 3. 核心方案（推荐并确认）

采用“混合规划型 Agent”：

- LLM 负责意图理解、初始计划草拟、澄清文案
- 规则系统负责计划合法性与可执行性校验
- 用户负责最终执行前审批（并可编辑）
- LangGraph 负责审批后的可审计执行

## 4. 端到端流程

1. `drafting`：解析意图与槽位（input/aoi/operation/output）。
2. `awaiting_clarification`：关键槽位缺失时追问，不进入执行。
3. `planning`：生成 `PlanDraft`（可包含复合目标拆解）。
4. `awaiting_approval`：展示可编辑计划。
5. 用户编辑计划后执行 `re-validate`。
6. `approved`：计划锁定并带版本号。
7. `running`：LangGraph 按步骤执行。
8. `success/failed/cancelled`：终态，支持重跑与从失败点恢复。

强约束：`未 approved 禁止执行`。

## 5. 架构分层

### 5.1 Intent & Slot 层

职责：从自然语言提取目标与槽位。

关键槽位：

- `input_raster` / `input_vector`
- `aoi`
- `analysis_goal`
- `target_crs`
- `resolution`
- `output_format`

### 5.2 Clarification 层

职责：只针对缺失关键参数进行最小追问。

策略：

- 缺少“必须参数”才追问
- 一次只问高价值缺口，减少对话负担

### 5.3 Plan Draft 层

职责：将目标拆为 DAG 节点（原子操作）。

示例：

- `input.upload_raster` -> `raster.clip` -> `raster.zonal_stats` -> `artifact.export`

### 5.4 Plan Guard 层

职责：门禁校验。

校验类型：

- 操作白名单（仅注册表允许）
- 参数 schema 校验
- 拓扑无环校验
- 输入输出类型链路匹配

### 5.5 Approval 层

职责：用户改计划并确认。

特性：

- 编辑后强制重新校验
- 通过后生成 `PlanApproved(version=N)`

### 5.6 Execution 层（LangGraph）

职责：按 `PlanApproved` 执行、记录步骤事件与产物。

行为：

- 每步记录状态/输入摘要/输出摘要/错误码
- 失败可定位到 step

## 6. 核心数据模型

### 6.1 对象定义

- `TaskIntent`：用户目标摘要
- `SlotBundle`：参数槽位集合
- `PlanDraft`：LLM 草稿计划
- `PlanValidated`：校验通过计划
- `PlanApproved`：用户确认计划（唯一执行输入）
- `RunRecord`：执行实例

### 6.2 计划节点结构

- `step_id`
- `op_name`
- `depends_on`
- `inputs`
- `params`
- `outputs`
- `retry_policy`

### 6.3 计划版本规则

- 编辑即升版本
- 执行必须绑定具体 `approved_plan_version`

## 7. 操作注册表设计

### 7.1 统一注册项

每个操作包含：

- `op_name`
- `input_types`
- `param_schema`
- `output_types`
- `executor`
- `default_params`
- `retryable_errors`

### 7.2 第一阶段操作清单（上传优先）

输入相关：

- `input.upload_raster`
- `input.upload_vector`

栅格相关：

- `raster.clip`
- `raster.reproject`
- `raster.resample`
- `raster.band_math`（含 NDVI/NDWI 表达式）
- `raster.zonal_stats`

矢量相关：

- `vector.buffer`

产物相关：

- `artifact.export`（GeoTIFF/PNG/CSV/Markdown）

## 8. 错误处理与可观测性

### 8.1 错误码分层

规划层：

- `PLAN_SLOT_MISSING`
- `PLAN_SCHEMA_INVALID`
- `PLAN_DEPENDENCY_CYCLE`

审批层：

- `PLAN_APPROVAL_REQUIRED`
- `PLAN_EDIT_INVALID`

执行层：

- `OP_INPUT_TYPE_MISMATCH`
- `OP_PARAM_INVALID`
- `OP_RUNTIME_FAILED`

产物层：

- `ARTIFACT_EXPORT_FAILED`

### 8.2 事件流

至少记录：

- `clarification_requested`
- `plan_drafted`
- `plan_validated`
- `plan_approved`
- `step_started`
- `step_succeeded`
- `step_failed`
- `task_completed`

## 9. 与现有代码的迁移策略

### 9.1 保持主链稳定

- 保留现有 LangGraph 框架
- 将固定 `run_ndvi_pipeline` 演进为 `run_processing_pipeline`
- NDVI 作为预置模板操作迁移到注册表

### 9.2 与任务清单对齐

- `LPE-30`：输入/AOI 契约化（含上传 raster/vector）
- `LPE-31`：Catalog 作为可选输入源，第二阶段强化
- `LPE-32/33/34`：合并为通用处理引擎 + 操作注册表
- `LPE-35`：统一导出与产物元数据
- `LPE-40~44`：新增“计划编辑与确认”优先于纯展示增强

## 10. 验收标准（用户视角）

1. 用户输入模糊任务时，系统先追问关键缺失项。
2. 补全后自动生成可编辑计划。
3. 用户确认后任务才执行。
4. 用户改出非法计划时，系统明确拦截并说明原因。
5. 执行结果可下载、可追踪、可重跑。

## 11. 测试策略

1. Schema 测试：每个操作参数与输入输出类型验证。
2. Plan Guard 测试：白名单、依赖拓扑、类型链路校验。
3. Runtime 测试：逐步执行、失败中断、重试策略。
4. E2E 样例：
   - 上传 tif -> clip -> export
   - 上传 tif + 上传 AOI -> clip -> zonal_stats -> csv
   - 上传 vector -> buffer -> clip -> export
5. 回归：现有 NDVI 路径继续可运行。

## 12. 风险与缓解

1. LLM 计划不稳定：用 Plan Guard 兜底，拒绝不合法计划。
2. 用户编辑导致链路断裂：编辑后强制全量校验。
3. 操作扩展后测试成本上升：模板化测试结构（每操作 3 类用例）。

## 13. 实施顺序建议

1. 先落 `operation registry` + `plan guard` + 新状态机。
2. 再落 `raster.clip` + `artifact.export` 形成最小可用闭环。
3. 然后追加 `buffer`、`zonal_stats`、`reproject`、`resample`、`band_math`。
4. 最后完善前端计划编辑与审批交互。

---

本设计经过逐段确认，下一步进入 implementation planning（writing-plans）。
