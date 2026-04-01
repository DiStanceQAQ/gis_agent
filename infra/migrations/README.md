# Migrations

当前项目已经切到 Alembic 管理数据库变更，迁移脚本位于 `infra/migrations/versions/`。

当前约定：

1. 首个基线迁移显式创建 `postgis` extension
2. 后续所有表结构变更只能通过 migration
3. API 启动时会自动执行 `upgrade head`
4. 旧的 `create_all()` 骨架数据库会在启动时自动 `stamp` 到基线版本

常用命令：

```bash
alembic upgrade head
alembic current
alembic revision -m "describe change"
```
