# Migrations

当前骨架阶段使用 `Base.metadata.create_all()` 快速启动数据库。

当真实业务模型稳定后，建议把数据库变更正式迁到 Alembic：

1. 初始化 Alembic 配置
2. 生成首个基线迁移
3. 后续所有表结构变更走 migration

在切换到 PostgreSQL / PostGIS 作为默认存储前，这个目录应补齐正式迁移文件。

