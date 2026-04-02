from __future__ import annotations

from pydantic import BaseModel, Field

from packages.domain.errors import ErrorCode


class ToolContract(BaseModel):
    input_schema: dict[str, object] = Field(default_factory=dict)
    output_schema: dict[str, object] = Field(default_factory=dict)
    error_codes: list[str] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    step_name: str
    tool_name: str
    title: str
    purpose: str
    reasoning: str
    depends_on: list[str] = Field(default_factory=list)
    contract: ToolContract = Field(default_factory=ToolContract)


TOOL_STEP_SEQUENCE = [
    "plan_task",
    "normalize_aoi",
    "search_candidates",
    "recommend_dataset",
    "run_ndvi_pipeline",
    "generate_outputs",
]


TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    "plan_task": ToolDefinition(
        step_name="plan_task",
        tool_name="planner.build",
        title="规划任务",
        purpose="把自然语言需求转成可执行的 GIS 工具计划。",
        reasoning="先显式规划，后续执行和解释才有统一依据。",
        depends_on=[],
        contract=ToolContract(
            input_schema={
                "task_spec": "ParsedTaskSpec",
            },
            output_schema={
                "plan_status": "str",
                "objective": "str",
                "tool_count": "int",
            },
        ),
    ),
    "normalize_aoi": ToolDefinition(
        step_name="normalize_aoi",
        tool_name="aoi.normalize",
        title="标准化研究区",
        purpose="把 bbox、文件边界或名称输入统一成可执行 AOI。",
        reasoning="AOI 必须先稳定，后续目录搜索和裁剪才可靠。",
        depends_on=["plan_task"],
        contract=ToolContract(
            input_schema={
                "aoi_input": "str | None",
                "aoi_source_type": "str | None",
            },
            output_schema={
                "bbox": "list[float]",
                "aoi_name": "str | None",
                "aoi_area_km2": "float | None",
            },
            error_codes=[
                ErrorCode.AOI_PARSE_FAILED,
                ErrorCode.AOI_INVALID_BBOX,
            ],
        ),
    ),
    "search_candidates": ToolDefinition(
        step_name="search_candidates",
        tool_name="catalog.search",
        title="搜索候选数据",
        purpose="针对 AOI 和时间窗检索 Sentinel、Landsat 等候选集。",
        reasoning="先拿到候选目录摘要，后面才能做质量判断和推荐。",
        depends_on=["normalize_aoi"],
        contract=ToolContract(
            input_schema={
                "aoi": "AOIRecord",
                "time_range": "dict[str, str]",
                "requested_dataset": "str | None",
            },
            output_schema={
                "candidate_count": "int",
                "collections": "list[str]",
                "qc": "dict[str, object]",
            },
        ),
    ),
    "recommend_dataset": ToolDefinition(
        step_name="recommend_dataset",
        tool_name="recommendation.rank",
        title="推荐数据源",
        purpose="综合用户偏好和候选质量，输出主备方案排序。",
        reasoning="推荐层负责把目录事实转成可解释的执行选择。",
        depends_on=["search_candidates"],
        contract=ToolContract(
            input_schema={
                "candidates": "list[CatalogCandidate]",
                "user_priority": "str",
                "requested_dataset": "str | None",
            },
            output_schema={
                "primary_dataset": "str",
                "backup_dataset": "str | None",
                "scores": "dict[str, float]",
                "reason": "str",
                "risk_note": "str | None",
            },
        ),
    ),
    "run_ndvi_pipeline": ToolDefinition(
        step_name="run_ndvi_pipeline",
        tool_name="ndvi.run",
        title="执行分析",
        purpose="执行裁剪、质量过滤、合成和 NDVI 计算。",
        reasoning="这是核心 GIS 处理步骤，直接决定结果质量。",
        depends_on=["recommend_dataset"],
        contract=ToolContract(
            input_schema={
                "selected_dataset": "str",
                "aoi": "AOIRecord",
                "task_spec": "ParsedTaskSpec",
            },
            output_schema={
                "mode": "str",
                "selected_item_ids": "list[str]",
                "valid_pixel_ratio": "float",
                "fallback_used": "bool",
            },
            error_codes=[
                ErrorCode.TASK_RUNTIME_TIMEOUT,
            ],
        ),
    ),
    "generate_outputs": ToolDefinition(
        step_name="generate_outputs",
        tool_name="artifacts.publish",
        title="发布结果",
        purpose="生成 PNG、GeoTIFF 和说明文本，并发布到工作台。",
        reasoning="结果必须可下载、可预览、可解释，才算完成任务。",
        depends_on=["run_ndvi_pipeline"],
        contract=ToolContract(
            input_schema={
                "pipeline_outputs": "dict[str, object]",
                "recommendation": "dict[str, object] | None",
            },
            output_schema={
                "artifact_types": "list[str]",
                "duration_seconds": "int",
                "mode": "str",
            },
            error_codes=[
                ErrorCode.ARTIFACT_STORAGE_MISSING,
            ],
        ),
    ),
}


def get_tool_definition(step_name: str) -> ToolDefinition | None:
    return TOOL_DEFINITIONS.get(step_name)


def require_tool_definition(step_name: str) -> ToolDefinition:
    tool = get_tool_definition(step_name)
    if tool is None:
        raise KeyError(step_name)
    return tool


def list_tool_definitions() -> list[ToolDefinition]:
    return [TOOL_DEFINITIONS[step_name] for step_name in TOOL_STEP_SEQUENCE if step_name in TOOL_DEFINITIONS]
