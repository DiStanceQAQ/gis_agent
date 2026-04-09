# Task Intent Capability Model Design

日期：2026-04-09  
作者：Codex（与项目 owner 共创确认）

## 1. 背景与问题

当前系统围绕 [analysis.py](/Users/ljn/gis_agent/packages/schemas/analysis.py) 中的 `analysis_type` 组织任务理解、参数校验与执行分流。这个设计在项目早期足够直接，但随着对话理解、revision、response policy、planner 和多种 GIS 操作的演进，`analysis_type` 已经承担了过多职责：

1. 既表示“用户想做什么”，又表示“系统最终怎么执行”。
2. 既影响 parser 输出，又影响 response policy 缺字段规则。
3. 既影响 `operation_params` 校验，又影响 planner 的操作模板选择。
4. 既承载高层业务语义（如 `NDVI`），又承载低层操作标签（如 `CLIP`、`BUFFER`）。
5. 作为单一中心字段，迫使多层服务围绕它重复猜测和补丁式扩展。

这已经引出几个系统性问题：

- 语义混乱：`NDVI`、`BAND_MATH`、`WORKFLOW` 同处一层，但它们的抽象层级并不一致。
- 扩展困难：每增加一种能力，往往需要同步修改 parser、schema、response policy、planner 等多处逻辑。
- 规则分散：必填字段、条件字段、可执行性判断散落在多个模块和 prompt 中，容易互相打架。
- 迁移受限：revision 与 understanding 体系已经引入更丰富的结构，但核心任务模型仍被 `analysis_type` 限制。
- 用户体验不稳定：例如不需要时间范围的任务仍然可能被追问 `time_range`，根因不是局部 bug，而是模型中心设计不当。

因此，这次不是要“优化几个枚举值”，而是要重做任务语义模型本身。

## 2. 目标与非目标

### 2.1 目标

1. 用“语义层 + 能力层”为核心的新模型替代 `analysis_type` 单中心模型。
2. 显式区分：
   - 用户语义层：用户想达成什么目标。
   - GIS 能力层：系统将其映射到哪类专业能力。
   - 执行层：最终具体使用哪些操作。
3. 将字段约束和执行 readiness 从 parser / response policy 中抽离，建立统一的 Requirement Profile 系统。
4. 让 parser、resolver、planner 的职责边界清晰，不再重复“猜任务类型”。
5. 为后续扩展更多遥感指数、地理处理和多步 workflow 打下稳定基础。
6. 为 revision、session memory、前端解释性展示提供结构化、可解释的任务模型。

### 2.2 非目标

1. 本期不重写 GIS 执行内核、artifact 存储或 LangGraph runtime。
2. 本期不做前端交互形态重设计；前端仅消费新结构。
3. 本期不引入黑盒自动推理代替白盒 requirement 规则。
4. 本期不把多任务 cohort / sibling task 作为中心设计目标。

## 3. 决策（已确认）

1. 新架构采用“语义层 + 能力层”为核心的双层模型，而不是继续围绕 `analysis_type` 修补。
2. 重设计允许破坏式重构；旧模型只作为过渡兼容层存在。
3. 目标是完整修复语义层、执行层、参数约束层，而不是最小实现。
4. 用户语义层与 GIS 专业语义都需要保留：前者服务对话，后者服务执行。
5. `analysis_type` 退化为 legacy 兼容标签，不再作为核心任务主轴。
6. `RequirementProfile` 成为字段要求与 readiness 判断的唯一权威来源。
7. prompt 体系按层重写：`intent -> parser -> capability_resolver -> planner`。

## 4. 设计原则

1. **单一职责**：每一层只回答一个问题。
2. **语义与执行分离**：高层用户目标不得直接等同于低层操作。
3. **白盒约束**：字段必填、条件必填和执行门控都必须由显式规则声明。
4. **渐进替换**：实施上分阶段迁移，架构上以新模型为中心。
5. **revision 友好**：新模型必须天然适配 revision 与 session memory，而不是额外补丁。
6. **兼容可追溯**：旧 `analysis_type` 的来源、映射和退场过程都必须可回放。

## 5. 总体架构

新的任务建模主链路：

```text
用户消息
-> Intent Router
-> Parser (IntentResolution)
-> Capability Resolver (CapabilityResolution)
-> Requirement Evaluator
-> Executable Task Spec Builder
-> Planner
-> Runtime
```

新的任务建模中心：

```text
TaskIntent -> CapabilityFamily -> OperationKind -> RequirementProfile -> ExecutableTaskSpec
```

对应关系：

1. `TaskIntent`
- 面向用户语义。
- 回答“用户想达成什么目标”。

2. `CapabilityFamily`
- 面向 GIS 专业能力域。
- 回答“这属于哪类空间分析能力”。

3. `OperationKind`
- 面向执行原子操作。
- 回答“系统最后具体执行什么操作”。

4. `RequirementProfile`
- 面向字段约束和 readiness。
- 回答“这个目标在当前上下文下需要什么、缺什么、是否能执行”。

5. `ExecutableTaskSpec`
- 面向 planner / runtime 的规范化输入。
- 回答“当前任务在可执行语义上究竟是什么”。

## 6. 新的领域模型

### 6.1 用户语义层：TaskIntent

建议定义以下一组顶层任务语义：

```python
TaskIntent = Literal[
    "clip_dataset",
    "compute_index",
    "terrain_analysis",
    "vector_proximity",
    "transform_dataset",
    "combine_datasets",
    "custom_raster_formula",
    "raster_filtering",
    "extract_features",
    "summarize_dataset",
]
```

说明：

- `clip_dataset`：用户想把某个数据按边界裁剪。
- `compute_index`：用户想计算预置指数，如 NDVI/NDWI。
- `terrain_analysis`：用户想生成坡度、坡向等地形导数。
- `vector_proximity`：用户想做 buffer 等邻近类操作。
- `transform_dataset`：用户想重投影、重采样、格式转换等。
- `combine_datasets`：用户想拼接、叠加、联合处理多个数据源。
- `custom_raster_formula`：用户想执行自定义波段表达式。
- `raster_filtering`：用户想对栅格做平滑、滤波、去噪。
- `extract_features`：用户想从现有数据中筛选、提取对象。
- `summarize_dataset`：用户想查看数据摘要、范围、统计信息。

### 6.2 专业能力层：CapabilityFamily

```python
CapabilityFamily = Literal[
    "raster_processing",
    "vector_processing",
    "raster_vector_interaction",
    "temporal_remote_sensing",
    "terrain_modeling",
    "data_transformation",
]
```

说明：

- `raster_processing`：波段运算、滤波、重分类等栅格内部处理。
- `vector_processing`：buffer、clip、intersection 等矢量操作。
- `raster_vector_interaction`：栅格与矢量之间的掩膜、裁剪、统计。
- `temporal_remote_sensing`：带时间维度的指数分析、变化监测。
- `terrain_modeling`：DEM 派生分析。
- `data_transformation`：重投影、重采样、格式转换等预处理。

### 6.3 执行层：OperationKind

```python
OperationKind = Literal[
    "raster.clip",
    "raster.band_math",
    "raster.filter",
    "raster.reproject",
    "raster.resample",
    "raster.mosaic",
    "raster.terrain_slope",
    "raster.terrain_aspect",
    "vector.buffer",
    "vector.clip",
    "vector.intersection",
    "vector.union",
    "vector.erase",
    "vector.spatial_join",
]
```

说明：

- `OperationKind` 是 planner 和 runtime 最终关心的原子操作标签。
- 它不直接暴露给普通用户，但在前端诊断与调试界面中应可见。

### 6.4 RequirementProfile

每个任务不再通过 `analysis_type` 隐式决定字段规则，而是通过 requirement profile 明确声明：

- `required`
- `conditional_required`
- `optional`
- `non_blocking`
- `risk_sensitive`

示例：

```python
RequirementProfileId = Literal[
    "raster_clip",
    "vector_clip",
    "temporal_index_analysis",
    "terrain_derivative",
    "vector_buffer",
    "raster_formula",
    "raster_filter",
    "data_transform",
]
```

## 7. 新的核心对象

### 7.1 IntentResolution

parser 的正式输出，不再直接产出最终执行任务定义。

```python
class IntentResolution(BaseModel):
    task_intent: TaskIntent
    intent_confidence: float
    user_goal_summary: str
    entity_candidates: EntityCandidates
    intent_hints: dict[str, Any] = Field(default_factory=dict)
    uncertain_fields: list[str] = Field(default_factory=list)
```

职责：

- 提取用户目标。
- 提取候选实体：数据源、AOI、时间范围、输出偏好等。
- 提供高层提示：`index_name=ndvi`、`terrain_product=slope` 等。
- 不决定最终 operation plan。

### 7.2 CapabilityResolution

semantic mapper / capability resolver 的正式输出。

```python
class CapabilityResolution(BaseModel):
    capability_family: CapabilityFamily
    candidate_operation_kinds: list[OperationKind]
    selected_operation_kind: OperationKind | None = None
    requirement_profile_id: RequirementProfileId
    resolution_reasoning: str | None = None
```

职责：

- 将用户语义映射到 GIS 能力域。
- 选定候选操作与 requirement profile。
- 输出选择理由，供 trace 与前端解释使用。

### 7.3 ExecutableTaskSpec

planner / runtime 消费的规范化任务规格。

```python
class ExecutableTaskSpec(BaseModel):
    task_intent: TaskIntent
    capability_family: CapabilityFamily
    operation_kind: OperationKind
    input_bindings: dict[str, Any]
    constraints: dict[str, Any] = Field(default_factory=dict)
    required_fields_status: dict[str, str] = Field(default_factory=dict)
```

职责：

- 汇总 intent、capability、requirements 的结果。
- 提供 planner 所需的最终规范化输入。
- 与 revision 一起成为新任务模型的主存储载体。

## 8. 旧模型到新模型的映射

### 8.1 旧 `analysis_type` 的新归属

| 旧值 | 新 TaskIntent | 新 CapabilityFamily | 新 OperationKind | 备注 |
|------|---------------|---------------------|------------------|------|
| `NDVI` | `compute_index` | `temporal_remote_sensing` | `raster.band_math` | `index_name=ndvi` |
| `NDWI` | `compute_index` | `temporal_remote_sensing` | `raster.band_math` | `index_name=ndwi` |
| `BAND_MATH` | `custom_raster_formula` | `raster_processing` | `raster.band_math` | 自定义表达式 |
| `FILTER` | `raster_filtering` | `raster_processing` | `raster.filter` | 由 `filter_method` 决定细节 |
| `SLOPE_ASPECT` | `terrain_analysis` | `terrain_modeling` | `raster.terrain_slope` / `raster.terrain_aspect` | `terrain_product` 细化 |
| `BUFFER` | `vector_proximity` | `vector_processing` | `vector.buffer` | |
| `CLIP` | `clip_dataset` | `raster_vector_interaction` / `vector_processing` | `raster.clip` / `vector.clip` | 由源数据模态决定 |
| `WORKFLOW` | 不再作为顶层语义 | `data_transformation` 等 | 多个 operation | 退化为执行组织形态 |

### 8.2 关键解释

1. `NDVI` / `NDWI`
- 不再是平行的大类。
- 它们是 `compute_index` 的具体参数值。

2. `CLIP`
- 不再既是语义、又是执行操作。
- 用户说“裁剪”得到的是 `task_intent=clip_dataset`。
- resolver 再根据数据模态判断 `raster.clip` 还是 `vector.clip`。

3. `WORKFLOW`
- 不再作为顶层用户语义。
- 它只是 planner 生成的执行组织方式，例如 multi-step workflow。

## 9. Requirement Profile 体系

### 9.1 设计目标

将字段规则统一到单一系统中，替代当前分散在 parser、response policy、prompt 和 schema 校验中的多份逻辑。

### 9.2 示例

#### `raster_clip`

```python
{
  "required": ["source_dataset", "clip_geometry"],
  "conditional_required": [],
  "optional": ["output_format", "output_name", "target_crs"],
  "non_blocking": ["time_range"],
}
```

#### `temporal_index_analysis`

```python
{
  "required": ["source_dataset", "aoi", "time_range", "index_name"],
  "conditional_required": [],
  "optional": ["cloud_filter", "output_format"],
  "non_blocking": [],
}
```

#### `vector_buffer`

```python
{
  "required": ["source_vector", "distance_m"],
  "conditional_required": [],
  "optional": ["dissolve", "output_format"],
  "non_blocking": [],
}
```

### 9.3 分工

1. parser：只负责提取候选字段。
2. capability resolver：选择 `requirement_profile_id`。
3. requirement evaluator：计算 `missing_required`、`field_satisfaction`、`execution_readiness`。
4. response policy：只消费 evaluator 的输出做交互决策。

### 9.4 结果

- `time_range` 不再是全局概念。
- 它只在某些 requirement profile 中是 required，在另一些 profile 中是 optional 或 irrelevant。
- 所有“缺什么字段”的来源最终可追溯到同一个 profile。

## 10. 新的职责分工

### 10.1 Intent Router

职责：识别消息角色，而不是 GIS 细分语义。

输出：

- `chat`
- `new_task`
- `task_correction`
- `task_confirmation`
- `task_followup`
- `ambiguous`

它不得再输出：

- `analysis_type`
- `task_intent`
- `operation_kind`

### 10.2 Parser

职责：理解用户目标与实体候选。

输出：`IntentResolution`

它负责：

- 识别 `task_intent`
- 提取数据源、AOI、时间范围、输出偏好
- 提供 intent hints

它不负责：

- 选定最终 operation kind
- 决定 requirement profile
- 直接给 planner 生成可执行 spec

### 10.3 Capability Resolver

职责：将 `IntentResolution` 映射到 GIS 能力与执行候选。

输入：

- `IntentResolution`
- session / revision context
- uploaded file summaries
- input slots

输出：`CapabilityResolution`

### 10.4 Requirement Evaluator

职责：

- 读取 `RequirementProfile`
- 检查当前字段状态
- 输出 field satisfaction 和 execution readiness

这是纯代码规则层，不依赖 LLM。

### 10.5 Planner

职责：

- 读取 `ExecutableTaskSpec`
- 生成 `OperationPlan`
- 判断审批、步骤依赖和执行顺序

planner 不再重新解释用户原话或猜语义。

## 11. Prompt 重写方案

### 11.1 `intent.md`

继续保持轻量，只负责判消息角色。

输出示例：

```json
{
  "intent": "task_correction",
  "confidence": 0.91,
  "reason": "用户在修正上一轮任务字段",
  "references_active_task": true
}
```

### 11.2 `parser.md`

改为输出 `IntentResolution`，例如：

```json
{
  "task_intent": "clip_dataset",
  "intent_confidence": 0.88,
  "user_goal_summary": "用户希望把上传的 tif 按省界进行裁剪",
  "entity_candidates": {
    "source_dataset": [],
    "clip_geometry": [],
    "aoi": [],
    "time_range": [],
    "output_preferences": []
  },
  "intent_hints": {
    "index_name": null,
    "terrain_product": null,
    "data_modality": "raster_vector"
  },
  "uncertain_fields": ["clip_geometry_selector"]
}
```

### 11.3 新增 `capability_resolver.md`

职责：

- 输入 `IntentResolution` 与上下文
- 输出 `CapabilityResolution`

示例：

```json
{
  "capability_family": "raster_vector_interaction",
  "candidate_operation_kinds": ["raster.clip"],
  "selected_operation_kind": "raster.clip",
  "requirement_profile_id": "raster_clip",
  "resolution_reasoning": "已有栅格源和矢量边界候选，用户目标是裁剪栅格"
}
```

### 11.4 `planner.md`

职责：

- 只消费 `ExecutableTaskSpec`
- 只做执行规划
- 不再解释用户原话，也不再猜 `analysis_type`

## 12. 数据模型与持久化设计

### 12.1 Pydantic schema 重组

建议新增：

1. `packages/schemas/task_intent.py`
2. `packages/schemas/capability.py`
3. `packages/schemas/requirements.py`
4. `packages/schemas/executable_task.py`

建议保留但降级：

- [analysis.py](/Users/ljn/gis_agent/packages/schemas/analysis.py)
- [task.py](/Users/ljn/gis_agent/packages/schemas/task.py) 中的 `ParsedTaskSpec`

### 12.2 Revision 持久化

在现有 `task_spec_revisions` 上新增：

- `task_intent`
- `capability_family`
- `operation_kind`
- `requirement_profile_id`
- `intent_resolution_json`
- `capability_resolution_json`
- `executable_spec_json`

其中：

- `raw_spec_json` 保留为兼容和回放用途
- 新字段成为系统主读路径

### 12.3 `message_understandings`

继续保留，但角色调整为：

- 理解过程日志
- candidate/evidence/trace 存储
- 与 revision 建立更明确的链路

而不再承担最终任务规格的主存储职责。

### 12.4 `task_runs`

保留 legacy 字段：

- `analysis_type`
- `operation_params`

但这些字段仅作为镜像兼容输出。新系统主读：

- `active_revision.task_intent`
- `active_revision.capability_family`
- `active_revision.operation_kind`
- `active_revision.executable_spec_json`

## 13. 迁移策略

### 13.1 Phase A：新模型入场，旧模型继续可用

1. 新 schema、新 resolver、新 requirement profile 入库。
2. parser 开始双写：
   - 新 `IntentResolution`
   - 旧 `ParsedTaskSpec`
3. revision 开始双写：
   - 新字段
   - 旧字段

### 13.2 Phase B：主读切换

1. response policy 改读 `RequirementProfile`。
2. planner 改读 `ExecutableTaskSpec`。
3. orchestrator 改以 `task_intent/capability/operation_kind` 为主。

### 13.3 Phase C：legacy 降级

1. `analysis_type` 不再参与核心决策。
2. 对外 API 仍短期返回 legacy 字段。
3. 前端改读新字段后，再考虑彻底弱化旧字段。

### 13.4 历史数据回填

对历史 revision 进行 backfill：

- `NDVI` -> `compute_index + index_name=ndvi`
- `NDWI` -> `compute_index + index_name=ndwi`
- `BAND_MATH` -> `custom_raster_formula`
- `FILTER` -> `raster_filtering`
- `SLOPE_ASPECT` -> `terrain_analysis`
- `BUFFER` -> `vector_proximity`
- `CLIP` -> `clip_dataset`
- `WORKFLOW` -> 根据参数推断为 `transform_dataset` / `combine_datasets` / `legacy_generic_workflow`

无法可靠映射的历史任务：

- 标记为 `legacy_unresolved`
- 继续走兼容路径
- 不做黑盒猜测

## 14. 风险与缓解

### 14.1 风险：重构范围大

缓解：

- 按层迁移，不一刀切。
- planner 和 runtime 后移替换。

### 14.2 风险：新旧模型长期并存导致复杂度上升

缓解：

- 明确三阶段迁移边界。
- 将 `analysis_type` 明确标注为 legacy mirror。

### 14.3 风险：新增 resolver 使链路变长

缓解：

- resolver 只负责语义映射，不承担 planner 或 response policy 的职责。
- 通过 trace 明确每层输出，避免黑盒化。

### 14.4 风险：历史任务映射不准确

缓解：

- 明确 `legacy_unresolved` 兜底路径。
- 不对无法可靠映射的数据强行做结构化迁移。

## 15. 测试策略

### 15.1 单元测试

1. `TaskIntent` 规范化与 schema 校验。
2. `CapabilityResolution` 映射规则。
3. `RequirementProfile` evaluator。
4. `analysis_type -> 新模型` 映射表。

### 15.2 集成测试

1. parser 产出 `IntentResolution`。
2. resolver 产出 `CapabilityResolution`。
3. requirement evaluator 正确决定 `missing_fields` 与 readiness。
4. planner 正确消费 `ExecutableTaskSpec`。

### 15.3 回归测试

1. `CLIP` 不再错误要求 `time_range`。
2. `NDVI` / `NDWI` 仍然稳定要求 `time_range`。
3. revision 链在 correction / confirmation / blocked 恢复时仍可回放。
4. 老接口中的 `analysis_type` 输出仍可兼容。

## 16. 推荐实施顺序

1. 新 schema 与 requirement profile 建模
2. parser 改产 `IntentResolution`
3. 新增 capability resolver
4. response policy 切到 requirement evaluator
5. planner 改读 `ExecutableTaskSpec`
6. revision / persistence 升级
7. API / 前端切换
8. `analysis_type` 退场

## 17. 结论

这次重设计的核心，不是替换一个字段名，而是重置任务建模中心。

旧设计的中心是：

```text
analysis_type
```

新设计的中心是：

```text
TaskIntent -> CapabilityFamily -> OperationKind -> RequirementProfile -> ExecutableTaskSpec
```

做完这次重构后：

1. 用户语义、GIS 能力、执行原子操作各归其位。
2. 字段约束拥有统一权威来源。
3. parser、resolver、planner 的职责边界清晰。
4. 新能力扩展不再需要围着 `analysis_type` 四处打补丁。
5. revision、session memory、前端解释性展示将拥有更稳定的结构基础。
