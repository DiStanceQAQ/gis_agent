# GIS Agent 性能与成本基线

更新时间：2026-04-03

这份文档记录 LangGraph 主链在接入 DeepSeek API 后的本地基线。数字用于回归对比，不代表 SLA。

## 环境说明

- 运行方式：本地 `uv run` 同步执行（`GIS_AGENT_EXECUTION_MODE=sync`）。
- LLM 配置：`openai_compatible` + `deepseek-chat`（`GIS_AGENT_LLM_BASE_URL=https://api.deepseek.com/v1`）。
- 运行开关：`GIS_AGENT_REAL_PIPELINE_ENABLED=false`、`GIS_AGENT_CATALOG_LIVE_SEARCH=false`、`GIS_AGENT_STORAGE_BACKEND=local`。
- 典型 AOI：`bbox(116.1,39.8,116.5,40.1)`。
- 计量口径：
  - 耗时：`create_message_and_task` 的 wall time + `task.duration_seconds`（runtime 段）。
  - token：按 `llm_call_logs` 的 ID 增量统计，覆盖 `parse/plan/recommend`。

## 基线样本（3 组）

| 样本 | 任务 ID | 分析类型 | 状态 | runtime 秒 | wall 秒 | LLM 调用 | 输入 tokens | 输出 tokens | 总 tokens |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NDVI | `task_53e032e31f52` | `NDVI` | `success` | 8 | 51.414 | 5 | 4103 | 1627 | 5730 |
| NDWI | `task_f73a67b07340` | `NDWI` | `success` | 8 | 51.903 | 5 | 4103 | 1733 | 5836 |
| BUFFER | `task_d13c0ecdbc99` | `BUFFER` | `success` | 8 | 51.616 | 5 | 4189 | 1717 | 5906 |

## 分阶段成本（总 tokens）

| 样本 | parse | plan | recommend |
| --- | ---: | ---: | ---: |
| NDVI | 1963 | 2908 | 859 |
| NDWI | 1963 | 2989 | 884 |
| BUFFER | 2060 | 2985 | 861 |

## 当前观察

- 在 mock pipeline 条件下，端到端 wall time 基本稳定在 `~52s`，主要耗时来自 5 次 LLM 调用往返。
- 计划阶段成本最高（约占总 token 的 50%），当前链路会经历两次 `plan` 调用。
- BUFFER 的解析 token 略高于 NDVI/NDWI，来自更长输入与结构化参数抽取。

## 回归阈值建议（当前基线）

- wall time 稳定超过 `70s`：优先排查模型延迟与重试次数。
- 单任务总 tokens 稳定超过 `7000`：优先排查 parser/planner 的修复回合是否异常增加。
- `plan` 阶段占比持续高于 `60%`：优先评估减少重复规划调用。
