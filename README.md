# GIS Agent

GIS Agent 的第一版工程骨架。当前这套代码优先解决三件事：

- 按照 PRD 把前后端、Worker、共享模型和本地运行方式搭起来
- 固定 API 契约、任务状态机和数据模型，减少后续返工
- 用一个可运行的 mock pipeline 跑通任务创建、状态推进和结果展示

## 当前状态

这不是完整的 GIS 实现，当前默认是 `inline_mock` 执行模式，数据库默认已切到 PostgreSQL / PostGIS：

- 会创建会话、消息、任务、步骤、产物记录
- 会生成 mock 的 PNG、GeoTIFF、方法说明和结果摘要
- 会输出 Sentinel-2 / Landsat 的候选对比和推荐结果
- AOI 已改为使用 PostGIS 几何字段存储，并支持“已配置 AOI 注册表”里的行政区名 / 别名确定性解析
- 仓库里已经带有真实 STAC 检索、AOI 标准化和 NDVI pipeline，但默认仍保持关闭，需要显式开启

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
- `api`
- `web`

这条链路不依赖 Redis / Worker / MinIO，足够跑通当前的 `inline_mock` 工程骨架。

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
docker compose --profile async --profile storage up -d --build
```

### 2. 查看状态

```bash
docker compose ps
docker compose logs -f api web
```

### 3. 访问地址

- 前端: `http://localhost:5173`
- API 健康检查: `http://localhost:8000/api/v1/health`

### 4. 可选服务

如果后面要切到异步任务模式，再额外启动：

```bash
docker compose --profile async up -d redis worker
```

如果后面要补对象存储，再额外启动：

```bash
docker compose --profile storage up -d minio
```

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
docker compose up -d postgres
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

### 6. 启动 Worker（可选）

默认模式是 `inline_mock`，不需要 Celery。若要改成异步模式：

```bash
export GIS_AGENT_EXECUTION_MODE=celery_mock
celery -A apps.worker.celery_app.celery_app worker --loglevel=info
```

### 7. 开启真实目录搜索与真实 NDVI

默认配置为了本地开发稳定性，关闭了 live catalog 和 real pipeline。要显式开启时：

```bash
export GIS_AGENT_CATALOG_LIVE_SEARCH=true
export GIS_AGENT_REAL_PIPELINE_ENABLED=true
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

## Docker Compose

```bash
docker compose up -d --build
```

默认会拉起：

- `postgres`
- `api`
- `web`

按 profile 启动的可选服务：

- `redis`
- `worker`
- `minio`

## 下一步建议

1. 把 `TaskParser` 从基础 regex 继续收口成可继承上下文的结构化解析器
2. 把 AOI 注册表从示例扩成真实业务区域库
3. 新建 QC / fallback 模块，把失败和降级显式记录下来
4. 把本地文件存储抽象成可切换 MinIO / S3 的后端
5. 补上 CI、样例集和性能基线
