# GIS Agent

GIS Agent 的第一版工程骨架。当前这套代码优先解决三件事：

- 按照 PRD 把前后端、Worker、共享模型和本地运行方式搭起来
- 固定 API 契约、任务状态机和数据模型，减少后续返工
- 用一个可运行的 mock pipeline 跑通任务创建、状态推进和结果展示

## 当前状态

这不是完整的 GIS 实现，当前默认是 `inline_mock` 执行模式：

- 会创建会话、消息、任务、步骤、产物记录
- 会生成 mock 的 PNG、GeoTIFF、方法说明和结果摘要
- 会输出 Sentinel-2 / Landsat 的候选对比和推荐结果
- 真实的 STAC 检索、AOI 标准化和 NDVI 流水线仍需继续替换 mock 服务

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

## 本地开发

### 1. Python 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### 2. 前端依赖

```bash
npm install
```

### 3. 启动 API

```bash
uvicorn apps.api.main:app --reload
```

### 4. 启动前端

```bash
npm --workspace apps/web run dev
```

### 5. 启动 Worker（可选）

默认模式是 `inline_mock`，不需要 Celery。若要改成异步模式：

```bash
export GIS_AGENT_EXECUTION_MODE=celery_mock
celery -A apps.worker.celery_app.celery_app worker --loglevel=info
```

## Docker Compose

```bash
docker compose up --build
```

Compose 默认会拉起：

- `postgres`
- `redis`
- `minio`
- `api`
- `worker`
- `web`

## 下一步建议

1. 用真实的 `TaskParser` 替换当前 regex parser
2. 接入真实 AOI 标准化服务
3. 接入 STAC 检索和规则推荐
4. 用真实 NDVI 工作流替换 mock artifact 生成
5. 把 SQLite 开发模式迁到 PostgreSQL / PostGIS 持久化

