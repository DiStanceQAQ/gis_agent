# GIS Agent

GIS Agent 的第一版工程骨架。当前这套代码优先解决三件事：

- 按照 PRD 把前后端、Worker、共享模型和本地运行方式搭起来
- 固定 API 契约、任务状态机和数据模型，减少后续返工
- 用一个可运行的 baseline pipeline 跑通任务创建、状态推进和结果展示

## 当前状态

这不是完整的 GIS 实现，当前默认是 `celery_agent` 执行模式，数据库默认已切到 PostgreSQL / PostGIS：

- 会创建会话、消息、任务、步骤、产物记录
- 会生成 baseline 的 PNG、GeoTIFF、方法说明和结果摘要
- 会输出 Sentinel-2 / Landsat 的候选对比和推荐结果
- 前端结果页已支持主推 / 备选数据源的细粒度对比卡，可直接查看云量、景数、分辨率和目录来源
- AOI 已改为使用 PostGIS 几何字段存储，并支持“已配置 AOI 注册表”里的行政区名 / 别名确定性解析
- 仓库里已经带有真实 STAC 检索、AOI 标准化和 NDVI pipeline，默认会优先尝试真实链路

当前推荐按这份执行清单推进开发：

- [开发任务清单.md](/Users/ljn/gis_agent/开发任务清单.md)

## 目录

```text
apps/
  api/        FastAPI 应用
  worker/     Celery worker
  web/        React + Vite 前端
packages/
  domain/     配置、数据库模型、服务层
  schemas/    API / 领域 schema
infra/
  docker/     Dockerfile
```

## Docker 启动

默认推荐直接用 Docker Compose。当前默认启动链路是：

- `postgres`
- `redis`
- `minio`
- `api`
- `worker`
- `web`

这条链路默认走 Celery 队列执行，任务创建会先入队，再由 `worker` 推进状态和产物生成。当前 Docker 开发默认也会把产物和上传文件写入 MinIO。

当前默认镜像源使用 `docker.1panel.live` 代理，以适配 Docker Hub / GHCR 拉取较慢的网络环境。
Python 依赖默认走清华 PyPI，前端依赖默认走 `npmmirror`。
如果你的网络直连官方仓库更快，可以在启动前覆盖这些变量：

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

### 1. 启动默认服务

```bash
cp .env.example .env
docker compose up -d --build
```

### 2. 查看状态

```bash
docker compose ps
docker compose logs -f api worker web
```

### 3. 访问地址

- 前端: `http://localhost:5173`
- API 健康检查: `http://localhost:8000/api/v1/health`
- MinIO Console: `http://localhost:9001`

## 本地开发

### 1. Python 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### 2. 启动 PostgreSQL / PostGIS

本地直接起 API 之前，先保证数据库已启动：

```bash
docker compose up -d postgres minio
```

如果你本机直跑 API 时不想启 MinIO，也可以在启动前切回本地文件存储：

```bash
export GIS_AGENT_STORAGE_BACKEND=local
```

### 3. 前端依赖

```bash
npm install
```

### 4. 启动 API

```bash
uvicorn apps.api.main:app --reload
```

首次启动时，应用会自动执行：

- `alembic upgrade head`
- 首个基线迁移里创建 `postgis` extension
- 如果数据库来自旧版 `create_all()` 骨架，会自动 `stamp` 到基线版本

### 5. 启动前端

```bash
npm --workspace apps/web run dev
```

### 6. 启动 Worker

本地直接跑服务时，默认模式已经是 `celery_agent`，需要同时起 Worker：

```bash
celery -A apps.worker.celery_app.celery_app worker --loglevel=info
```

### 7. 真实目录搜索与真实 NDVI

默认配置会优先开启 live catalog 和 real pipeline；如需要临时切回 baseline 路径：

```bash
export GIS_AGENT_CATALOG_LIVE_SEARCH=false
export GIS_AGENT_REAL_PIPELINE_ENABLED=false
```

### 8. 配置本地 AOI 注册表

仓库默认带了一份示例注册表：

- [aoi_registry.json](/Users/ljn/gis_agent/packages/domain/data/aoi_registry.json)

默认会优先读取这份文件；如果你要切到自己的区域库，可以在 `.env` 里设置：

```bash
export GIS_AGENT_AOI_REGISTRY_PATH=/absolute/path/to/aoi_registry.json
```

注册表里的每条记录至少需要：

- `name`
- `source_type`
- `bbox` 或 `geometry`

`aliases` 是可选的。当前策略是“命中注册表就直接解析，没命中就要求澄清”，不会再静默猜测自然地名。

### 9. 配置 LLM 基础参数

当前仓库已经补齐 LLM 客户端与配置项（后续会接入 parser / planner / recommendation 主链）：

```bash
export GIS_AGENT_LLM_PROVIDER=openai_compatible
export GIS_AGENT_LLM_BASE_URL=https://api.openai.com/v1
export GIS_AGENT_LLM_API_KEY=<your_api_key>
export GIS_AGENT_LLM_MODEL=gpt-4o-mini
export GIS_AGENT_LLM_TIMEOUT_SECONDS=60
export GIS_AGENT_LLM_MAX_RETRIES=2
export GIS_AGENT_LLM_TEMPERATURE=0.2
export GIS_AGENT_AGENT_MAX_STEPS=12
export GIS_AGENT_AGENT_MAX_TOOL_CALLS=24
```

建议把这些变量写入 `.env`，并按你实际供应商调整 `GIS_AGENT_LLM_BASE_URL` 与 `GIS_AGENT_LLM_MODEL`。

## Docker Compose

```bash
docker compose up -d --build
```

默认会拉起：

- `postgres`
- `redis`
- `minio`
- `api`
- `worker`
- `web`

## 检查与 CI

仓库现在已经带了基础 CI：

- [ci.yml](/Users/ljn/gis_agent/.github/workflows/ci.yml)

GitHub Actions 里会跑两段：

- 后端：`python -m compileall apps packages tests`、`ruff check apps packages tests`、`pytest -q tests`
- 前端：`npm run check:web`

如果你继续用 Docker 开发，最稳的本地检查方式是：

```bash
docker exec gis_agent-api-1 python -m compileall apps packages tests
docker exec gis_agent-api-1 ruff check apps packages tests
docker exec gis_agent-api-1 pytest -q tests
npm run check:web
```

如果你切到本机虚拟环境，也可以直接跑：

```bash
npm run check:py
npm run check:web
```

当前也已经补了第一批端到端任务样例夹具：

- [tests/fixtures/tasks/task_suite.json](/Users/ljn/gis_agent/tests/fixtures/tasks/task_suite.json)
- [tests/fixtures/tasks/fallback_real_pipeline_to_baseline.json](/Users/ljn/gis_agent/tests/fixtures/tasks/fallback_real_pipeline_to_baseline.json)
- [tests/fixtures/tasks/failure_generate_outputs.json](/Users/ljn/gis_agent/tests/fixtures/tasks/failure_generate_outputs.json)

对应回归在：

- [test_task_samples.py](/Users/ljn/gis_agent/tests/test_task_samples.py)
- [test_ndvi_pipeline.py](/Users/ljn/gis_agent/tests/test_ndvi_pipeline.py)

当前样例总数已经到 20 条，覆盖：

- 成功任务
- 失败任务
- fallback
- 追问继承
- 导出 / 重跑

当前也已经记录了第一版性能基线：

- [docs/perf-baseline.md](/Users/ljn/gis_agent/docs/perf-baseline.md)

当前存储层也已经抽象成统一接口：

- Docker 开发默认：`GIS_AGENT_STORAGE_BACKEND=s3`
- 本机直跑 API 时，如不启 MinIO，可手动切回 `GIS_AGENT_STORAGE_BACKEND=local`
- Docker 开发默认是 `GIS_AGENT_CATALOG_LIVE_SEARCH=true` 和 `GIS_AGENT_REAL_PIPELINE_ENABLED=true`

如果要按默认的 MinIO 路径启动，直接执行：

```bash
docker compose up -d --build
```

相关环境变量可参考：

- [\.env.example](/Users/ljn/gis_agent/.env.example)

## 下一步建议

1. 把 AOI 注册表从示例扩成真实业务区域库
2. 接入 HLS，补第三数据源到同一套候选与推荐框架
3. 继续扩样例集和性能基线
