from packages.domain.errors import AppError, ErrorCode, build_error_response, normalize_validation_errors


def test_build_error_response_payload() -> None:
    payload = build_error_response(
        request_id="req-123",
        error_code=ErrorCode.TASK_NOT_FOUND,
        message="Task not found.",
        detail={"task_id": "task-1"},
    )

    assert payload == {
        "error": {
            "code": ErrorCode.TASK_NOT_FOUND,
            "message": "Task not found.",
            "request_id": "req-123",
            "detail": {"task_id": "task-1"},
        }
    }


def test_normalize_validation_errors() -> None:
    errors = normalize_validation_errors(
        [
            {
                "loc": ("body", "session_id"),
                "msg": "Field required",
                "type": "missing",
                "ctx": {"unused": True},
            }
        ]
    )

    assert errors == [
        {
            "loc": ["body", "session_id"],
            "msg": "Field required",
            "type": "missing",
        }
    ]


def test_app_error_constructor() -> None:
    error = AppError.not_found(
        error_code=ErrorCode.SESSION_NOT_FOUND,
        message="Session not found.",
        detail={"session_id": "ses-1"},
    )

    assert error.status_code == 404
    assert error.error_code == ErrorCode.SESSION_NOT_FOUND
    assert error.message == "Session not found."
    assert error.detail == {"session_id": "ses-1"}
