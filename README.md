# GIS Agent

GIS Agent 是一个面向自然语言的 GIS 分析工作台。它把用户在聊天框里的需求收敛成结构化任务，完成 AOI 解析、数据源检索与推荐、GIS 处理链执行，并把 PNG 地图、GeoTIFF、方法说明和结果摘要等产物发布出来。

当前仓库是 MVP 工程骨架和可运行 baseline，不是完整生产级 GIS 平台。默认运行方式是 FastAPI + Celery Worker + PostgreSQL/PostGIS + Redis + MinIO，前端使用 React + Vite。

## 当前能力

- 会话、消息、文件上传、任务、步骤、事件和产物的完整记录。
- PostgreSQL/PostGIS 持久化 AOI 几何、任务状态、revision、understanding 和 LLM 调用日志。
- 基于 LangGraph 的固定任务管道：`parse -> plan -> normalize_aoi -> search_candidates -> recommend_dataset -> run_processing_pipeline -> generate_outputs`。
- 支持 Sentinel-2 / Landsat 8/9 候选检索、目录级评分、主备推荐和前端对比卡展示。
- 支持 AOI 注册表的行政区名 / 别名确定性解析，未命中时进入澄清，不再静默猜测。
- 支持通用 GIS operation plan，当前注册了 raster、vector、artifact 等操作节点。
- 默认可连接真实 STAC 检索和真实处理链路，也可以切回 baseline / local-only 路径做开发验证。

## 架构概览

```text
apps/
  api/        FastAPI 应用与 API routers
  worker/     Celery worker
  web/        React + Vite 前端工作台
packages/
  domain/     配置、数据库模型、服务层、LangGraph runtime、GIS pipeline
  schemas/    API / 领域 schema
infra/
  docker/     API / Worker / Web 镜像
  migrations/ Alembic 迁移
tests/        后端单元测试与 API 回归测试
```

Runtime 不是自由循环选工具的 agent。当前 ReAct 决策发生在固定 LangGraph step 内，只负责每一步的 `continue`、`skip`、`fail` 判断；真正的 GIS 操作链在 `run_processing_pipeline` 中按 operation plan 执行。

任务状态主要包括 `draft`、`queued`、`running`、`success`、`failed`、`waiting_clarification`、`awaiting_approval` 和 `cancelled`。审计事件分为 LangGraph step 级别和 operation node 级别，便于定位失败发生在编排层还是具体处理节点。

## 快速启动

推荐先用 Docker Compose 跑完整链路。

```bash
cp .env.example .env
docker compose up -d --build
```

查看服务状态和日志：

```bash
docker compose ps
docker compose logs -f api worker web
```

访问地址：

- 前端工作台: `http://localhost:5173`
- API 健康检查: `http://localhost:8000/api/v1/health`
- MinIO Console: `http://localhost:9001`

默认 Compose 会启动：

- `postgres`: PostgreSQL + PostGIS
- `redis`: Celery broker / result backend
- `minio`: S3 兼容对象存储
- `api`: FastAPI
- `worker`: Celery worker
- `web`: Vite dev server

首次 API 启动会自动执行 `alembic upgrade head`。S3 模式下，应用会在启动时检查并创建默认 bucket。

## 本地开发

### 后端环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### 前端依赖

```bash
npm install
```

### 启动基础设施

本机直接跑 API / Worker 时，仍然建议用 Compose 启动数据库、队列和对象存储：

```bash
docker compose up -d postgres redis minio
```

如果继续使用 MinIO，把 `.env` 或当前 shell 里的 endpoint 改成本机可访问地址：

```bash
export GIS_AGENT_S3_ENDPOINT_URL=http://localhost:9000
```

如果不想在本地开发时使用 MinIO，可以切到本地文件存储：

```bash
export GIS_AGENT_STORAGE_BACKEND=local
```

### 启动服务

分别打开三个终端：

```bash
uvicorn apps.api.main:app --reload
```

```bash
celery -A apps.worker.celery_app.celery_app worker --loglevel=info
```

```bash
npm run dev:web
```

前端默认通过 Vite proxy 访问 `http://localhost:8000`。如果 API 地址不同，可以设置：

```bash
export VITE_PROXY_TARGET=http://localhost:8000
```

## 关键配置

常用配置都在 [.env.example](.env.example) 里。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `GIS_AGENT_EXECUTION_MODE` | `celery_agent` | 任务先入队，再由 Worker 执行 |
| `GIS_AGENT_STORAGE_BACKEND` | `s3` | `s3` 使用 MinIO，`local` 使用本地 `.data` |
| `GIS_AGENT_CATALOG_LIVE_SEARCH` | `true` | 是否优先使用真实 STAC 检索 |
| `GIS_AGENT_CATALOG_ALLOW_BASELINE_FALLBACK` | `true` | 真实检索失败时是否回退 baseline catalog |
| `GIS_AGENT_REAL_PIPELINE_ENABLED` | `true` | 是否启用真实 GIS 处理链 |
| `GIS_AGENT_LOCAL_FILES_ONLY_MODE` | `false` | 是否只处理本地上传文件，不跑远程目录搜索 |
| `GIS_AGENT_AOI_REGISTRY_PATH` | 空 | 自定义 AOI 注册表路径 |
| `GIS_AGENT_LLM_API_KEY` | 空 | OpenAI-compatible LLM API key |

`.env.example` 默认开启 LLM parser / planner / recommendation。要跑自然语言主链，请配置：

```bash
export GIS_AGENT_LLM_PROVIDER=openai_compatible
export GIS_AGENT_LLM_BASE_URL=https://api.openai.com/v1
export GIS_AGENT_LLM_API_KEY=<your_api_key>
export GIS_AGENT_LLM_MODEL=gpt-4o-mini
```

如果只是本地验证 deterministic fallback，可以临时关闭对应 LLM 阶段：

```bash
export GIS_AGENT_LLM_PARSER_ENABLED=false
export GIS_AGENT_LLM_PLANNER_ENABLED=false
export GIS_AGENT_LLM_RECOMMENDATION_ENABLED=false
```

## AOI 注册表

仓库默认带有示例注册表：

- [packages/domain/data/aoi_registry.json](packages/domain/data/aoi_registry.json)

每条记录至少需要：

- `name`
- `source_type`
- `bbox` 或 `geometry`

`aliases` 可选。命中注册表时系统会直接解析 AOI；没命中时会要求用户补充信息。

## 常用命令

后端检查：

```bash
npm run check:py
```

前端检查：

```bash
npm run check:web
```

全部检查：

```bash
npm run check
```

Docker 容器内检查：

```bash
docker exec gis_agent-api-1 python -m compileall apps packages tests
docker exec gis_agent-api-1 ruff check apps packages tests
docker exec gis_agent-api-1 pytest -q tests
npm run check:web
```

## 镜像与网络

默认镜像源使用 `docker.1panel.live`，Python 依赖默认走清华 PyPI，前端依赖默认走 `npmmirror`。如果你的网络直连官方源更快，可以在启动前覆盖：

```bash
export POSTGRES_IMAGE=postgis/postgis:16-3.4
export REDIS_IMAGE=redis:7
export MINIO_IMAGE=minio/minio:latest
export PYTHON_BASE_IMAGE=python:3.12-slim
export WEB_BASE_IMAGE=node:24-alpine
export APT_MIRROR_HOST=deb.debian.org
export PIP_INDEX_URL=https://pypi.org/simple
export PIP_TRUSTED_HOST=pypi.org
export NPM_REGISTRY=https://registry.npmjs.org
```

## 参考文档

- [GIS Agent MVP PRD.md](GIS%20Agent%20MVP%20PRD.md)
- [GIS Agent MVP 技术方案与任务拆解.md](GIS%20Agent%20MVP%20技术方案与任务拆解.md)
- [开发任务清单.md](开发任务清单.md)
- [infra/migrations/README.md](infra/migrations/README.md)
- [.github/workflows/ci.yml](.github/workflows/ci.yml)
