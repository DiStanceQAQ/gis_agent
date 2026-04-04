import pytest

from packages.domain.services.task_state import (
    TASK_STATUS_APPROVED,
    TASK_STATUS_AWAITING_APPROVAL,
    TASK_STATUS_CANCELLED,
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SUCCESS,
    TASK_STATUS_DRAFT,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_WAITING_CLARIFICATION,
    can_transition,
    ensure_step_status_transition,
    ensure_task_status_transition,
    TASK_STATUS_TRANSITIONS,
)


def test_can_transition_allows_valid_task_status_change() -> None:
    assert can_transition(TASK_STATUS_TRANSITIONS, TASK_STATUS_DRAFT, TASK_STATUS_QUEUED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TASK_STATUS_DRAFT, TASK_STATUS_AWAITING_APPROVAL),
        (TASK_STATUS_WAITING_CLARIFICATION, TASK_STATUS_AWAITING_APPROVAL),
        (TASK_STATUS_WAITING_CLARIFICATION, TASK_STATUS_FAILED),
        (TASK_STATUS_WAITING_CLARIFICATION, TASK_STATUS_CANCELLED),
        (TASK_STATUS_QUEUED, TASK_STATUS_WAITING_CLARIFICATION),
        (TASK_STATUS_QUEUED, TASK_STATUS_CANCELLED),
        (TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_APPROVED),
        (TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_CANCELLED),
        (TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_FAILED),
        (TASK_STATUS_APPROVED, TASK_STATUS_QUEUED),
        (TASK_STATUS_APPROVED, TASK_STATUS_CANCELLED),
        (TASK_STATUS_RUNNING, TASK_STATUS_CANCELLED),
        (TASK_STATUS_APPROVED, TASK_STATUS_FAILED),
    ],
)
def test_approval_state_allows_expected_transitions(current: str, target: str) -> None:
    ensure_task_status_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TASK_STATUS_DRAFT, TASK_STATUS_APPROVED),
        (TASK_STATUS_DRAFT, TASK_STATUS_CANCELLED),
        (TASK_STATUS_AWAITING_APPROVAL, TASK_STATUS_QUEUED),
        (TASK_STATUS_APPROVED, TASK_STATUS_RUNNING),
        (TASK_STATUS_APPROVED, TASK_STATUS_SUCCESS),
        (TASK_STATUS_CANCELLED, TASK_STATUS_QUEUED),
        (TASK_STATUS_CANCELLED, TASK_STATUS_RUNNING),
        (TASK_STATUS_CANCELLED, TASK_STATUS_SUCCESS),
        (TASK_STATUS_CANCELLED, TASK_STATUS_FAILED),
        (TASK_STATUS_CANCELLED, TASK_STATUS_WAITING_CLARIFICATION),
        (TASK_STATUS_CANCELLED, TASK_STATUS_APPROVED),
    ],
)
def test_approval_state_rejects_invalid_transitions(current: str, target: str) -> None:
    with pytest.raises(ValueError):
        ensure_task_status_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TASK_STATUS_DRAFT, TASK_STATUS_SUCCESS),
        (TASK_STATUS_SUCCESS, TASK_STATUS_RUNNING),
        (TASK_STATUS_FAILED, TASK_STATUS_QUEUED),
    ],
)
def test_ensure_task_status_transition_rejects_invalid_changes(current: str, target: str) -> None:
    with pytest.raises(ValueError):
        ensure_task_status_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (STEP_STATUS_PENDING, STEP_STATUS_RUNNING),
        (STEP_STATUS_PENDING, STEP_STATUS_FAILED),
        (STEP_STATUS_RUNNING, STEP_STATUS_SUCCESS),
        (STEP_STATUS_RUNNING, STEP_STATUS_FAILED),
    ],
)
def test_ensure_step_status_transition_accepts_valid_changes(current: str, target: str) -> None:
    ensure_step_status_transition(current, target)


def test_ensure_step_status_transition_rejects_invalid_changes() -> None:
    with pytest.raises(ValueError):
        ensure_step_status_transition(STEP_STATUS_SUCCESS, STEP_STATUS_RUNNING)
