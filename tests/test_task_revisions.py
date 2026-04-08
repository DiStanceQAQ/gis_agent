from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.domain.database import SessionLocal
from packages.domain.models import MessageRecord, SessionRecord, TaskRunRecord, TaskSpecRecord, TaskSpecRevisionRecord
from packages.domain.services.task_revisions import (
    activate_revision,
    create_initial_revision,
    ensure_initial_revision_for_task,
    get_active_revision,
)
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as db:
        yield db


@dataclass(slots=True)
class UnderstandingStub:
    intent: str
    understanding_summary: str | None
    parsed_spec: ParsedTaskSpec | None
    ranked_candidates: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    trace: dict[str, object] = field(default_factory=dict)
    _field_confidences: dict[str, object] = field(default_factory=dict)

    def field_confidences_dump(self) -> dict[str, object]:
        return dict(self._field_confidences)


def _seed_task(db_session: Session) -> tuple[SessionRecord, MessageRecord, TaskRunRecord]:
    session = SessionRecord(id=make_id("ses"), title="revision-tests", status="active")
    message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="江西")
    task = TaskRunRecord(id=make_id("task"), session_id=session.id, user_message_id=message.id)
    db_session.add_all([session, message, task])
    db_session.flush()
    return session, message, task


def _seed_task_with_two_revisions(
    db_session: Session,
) -> tuple[TaskRunRecord, TaskSpecRevisionRecord, TaskSpecRevisionRecord]:
    _, message, task = _seed_task(db_session)
    second_message = MessageRecord(
        id=make_id("msg"),
        session_id=task.session_id,
        role="user",
        content="改成北京",
    )
    db_session.add(second_message)
    db_session.add(
        TaskSpecRecord(
            task_id=task.id,
            aoi_input="江西",
            aoi_source_type="admin_name",
            preferred_output=["png_map"],
            user_priority="balanced",
            need_confirmation=False,
            raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
        )
    )
    rev1 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=message.id,
        change_type="initial_parse",
        is_active=True,
        understanding_intent="new_task",
        understanding_summary="江西作为 AOI。",
        raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="confirm_understanding",
        understanding_trace_json={},
    )
    rev2 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=2,
        base_revision_id=rev1.id,
        source_message_id=second_message.id,
        change_type="user_edit",
        is_active=False,
        understanding_intent="revise_task",
        understanding_summary="北京作为 AOI。",
        raw_spec_json={"aoi_input": "北京", "aoi_source_type": "admin_name"},
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="show_revision",
        understanding_trace_json={},
    )
    db_session.add_all([rev1, rev2])
    db_session.flush()
    return task, rev1, rev2


def test_create_initial_revision_sets_revision_number_one(db_session: Session) -> None:
    _session, _message, task = _seed_task(db_session)
    understanding = UnderstandingStub(
        intent="new_task",
        understanding_summary="江西作为 AOI。",
        parsed_spec=ParsedTaskSpec(aoi_input="江西", aoi_source_type="admin_name"),
    )

    revision = create_initial_revision(
        db_session,
        task=task,
        understanding=understanding,
        source_message_id=task.user_message_id,
    )

    assert revision.revision_number == 1
    assert revision.is_active is False
    assert revision.raw_spec_json["aoi_input"] == "江西"


def test_activate_revision_clears_previous_active_and_mirrors_legacy_task_spec(
    db_session: Session,
) -> None:
    task, _rev1, rev2 = _seed_task_with_two_revisions(db_session)

    activate_revision(db_session, task=task, revision=rev2)

    db_session.refresh(task)
    assert get_active_revision(db_session, task.id).id == rev2.id
    assert task.task_spec is not None
    assert task.task_spec.raw_spec_json["aoi_input"] == rev2.raw_spec_json["aoi_input"]


def test_ensure_initial_revision_reloads_after_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    _session, _message, task = _seed_task(db_session)
    task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="江西",
        aoi_source_type="admin_name",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
    )
    db_session.add(task_spec)
    db_session.commit()

    calls = {"insert": 0}
    expected_revision_id = make_id("rev")

    def _insert_once(*args, **kwargs):
        calls["insert"] += 1
        with SessionLocal() as other_db:
            other_db.add(
                TaskSpecRevisionRecord(
                    id=expected_revision_id,
                    task_id=task.id,
                    revision_number=1,
                    base_revision_id=None,
                    source_message_id=task.user_message_id,
                    change_type="legacy_backfill",
                    is_active=True,
                    understanding_intent="legacy_materialized",
                    understanding_summary="legacy",
                    raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
                    field_confidences_json={},
                    ranked_candidates_json={},
                    understanding_trace_json={},
                )
            )
            other_db.commit()
        raise IntegrityError("insert", {}, Exception("duplicate"))

    monkeypatch.setattr("packages.domain.services.task_revisions._insert_initial_revision", _insert_once)
    revision = ensure_initial_revision_for_task(db_session, task)

    assert calls["insert"] == 1
    assert revision.id == expected_revision_id
    assert revision.is_active is True
