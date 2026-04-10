from __future__ import annotations

from packages.domain.models import TaskRunRecord
from packages.schemas.operation_plan import OperationPlan
from packages.schemas.task import TaskPlanResponse


def write_plan_json(
    task: TaskRunRecord,
    plan_data: dict,
    *,
    validate_operation_plan: bool = True,
) -> None:
    TaskPlanResponse.model_validate(plan_data)
    if validate_operation_plan:
        op_plan_data = plan_data.get("operation_plan")
        if isinstance(op_plan_data, dict) and op_plan_data:
            OperationPlan.model_validate(op_plan_data)
    task.plan_json = plan_data


def update_plan_json(
    task: TaskRunRecord,
    updates: dict,
    *,
    validate_operation_plan: bool = True,
) -> None:
    current = dict(task.plan_json or {})
    current.update(updates)
    write_plan_json(task, current, validate_operation_plan=validate_operation_plan)
