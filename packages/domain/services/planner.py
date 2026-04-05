from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode
from packages.domain.logging import get_logger
from packages.domain.services.llm_client import LLMClient, LLMClientError
from packages.domain.services.operation_registry import list_operation_specs
from packages.domain.services.plan_guard import validate_operation_plan
from packages.domain.services.tool_registry import list_tool_definitions
from packages.domain.services.task_state import STEP_SEQUENCE, STEP_STATUS_FAILED, STEP_STATUS_PENDING
from packages.schemas.operation_plan import OperationNode, OperationPlan
from packages.schemas.agent import LLMTaskPlan
from packages.schemas.task import ParsedTaskSpec

logger = get_logger(__name__)

PLAN_STATUS_READY = "ready"
PLAN_STATUS_NEEDS_CLARIFICATION = "needs_clarification"
PLAN_STATUS_RUNNING = "running"
PLAN_STATUS_SUCCESS = "success"
PLAN_STATUS_FAILED = "failed"

_STEP_NAME_ALIASES = {
    "run_ndvi_pipeline": "run_processing_pipeline",
}


class TaskPlanStep(BaseModel):
    step_name: str
    tool_name: str
    title: str
    purpose: str
    status: str = STEP_STATUS_PENDING
    reasoning: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    detail: dict[str, object] | None = None


class TaskPlan(BaseModel):
    version: str = "agent-v1"
    mode: str = "agent_driven_gis_workspace"
    status: str = PLAN_STATUS_READY
    objective: str
    reasoning_summary: str
    missing_fields: list[str] = Field(default_factory=list)
    steps: list[TaskPlanStep] = Field(default_factory=list)
    operation_plan_nodes: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


def _format_time_range(time_range: dict[str, str] | None) -> str:
    if not time_range:
        return "未指定时间范围"
    start = time_range.get("start") or "未知开始"
    end = time_range.get("end") or "未知结束"
    return f"{start} 到 {end}"


def _build_objective(parsed: ParsedTaskSpec) -> str:
    if parsed.analysis_type == "CLIP":
        source_path = str(parsed.operation_params.get("source_path") or "输入栅格")
        clip_path = str(parsed.operation_params.get("clip_path") or "裁剪范围")
        return f"将 {source_path} 按 {clip_path} 执行栅格裁剪并发布可下载结果。"

    aoi_text = parsed.aoi_input or "当前研究区"
    time_text = _format_time_range(parsed.time_range)
    dataset_text = parsed.requested_dataset or "自动选择数据源"
    return f"在 {aoi_text} 范围内，对 {time_text} 的 {parsed.analysis_type} 任务进行自动处理，并以 {dataset_text} 完成结果发布。"


def _build_reasoning_summary(parsed: ParsedTaskSpec) -> str:
    if parsed.analysis_type == "CLIP":
        summary_parts = [
            "先解析输入栅格与裁剪范围路径",
            "再生成可审批的操作计划并执行栅格裁剪与结果导出",
        ]
        if parsed.need_confirmation:
            summary_parts.append("当前参数不完整，计划会先停在待澄清状态")
        return "；".join(summary_parts) + "。"

    summary_parts = [
        "先把自然语言请求收敛为结构化任务",
        "再按 AOI、目录搜索、规则推荐、栅格处理和结果发布执行",
    ]
    if parsed.requested_dataset:
        summary_parts.append(f"优先尊重用户指定的数据源 {parsed.requested_dataset}")
    if parsed.need_confirmation:
        summary_parts.append("当前信息不完整，计划会先停在待澄清状态")
    return "；".join(summary_parts) + "。"


def _build_steps_legacy(parsed: ParsedTaskSpec) -> list[TaskPlanStep]:
    requested_dataset = parsed.requested_dataset or "auto"
    tool_definitions = {definition.step_name: definition for definition in list_tool_definitions()}

    steps: list[TaskPlanStep] = []
    for step_name in STEP_SEQUENCE:
        definition = tool_definitions.get(step_name)
        if definition is None:
            continue

        purpose = definition.purpose
        if step_name == "recommend_dataset":
            purpose = f"综合用户偏好和候选质量，对 {requested_dataset} 请求进行主备方案排序。"

        steps.append(
            TaskPlanStep(
                step_name=definition.step_name,
                tool_name=definition.tool_name,
                title=definition.title,
                purpose=purpose,
                reasoning=definition.reasoning,
                depends_on=definition.depends_on,
            )
        )
    return steps


def _build_task_plan_legacy(parsed: ParsedTaskSpec) -> TaskPlan:
    status = PLAN_STATUS_NEEDS_CLARIFICATION if parsed.need_confirmation else PLAN_STATUS_READY
    return TaskPlan(
        status=status,
        objective=_build_objective(parsed),
        reasoning_summary=_build_reasoning_summary(parsed),
        missing_fields=list(parsed.missing_fields),
        steps=_build_steps_legacy(parsed),
    )


def _map_planner_failure_reason(reason: str) -> tuple[str, str]:
    if reason.startswith("schema_validation_failed"):
        return (
            ErrorCode.TASK_LLM_PLANNER_SCHEMA_VALIDATION_FAILED,
            "Planner LLM 输出未通过 schema 校验，任务进入待澄清状态。",
        )
    return (
        ErrorCode.TASK_LLM_PLANNER_FAILED,
        f"Planner LLM 处理失败（{reason[:120]}），任务进入待澄清状态。",
    )


def _build_planner_failure_plan(parsed: ParsedTaskSpec, *, reason: str) -> TaskPlan:
    missing_fields = list(dict.fromkeys([*parsed.missing_fields, "planning_context"]))
    error_code, error_message = _map_planner_failure_reason(reason)
    return TaskPlan(
        version="agent-v2",
        mode="llm_plan_execute_gis_workspace",
        status=PLAN_STATUS_NEEDS_CLARIFICATION,
        objective=_build_objective(parsed),
        reasoning_summary=f"规划阶段失败（{reason[:120]}），需要补充上下文后重试。",
        missing_fields=missing_fields,
        steps=_build_steps_legacy(parsed),
        operation_plan_nodes=[],
        error_code=error_code,
        error_message=error_message,
    )


def _normalize_step_name(step_name: str) -> str:
    return _STEP_NAME_ALIASES.get(step_name, step_name)


def _normalize_llm_steps(steps: list[Any]) -> list[TaskPlanStep]:
    tool_definitions = {definition.step_name: definition for definition in list_tool_definitions()}
    required_step_names = [step_name for step_name in STEP_SEQUENCE if step_name in tool_definitions]

    llm_steps_by_name: dict[str, Any] = {}
    for step in steps:
        normalized_name = _normalize_step_name(step.step_name)
        if normalized_name in llm_steps_by_name:
            raise ValueError(f"Duplicated step after alias normalization: {normalized_name}")
        llm_steps_by_name[normalized_name] = step

    missing_steps = [step_name for step_name in required_step_names if step_name not in llm_steps_by_name]
    if missing_steps:
        raise ValueError(f"LLM task plan missing required steps: {missing_steps}")

    normalized_steps: list[TaskPlanStep] = []
    for step_name in required_step_names:
        llm_step = llm_steps_by_name[step_name]
        definition = tool_definitions[step_name]

        depends_on = [
            normalized_dep
            for normalized_dep in (_normalize_step_name(dep_step) for dep_step in llm_step.depends_on)
            if normalized_dep in required_step_names and normalized_dep != step_name
        ]
        if not depends_on:
            depends_on = [dep_step for dep_step in definition.depends_on if dep_step in required_step_names]

        normalized_steps.append(
            TaskPlanStep(
                step_name=step_name,
                tool_name=definition.tool_name,
                title=llm_step.title or definition.title,
                purpose=llm_step.purpose or definition.purpose,
                reasoning=llm_step.reasoning or definition.reasoning,
                depends_on=depends_on,
            )
        )

    return normalized_steps


def _sanitize_missing_fields(parsed: ParsedTaskSpec, missing_fields: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(str(field).strip() for field in missing_fields if str(field).strip()))
    if parsed.analysis_type != "CLIP":
        return deduped

    operation_params = parsed.operation_params or {}
    clip_upload_placeholders = {
        "uploaded_aoi_id",
        "source_raster_upload_id",
        "clip_vector_upload_id",
        "input_raster_file_id",
        "input_vector_file_id",
    }
    source_path = str(operation_params.get("source_path") or "").strip()
    clip_path = str(operation_params.get("clip_path") or "").strip()
    output_path = str(operation_params.get("output_path") or "").strip()

    cleaned: list[str] = []
    for field in deduped:
        if field in clip_upload_placeholders:
            continue
        if field in {"source_path", "operation_params.source_path"} and source_path:
            continue
        if field in {"clip_path", "operation_params.clip_path"} and clip_path:
            continue
        if field in {"output_path", "operation_params.output_path"} and output_path:
            continue
        cleaned.append(field)
    return cleaned


def _normalize_llm_task_plan(payload: LLMTaskPlan, parsed: ParsedTaskSpec) -> TaskPlan:
    missing_fields = _sanitize_missing_fields(
        parsed,
        [*parsed.missing_fields, *payload.missing_fields],
    )
    status = PLAN_STATUS_NEEDS_CLARIFICATION if parsed.need_confirmation or missing_fields else PLAN_STATUS_READY
    objective = payload.objective.strip() or _build_objective(parsed)
    reasoning_summary = payload.reasoning_summary.strip() or _build_reasoning_summary(parsed)
    normalized_steps = _normalize_llm_steps(payload.steps)
    normalized_operation_plan_nodes: list[dict[str, Any]] = []
    if payload.operation_plan_nodes:
        try:
            candidate = OperationPlan(
                version=1,
                status="draft",
                missing_fields=missing_fields,
                nodes=[OperationNode.model_validate(node.model_dump()) for node in payload.operation_plan_nodes],
            )
            validated = validate_operation_plan(candidate)
            normalized_operation_plan_nodes = [node.model_dump() for node in validated.nodes]
        except AppError as exc:
            raise ValueError(f"invalid_operation_plan_nodes:{exc.error_code}") from exc

    return TaskPlan(
        version=payload.version or "agent-v2",
        mode=payload.mode or "llm_plan_execute_gis_workspace",
        status=status,
        objective=objective,
        reasoning_summary=reasoning_summary,
        missing_fields=missing_fields,
        steps=normalized_steps,
        operation_plan_nodes=normalized_operation_plan_nodes,
        error_code=None,
        error_message=None,
    )


@lru_cache
def _load_planner_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"
    return prompt_path.read_text(encoding="utf-8")


def _build_planner_user_prompt(parsed: ParsedTaskSpec) -> str:
    tool_whitelist = [
        {
            "step_name": definition.step_name,
            "tool_name": definition.tool_name,
            "depends_on": definition.depends_on,
            "purpose": definition.purpose,
        }
        for definition in list_tool_definitions()
    ]
    operation_whitelist = [
        {
            "op_name": spec.op_name,
            "input_types": spec.input_types,
            "output_types": spec.output_types,
            "default_params": spec.default_params,
        }
        for spec in list_operation_specs()
    ]
    payload = {
        "task": "build_gis_task_plan",
        "language": "zh-CN",
        "parsed_task_spec": parsed.model_dump(),
        "required_fields": [
            "version",
            "mode",
            "objective",
            "reasoning_summary",
            "missing_fields",
            "steps",
            "operation_plan_nodes",
        ],
        "required_step_names": list(STEP_SEQUENCE),
        "tool_whitelist": tool_whitelist,
        "operation_whitelist": operation_whitelist,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_planner_repair_user_prompt(
    *,
    base_prompt: str,
    previous_json: dict[str, Any],
    validation_errors: list[dict[str, Any]],
) -> str:
    payload = {
        "task": "repair_invalid_json_output",
        "instructions": "上一次 JSON 未通过后端 schema 校验。请仅返回修复后的 JSON，不要输出解释。",
        "original_prompt": base_prompt,
        "previous_json": previous_json,
        "validation_errors": validation_errors,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_task_plan_with_llm(
    parsed: ParsedTaskSpec,
    *,
    task_id: str | None = None,
    db_session: Session | None = None,
) -> TaskPlan:
    settings = get_settings()
    planner_retries = max(0, settings.llm_planner_schema_retries)
    client = LLMClient(settings)
    system_prompt = _load_planner_system_prompt()
    base_user_prompt = _build_planner_user_prompt(parsed)
    user_prompt = base_user_prompt
    attempt = 0
    last_error = "unknown"

    while attempt <= planner_retries:
        response = client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            phase="plan",
            task_id=task_id,
            db_session=db_session,
        )
        try:
            payload = LLMTaskPlan.model_validate(response.content_json)
            return _normalize_llm_task_plan(payload, parsed)
        except (ValidationError, ValueError) as exc:
            last_error = "schema_validation_failed"
            if attempt >= planner_retries:
                break
            attempt += 1
            validation_errors = exc.errors() if isinstance(exc, ValidationError) else [{"msg": str(exc)}]
            user_prompt = _build_planner_repair_user_prompt(
                base_prompt=base_user_prompt,
                previous_json=response.content_json,
                validation_errors=validation_errors,
            )

    return _build_planner_failure_plan(parsed, reason=last_error)


def build_task_plan(
    parsed: ParsedTaskSpec,
    *,
    task_id: str | None = None,
    db_session: Session | None = None,
) -> TaskPlan:
    settings = get_settings()
    if not settings.llm_planner_enabled:
        return _build_task_plan_legacy(parsed)

    try:
        return _build_task_plan_with_llm(
            parsed,
            task_id=task_id,
            db_session=db_session,
        )
    except LLMClientError as exc:
        logger.warning("planner.llm_client_failed detail=%s", exc.detail)
        if settings.llm_planner_legacy_fallback:
            return _build_task_plan_legacy(parsed)
        return _build_planner_failure_plan(parsed, reason="llm_client_error")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("planner.llm_unexpected_error")
        if settings.llm_planner_legacy_fallback:
            return _build_task_plan_legacy(parsed)
        return _build_planner_failure_plan(parsed, reason=repr(exc))


def load_task_plan(plan_payload: dict | None) -> TaskPlan | None:
    if not plan_payload:
        return None
    return TaskPlan(**plan_payload)


def ensure_task_plan(
    plan_payload: dict | None,
    parsed: ParsedTaskSpec,
    *,
    task_id: str | None = None,
) -> TaskPlan:
    plan = load_task_plan(plan_payload)
    if plan is not None:
        return plan
    return build_task_plan(parsed, task_id=task_id)


def set_task_plan_status(plan_payload: dict | None, status: str) -> dict[str, object]:
    plan = deepcopy(plan_payload) if plan_payload else _build_task_plan_legacy(ParsedTaskSpec()).model_dump()
    plan["status"] = status
    return plan


def set_task_plan_step_status(
    plan_payload: dict | None,
    *,
    step_name: str,
    status: str,
    detail: dict[str, object] | None = None,
) -> dict[str, object]:
    plan = deepcopy(plan_payload or {})
    steps = list(plan.get("steps") or [])
    for step in steps:
        if step.get("step_name") != step_name:
            continue
        step["status"] = status
        if detail is not None:
            step["detail"] = detail
        break
    else:
        if status == STEP_STATUS_FAILED:
            plan["status"] = PLAN_STATUS_FAILED
    if status == STEP_STATUS_FAILED:
        plan["status"] = PLAN_STATUS_FAILED
    elif plan.get("status") not in {PLAN_STATUS_NEEDS_CLARIFICATION, PLAN_STATUS_SUCCESS}:
        plan["status"] = PLAN_STATUS_RUNNING
    return plan
