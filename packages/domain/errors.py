from __future__ import annotations

from typing import Any

from fastapi import status


class ErrorCode:
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"

    SESSION_NOT_FOUND = "session_not_found"
    TASK_NOT_FOUND = "task_not_found"
    FILE_NOT_FOUND = "file_not_found"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_STORAGE_MISSING = "artifact_storage_missing"

    FILE_NAME_REQUIRED = "file_name_required"
    TASK_SPEC_MISSING = "task_spec_missing"
    AOI_UNSUPPORTED_FILE_TYPE = "aoi_unsupported_file_type"
    AOI_PARSE_FAILED = "aoi_parse_failed"
    AOI_INVALID_BBOX = "aoi_invalid_bbox"
    AOI_AREA_TOO_LARGE = "aoi_area_too_large"

    TASK_RUNTIME_FAILED = "task_runtime_failed"
    TASK_RUNTIME_UNKNOWN_TOOL = "task_runtime_unknown_tool"
    TASK_RUNTIME_INVALID_STATE_TRANSITION = "task_runtime_invalid_state_transition"
    TASK_RUNTIME_TIMEOUT = "task_runtime_timeout"
    TASK_RUNTIME_MAX_STEPS_EXCEEDED = "task_runtime_max_steps_exceeded"
    TASK_RUNTIME_MAX_TOOL_CALLS_EXCEEDED = "task_runtime_max_tool_calls_exceeded"
    TASK_LLM_PARSER_SCHEMA_VALIDATION_FAILED = "task_llm_parser_schema_validation_failed"
    TASK_LLM_PARSER_FAILED = "task_llm_parser_failed"
    TASK_LLM_PLANNER_SCHEMA_VALIDATION_FAILED = "task_llm_planner_schema_validation_failed"
    TASK_LLM_PLANNER_FAILED = "task_llm_planner_failed"
    TASK_LLM_RECOMMENDATION_SCHEMA_VALIDATION_FAILED = "task_llm_recommendation_schema_validation_failed"
    TASK_LLM_RECOMMENDATION_FAILED = "task_llm_recommendation_failed"
    PLAN_SLOT_MISSING = "plan_slot_missing"
    PLAN_SCHEMA_INVALID = "plan_schema_invalid"
    PLAN_DEPENDENCY_CYCLE = "plan_dependency_cycle"
    PLAN_APPROVAL_REQUIRED = "plan_approval_required"
    PLAN_EDIT_INVALID = "plan_edit_invalid"
    OP_INPUT_TYPE_MISMATCH = "op_input_type_mismatch"
    OP_PARAM_INVALID = "op_param_invalid"
    OP_RUNTIME_FAILED = "op_runtime_failed"
    ARTIFACT_EXPORT_FAILED = "artifact_export_failed"


class AppError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.detail = detail

    @classmethod
    def bad_request(
        cls,
        *,
        error_code: str = ErrorCode.BAD_REQUEST,
        message: str,
        detail: Any | None = None,
    ) -> "AppError":
        return cls(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code=error_code,
            message=message,
            detail=detail,
        )

    @classmethod
    def not_found(
        cls,
        *,
        error_code: str,
        message: str,
        detail: Any | None = None,
    ) -> "AppError":
        return cls(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code=error_code,
            message=message,
            detail=detail,
        )


def normalize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg"),
            "type": error.get("type"),
        }
        for error in errors
    ]


def build_error_response(
    *,
    request_id: str | None,
    error_code: str,
    message: str,
    detail: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "code": error_code,
            "message": message,
            "request_id": request_id,
        }
    }
    if detail is not None:
        payload["error"]["detail"] = detail
    return payload
