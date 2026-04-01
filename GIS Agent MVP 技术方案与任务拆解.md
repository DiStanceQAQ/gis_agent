# GIS Agent MVP 技术方案与任务拆解

> 文档类型：技术方案 + 开发拆解  
> 对应产品文档：`GIS Agent MVP PRD.md`  
> 版本：v1.0  

------

## 1. 文档目标

本文档用于把 PRD 收敛为研发可直接执行的方案，明确：

- 首发能力边界
- 模块划分与职责
- 核心接口设计
- 表结构建议
- 8 周实施排期
- 各阶段 DoD（Definition of Done）

------

## 2. 首发范围基线

### 2.1 P0 必做范围

- 自然语言输入 NDVI 任务
- AOI 输入支持：行政区名称、已配置别名、`geojson`、`shp.zip`
- 数据源：Sentinel-2 L2A、Landsat 8/9 Collection 2 Level-2
- 自动候选搜索与规则推荐
- NDVI 计算
- 中位数合成
- 质量检查与 1 次自动 fallback
- 输出 PNG、GeoTIFF/COG、方法说明、结果摘要
- 单页对话式 Web 界面
- 当前任务状态展示
- 最近任务上下文追问
- 全链路日志与任务记录

### 2.2 P1 预留能力

- HLS 适配器
- `single_scene`
- `max`
- 更丰富区域统计
- 更复杂多轮追问
- 更强地图样式

### 2.3 不做事项

- Earth Engine 作为唯一执行引擎
- 任意公式引擎
- 通用 WebGIS
- 团队协作与权限
- 并发多任务编排平台

------

## 3. 技术选型

### 3.1 推荐栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 前端 | React + Vite + TypeScript | 单页应用足够，构建快，开发复杂度低 |
| API | FastAPI | Python 生态友好，适合文件上传、SSE、任务编排 |
| 队列 | Celery + Redis | 计算任务明显重于 HTTP 请求，需要独立 Worker |
| 数据库 | PostgreSQL 16 + PostGIS | 任务记录、AOI 几何、空间查询统一处理 |
| 对象存储 | MinIO / S3 | 保存 GeoTIFF、PNG、方法说明等产物 |
| GIS 处理 | rasterio、geopandas、shapely、pyproj | 固定工具链，易测试 |
| STAC 检索 | pystac-client | 统一检索 STAC API |
| 栅格装载 | odc-stac | 将 STAC Item 加载为 xarray Dataset，适合 P0 |
| 导出优化 | rio-cogeo | 输出 COG，便于下载与后续复用 |
| 测试 | pytest | 单元测试 + 集成测试 |

### 3.2 选型原则

- 前后端解耦，避免把工作流逻辑塞进前端。
- LLM 仅做解析、推荐理由和解释，不做核心栅格运算。
- 计算任务走异步 Worker，API 只负责接单、状态和结果分发。
- 输出物统一落对象存储，数据库只保存元数据和路径。
- P0 不同时引入两套栅格执行框架；如后期性能不足，再评估 `stackstac`。

### 3.3 环境建议

- 开发环境：Docker Compose
- 最小服务：`web`、`api`、`worker`、`redis`、`postgres`、`minio`
- Python 建议：3.11
- GDAL / raster 依赖建议通过 `conda-forge` 或预构建镜像管理，避免本地编译不一致

------

## 4. 总体架构

### 4.1 逻辑分层

```text
Web UI
-> API Gateway / Session Service
-> Task Orchestrator
-> Celery Queue
-> GIS Worker
   -> AOI Service
   -> Catalog Adapter
   -> Recommendation Engine
   -> NDVI Pipeline
   -> QC / Fallback
   -> Exporter
-> PostgreSQL / PostGIS
-> Object Storage
```

### 4.2 核心运行链路

```text
用户发消息 / 上传文件
-> API 生成 message 与 task
-> Parser 产出 TaskSpec
-> AOI 标准化
-> 搜索 Sentinel-2 / Landsat
-> 规则打分推荐
-> Worker 执行 NDVI 中位数合成
-> QC
-> 产出 PNG + GeoTIFF/COG + methods.md
-> 写回结果摘要与任务状态
-> 前端轮询或 SSE 更新界面
```

### 4.3 状态机

建议任务状态：

- `draft`
- `parsed`
- `waiting_clarification`
- `queued`
- `running`
- `fallback_running`
- `success`
- `failed`
- `cancelled`

建议步骤状态：

- `pending`
- `running`
- `success`
- `failed`
- `skipped`

------

## 5. 模块拆解

### 5.1 模块清单

| 模块 | 职责 | 是否 P0 |
| --- | --- | --- |
| Session / Message | 保存对话、上下文继承、最近任务引用 | 是 |
| File Ingest | 上传文件校验、解压、元数据保存 | 是 |
| Task Parser | 自然语言转 `TaskSpec` | 是 |
| AOI Service | 边界解析、几何修复、面积和 bbox 计算 | 是 |
| Catalog Adapter | STAC 检索与候选摘要 | 是 |
| Recommendation Engine | 规则打分、主备方案生成 | 是 |
| NDVI Pipeline | 波段映射、QA、裁剪、合成、导出 | 是 |
| QC / Fallback | 结果质量判断与自动重试 | 是 |
| Artifact Service | 管理 PNG、GeoTIFF、Markdown 等产物 | 是 |
| Explanation Service | 生成方法说明、结果解释、局限性 | 是 |
| Event / Logging | 状态流转、步骤日志、错误追踪 | 是 |
| HLS Adapter | 第三类数据源接入 | 否 |

### 5.2 关键模块职责

#### A. Session / Message

输入：

- 用户文本
- 上传文件引用
- 会话 ID

输出：

- `message_id`
- 上下文窗口摘要
- `task_id`

规则：

- 只允许围绕“当前任务或最近一次成功任务”继承参数。
- 追问若无法安全继承，必须回退到补充提问。

#### B. Task Parser

输入：

- 当前消息
- 最近任务上下文
- 上传文件元信息

输出：

```json
{
  "aoi_input": "北京西山",
  "aoi_source_type": "place_alias",
  "time_range": {
    "start": "2024-06-01",
    "end": "2024-08-31"
  },
  "analysis_type": "NDVI",
  "preferred_output": ["png_map", "geotiff", "methods_text"],
  "user_priority": "balanced",
  "need_confirmation": false
}
```

关键约束：

- 区域和时间缺一不可。
- 未指明“植被长势”时默认 NDVI。
- 模糊地名必须返回 `need_confirmation=true`。

#### C. AOI Service

职责：

- 读取 `geojson`
- 解压 `shp.zip`
- 行政区 / 别名边界解析
- `make_valid` 修复
- 统一 CRS 为 EPSG:4326
- 计算 `bbox`、面积、质检结果

输出：

```json
{
  "aoi_id": "aoi_xxx",
  "geom_wkt": "MULTIPOLYGON(...)",
  "bbox_wkt": "POLYGON(...)",
  "bbox": [115.7, 39.8, 116.2, 40.1],
  "area_km2": 326.5,
  "is_valid": true
}
```

#### D. Catalog Adapter

职责：

- 调用 STAC API 搜索候选影像
- 汇总 `scene_count`
- 估算覆盖率和可用像元比例
- 标准化不同数据源字段

统一输出：

```json
{
  "dataset_name": "sentinel2",
  "collection_id": "sentinel-2-l2a",
  "scene_count": 18,
  "coverage_ratio": 0.98,
  "effective_pixel_ratio_estimate": 0.81,
  "cloud_metric_summary": {
    "median": 0.12,
    "p75": 0.20
  },
  "spatial_resolution": 10,
  "temporal_density_note": "high"
}
```

#### E. Recommendation Engine

职责：

- 读取候选摘要
- 执行规则打分
- 产出主备数据源和理由

评分建议：

| 维度 | 权重 | 说明 |
| --- | --- | --- |
| 空间匹配度 | 35 | 是否需要更高分辨率 |
| 时间匹配度 | 25 | 可用场景数和密度 |
| 质量可用性 | 25 | 云量、覆盖率、有效像元 |
| 任务适配性 | 15 | 对 NDVI 是否足够稳妥 |

输出：

```json
{
  "primary_dataset": "sentinel2",
  "backup_dataset": "landsat89",
  "scores": {
    "sentinel2": 0.87,
    "landsat89": 0.71
  },
  "reason": "局部山区植被细节分析优先选择 10m 数据",
  "risk_note": "若云量较高，可退回 Landsat 方案"
}
```

#### F. NDVI Pipeline

步骤：

1. 读取候选 STAC Items
2. 做 QA / cloud filter
3. 统一波段到 `red` / `nir`
4. 裁剪 AOI
5. 计算 NDVI
6. 时间窗口中位数合成
7. 生成 GeoTIFF/COG
8. 生成渲染用 PNG
9. 输出统计摘要

约束：

- P0 只保留中位数合成。
- 同一输入和同一数据版本应复现相同结果。
- 中间结果不长期保存，只保存必要统计和最终产物。

#### G. QC / Fallback

检查项：

- `coverage_ratio >= 0.95`
- `valid_pixel_ratio >= 0.70`
- `ndvi_out_of_range_ratio <= 0.01`
- `nodata_ratio <= 0.30`

P0 fallback：

1. 改用备选数据源
2. 在原始时间范围内重新筛选场景并重跑
3. 失败并给出建议

#### H. Explanation Service

输入：

- 推荐结果
- 实际执行参数
- QC 结果
- 统计摘要

输出：

- 方法说明
- 结果解释
- 局限性提示

硬约束：

- 解释文案只能引用实际执行结果，不允许“补写”不存在的步骤。

------

## 6. 接口设计

### 6.1 外部 API

#### 1. 创建会话

`POST /api/v1/sessions`

响应：

```json
{
  "session_id": "ses_001",
  "status": "active"
}
```

#### 2. 上传文件

`POST /api/v1/files`

表单字段：

- `session_id`
- `file`

响应：

```json
{
  "file_id": "file_001",
  "file_type": "geojson",
  "storage_key": "uploads/ses_001/file_001.geojson"
}
```

#### 3. 发送消息并创建任务

`POST /api/v1/messages`

请求：

```json
{
  "session_id": "ses_001",
  "content": "帮我计算 2024 年 6 到 8 月北京西山的 NDVI，并导出地图和 GeoTIFF。",
  "file_ids": []
}
```

响应：

```json
{
  "message_id": "msg_001",
  "task_id": "task_001",
  "task_status": "queued",
  "need_clarification": false
}
```

#### 4. 查询任务详情

`GET /api/v1/tasks/{task_id}`

响应：

```json
{
  "task_id": "task_001",
  "status": "running",
  "current_step": "search_candidates",
  "task_spec": {},
  "recommendation": null,
  "artifacts": []
}
```

#### 5. 查询任务事件流

`GET /api/v1/tasks/{task_id}/events`

返回：

- SSE 或轮询 JSON

事件示例：

```json
{
  "event": "task.step.updated",
  "task_id": "task_001",
  "step": "generate_map",
  "status": "running",
  "message": "正在生成地图"
}
```

#### 6. 重跑任务

`POST /api/v1/tasks/{task_id}/rerun`

请求：

```json
{
  "override": {
    "time_range": {
      "start": "2023-06-01",
      "end": "2023-08-31"
    }
  }
}
```

响应：

```json
{
  "task_id": "task_002",
  "parent_task_id": "task_001",
  "status": "queued"
}
```

#### 7. 下载产物

`GET /api/v1/artifacts/{artifact_id}`

返回：

- 文件流或带时效签名链接

### 6.2 内部服务接口

建议先以 Python service interface 固定契约，再决定是否拆微服务。

```python
class TaskParser:
    def parse(self, message: str, context: dict, files: list[dict]) -> TaskSpec: ...

class AOIService:
    def normalize(self, task_spec: TaskSpec) -> AOIRecord: ...

class CatalogService:
    def search(self, aoi: AOIRecord, time_range: dict, analysis_type: str) -> list[DatasetCandidate]: ...

class RecommendationService:
    def recommend(self, candidates: list[DatasetCandidate], user_priority: str) -> Recommendation: ...

class NDVIWorkflow:
    def run(self, task: TaskRun) -> WorkflowResult: ...
```

------

## 7. 表结构建议

### 7.1 设计原则

- 任务主表保存状态和高频查询字段。
- 大块、变化快、结构不稳定的数据放 `JSONB`。
- 空间对象统一进入 PostGIS。
- 产物落对象存储，表里只保存元数据和路径。

### 7.2 核心表

#### `sessions`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `varchar(32)` PK | 会话 ID |
| `title` | `varchar(255)` | 可空，首条消息摘要 |
| `status` | `varchar(32)` | `active / archived` |
| `created_at` | `timestamptz` | 创建时间 |
| `updated_at` | `timestamptz` | 更新时间 |

索引：

- `idx_sessions_updated_at`

#### `messages`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `varchar(32)` PK | 消息 ID |
| `session_id` | `varchar(32)` FK | 会话 ID |
| `role` | `varchar(16)` | `user / assistant / system` |
| `content` | `text` | 消息内容 |
| `linked_task_id` | `varchar(32)` | 关联任务，可空 |
| `created_at` | `timestamptz` | 创建时间 |

索引：

- `idx_messages_session_created`

#### `uploaded_files`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `varchar(32)` PK | 文件 ID |
| `session_id` | `varchar(32)` FK | 会话 ID |
| `original_name` | `varchar(255)` | 原文件名 |
| `file_type` | `varchar(32)` | `geojson / shp_zip / other` |
| `storage_key` | `varchar(512)` | 对象存储路径 |
| `size_bytes` | `bigint` | 文件大小 |
| `checksum` | `varchar(128)` | 完整性校验 |
| `created_at` | `timestamptz` | 上传时间 |

#### `task_runs`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `varchar(32)` PK | 任务 ID |
| `session_id` | `varchar(32)` FK | 会话 ID |
| `parent_task_id` | `varchar(32)` | 重跑来源，可空 |
| `user_message_id` | `varchar(32)` FK | 触发消息 |
| `status` | `varchar(32)` | 任务状态 |
| `current_step` | `varchar(64)` | 当前步骤 |
| `analysis_type` | `varchar(32)` | 当前仅 `NDVI` |
| `requested_time_range` | `jsonb` | 原始请求时间 |
| `actual_time_range` | `jsonb` | 实际执行时间 |
| `selected_dataset` | `varchar(32)` | 主数据源 |
| `fallback_used` | `boolean` | 是否触发 fallback |
| `error_code` | `varchar(64)` | 错误码 |
| `error_message` | `text` | 错误信息 |
| `duration_seconds` | `integer` | 总耗时 |
| `created_at` | `timestamptz` | 创建时间 |
| `updated_at` | `timestamptz` | 更新时间 |

索引：

- `idx_task_runs_session_created`
- `idx_task_runs_status`
- `idx_task_runs_parent`

#### `task_specs`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | `varchar(32)` PK/FK | 任务 ID |
| `aoi_input` | `text` | 原始 AOI 表达 |
| `aoi_source_type` | `varchar(32)` | `admin_name / alias / geojson / shp_zip` |
| `preferred_output` | `jsonb` | 输出偏好 |
| `user_priority` | `varchar(32)` | `spatial / temporal / balanced` |
| `need_confirmation` | `boolean` | 是否需要澄清 |
| `raw_spec_json` | `jsonb` | 完整解析结果 |

#### `aois`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `varchar(32)` PK | AOI ID |
| `task_id` | `varchar(32)` FK | 任务 ID |
| `source_file_id` | `varchar(32)` | 来源文件，可空 |
| `name` | `varchar(255)` | 标准化名称 |
| `geom` | `geometry(MultiPolygon, 4326)` | AOI 几何 |
| `bbox` | `geometry(Polygon, 4326)` | 边界框 |
| `area_km2` | `numeric(12,2)` | 面积 |
| `is_valid` | `boolean` | 几何是否有效 |
| `validation_message` | `text` | 校验信息 |
| `created_at` | `timestamptz` | 创建时间 |

索引：

- `idx_aois_geom_gist`

#### `dataset_candidates`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `bigserial` PK | 主键 |
| `task_id` | `varchar(32)` FK | 任务 ID |
| `dataset_name` | `varchar(32)` | `sentinel2 / landsat89 / hls` |
| `collection_id` | `varchar(128)` | STAC collection |
| `scene_count` | `integer` | 场景数 |
| `coverage_ratio` | `numeric(5,4)` | 覆盖率 |
| `effective_pixel_ratio_estimate` | `numeric(5,4)` | 有效像元估计 |
| `cloud_metric_summary` | `jsonb` | 云量摘要 |
| `spatial_resolution` | `integer` | 分辨率 |
| `temporal_density_note` | `varchar(32)` | `low / medium / high` |
| `suitability_score` | `numeric(6,4)` | 推荐分数 |
| `recommendation_rank` | `integer` | 1 为首选 |
| `summary_json` | `jsonb` | 扩展信息 |

索引：

- `idx_candidates_task_rank`

#### `task_steps`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `bigserial` PK | 主键 |
| `task_id` | `varchar(32)` FK | 任务 ID |
| `step_name` | `varchar(64)` | 步骤名 |
| `status` | `varchar(32)` | 状态 |
| `started_at` | `timestamptz` | 开始时间 |
| `ended_at` | `timestamptz` | 结束时间 |
| `detail_json` | `jsonb` | 日志细节 |

索引：

- `idx_task_steps_task_started`

#### `artifacts`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `varchar(32)` PK | 产物 ID |
| `task_id` | `varchar(32)` FK | 任务 ID |
| `artifact_type` | `varchar(32)` | `png_map / geotiff / methods_md / summary_md` |
| `storage_key` | `varchar(512)` | 对象存储路径 |
| `mime_type` | `varchar(128)` | 文件类型 |
| `size_bytes` | `bigint` | 文件大小 |
| `checksum` | `varchar(128)` | 哈希 |
| `metadata_json` | `jsonb` | 补充元数据 |
| `created_at` | `timestamptz` | 创建时间 |

------

## 8. 目录结构建议

```text
gis-agent/
  apps/
    web/
    api/
    worker/
  packages/
    domain/
    schemas/
    prompts/
  infra/
    docker/
    migrations/
  tests/
    fixtures/
    integration/
    regression/
  docs/
```

建议约定：

- `apps/api` 负责 HTTP 和任务编排
- `apps/worker` 负责 GIS 实际执行
- `packages/domain` 放数据模型、规则引擎、公共服务
- `packages/schemas` 放 Pydantic 模型

------

## 9. 任务拆解

### 9.1 基础设施与工程化

| 编号 | 任务 | 优先级 | 产出 |
| --- | --- | --- | --- |
| INF-01 | 初始化 monorepo / Python 项目结构 | P0 | 可运行项目骨架 |
| INF-02 | Docker Compose：api / worker / redis / postgres / minio | P0 | 本地一键启动 |
| INF-03 | Alembic / migrations 初始化 | P0 | 可迁移数据库 |
| INF-04 | 统一日志、错误码、配置管理 | P0 | 标准化基础设施 |

### 9.2 应用层

| 编号 | 任务 | 优先级 | 产出 |
| --- | --- | --- | --- |
| APP-01 | 会话和消息模型 | P0 | `sessions` / `messages` API |
| APP-02 | 文件上传与对象存储接入 | P0 | `uploaded_files` 流程 |
| APP-03 | 任务状态机与队列投递 | P0 | `task_runs` 主流程 |
| APP-04 | SSE / 轮询状态推送 | P0 | 前端可见任务状态 |
| APP-05 | 重跑接口 | P0 | `rerun` 功能 |

### 9.3 LLM 与解析

| 编号 | 任务 | 优先级 | 产出 |
| --- | --- | --- | --- |
| NLP-01 | `TaskSpec` schema 定义 | P0 | 结构化解析契约 |
| NLP-02 | 任务解析 prompt 与校验器 | P0 | 可回归测试 |
| NLP-03 | 上下文继承规则 | P0 | 最近任务追问支持 |
| NLP-04 | 推荐理由和结果解释模板 | P0 | 可控说明文本 |

### 9.4 GIS 核心

| 编号 | 任务 | 优先级 | 产出 |
| --- | --- | --- | --- |
| GIS-01 | `geojson` / `shp.zip` ingest | P0 | 文件 AOI 标准化 |
| GIS-02 | 行政区 / 别名解析服务 | P0 | 名称 AOI 标准化 |
| GIS-03 | Sentinel-2 STAC adapter | P0 | 候选摘要 |
| GIS-04 | Landsat 8/9 STAC adapter | P0 | 候选摘要 |
| GIS-05 | 规则推荐引擎 | P0 | 主备方案 |
| GIS-06 | NDVI 中位数合成工作流 | P0 | 主链路执行 |
| GIS-07 | QC 与 fallback | P0 | 可解释失败 |
| GIS-08 | PNG 渲染与 COG 导出 | P0 | 最终产物 |
| GIS-09 | HLS adapter | P1 | 第三数据源 |

### 9.5 前端

| 编号 | 任务 | 优先级 | 产出 |
| --- | --- | --- | --- |
| FE-01 | 对话页骨架 | P0 | 输入区与消息流 |
| FE-02 | 文件上传区 | P0 | AOI 文件上传 |
| FE-03 | 任务状态卡 | P0 | 各步骤实时状态 |
| FE-04 | 结果页 | P0 | 地图、下载、方法说明 |
| FE-05 | 最近任务追问入口 | P0 | 快捷重跑体验 |
| FE-06 | 数据对比卡增强 | P1 | 更丰富推荐展示 |

### 9.6 QA 与验收

| 编号 | 任务 | 优先级 | 产出 |
| --- | --- | --- | --- |
| QA-01 | 样例任务集 20-30 条 | P0 | 固定回归样本 |
| QA-02 | Parser 准确率测试 | P0 | 结构化解析报告 |
| QA-03 | GIS 集成测试 | P0 | 主流程稳定性报告 |
| QA-04 | 失败场景测试 | P0 | fallback 验证 |
| QA-05 | 性能压测 | P0 | 典型任务耗时基线 |

------

## 10. 8 周排期

### 第 1 周：定基线

目标：

- PRD 和技术方案冻结
- 项目骨架、开发环境、数据库迁移初始化
- 20-30 条样例任务集确定

DoD：

- 本地 `docker compose up` 可启动
- 核心表迁移可执行
- `TaskSpec` schema 冻结

### 第 2 周：应用骨架

目标：

- 会话、消息、文件上传 API
- 任务状态机
- 基础任务创建与查询接口

DoD：

- 能创建 session
- 能上传 AOI 文件
- 能创建 `queued` 任务并回查状态

### 第 3 周：AOI 与数据搜索

目标：

- `geojson` / `shp.zip` ingest
- 行政区 / 别名解析
- Sentinel-2 / Landsat 检索打通

DoD：

- 样例 AOI 可标准化
- 两类数据源均可返回统一候选摘要
- 失败时有结构化错误

### 第 4 周：推荐与任务解析

目标：

- 任务解析 prompt + validator
- 上下文继承
- 规则推荐引擎

DoD：

- 标准任务可产出完整 `TaskSpec`
- “换成 2023 年”类请求能继承上下文
- 推荐输出有主备与理由

### 第 5 周：NDVI 主链路

目标：

- NDVI 计算
- 中位数合成
- GeoTIFF/COG 导出
- 基础 PNG 渲染

DoD：

- 标准成功用例跑通端到端
- 输出值域合理
- GeoTIFF 可在 QGIS 打开

### 第 6 周：质量检查与解释

目标：

- QC 阈值
- fallback
- 方法说明与结果解释
- 任务步骤日志

DoD：

- 不可用结果不会误报成功
- fallback 有可读记录
- 方法说明与执行参数一致

### 第 7 周：前端联调

目标：

- 对话页
- 状态卡
- 结果页
- 下载与重跑

DoD：

- 用户能完成一次完整任务
- 状态变化前端可见
- 最近任务追问可触发重跑

### 第 8 周：验收与内测准备

目标：

- 回归测试
- 性能调优
- 内测部署
- 运行手册

DoD：

- 20-30 条样例任务完成回归
- 关键已知问题收敛
- 内测环境可稳定演示

------

## 11. 风险与工程应对

### 11.1 地名解析不稳定

应对：

- 首发只保证行政区名称和已配置别名
- 模糊地名强制澄清
- 文件上传始终作为最可靠路径

### 11.2 外部数据源不稳定

应对：

- Catalog adapter 做超时和重试
- 搜索结果结构化缓存
- 单数据源失败不拖垮整体任务

### 11.3 运行过慢

应对：

- 控制 AOI 和时间窗
- 所有计算走异步
- 先保证正确性，再做并行优化

### 11.4 解释与执行脱节

应对：

- 说明文本只消费 `task_runs`、`dataset_candidates`、`task_steps`、`artifacts`
- 禁止解释模块读取原始自由文本后自行补全未执行步骤

------

## 12. 开工建议

建议第一天就冻结三件事：

1. `TaskSpec`、`DatasetCandidate`、`RunRecord` 的 schema
2. P0 只做 Sentinel-2 + Landsat 8/9 + median
3. 20-30 条标准回归任务集

如果这三件事不先冻结，后续研发会不断被“再加一个数据源 / 再加一种合成 / 再加一个交互”拖慢。
