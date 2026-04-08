from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from packages.domain.database import SessionLocal
from packages.domain.models import (
    MessageRecord,
    MessageUnderstandingRecord,
    SessionRecord,
    TaskRunRecord,
    TaskSpecRevisionRecord,
)
from packages.domain.services.conversation_context import ConversationContextBundle
from packages.domain.services.intent import IntentResult
from packages.domain.utils import make_id
from packages.schemas.task import ParsedTaskSpec

from packages.domain.services.understanding import (
    persist_message_understanding,
    understand_message,
)


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as db:
        yield db


def _create_session(db_session: Session) -> SessionRecord:
    session = SessionRecord(id=make_id("ses"), title="understanding-tests", status="active")
    db_session.add(session)
    db_session.flush()
    return session


def _create_message(
    db_session: Session,
    *,
    session_id: str,
    content: str,
    role: str = "user",
    linked_task_id: str | None = None,
) -> MessageRecord:
    message = MessageRecord(
        id=make_id("msg"),
        session_id=session_id,
        role=role,
        content=content,
        linked_task_id=linked_task_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(message)
    db_session.flush()
    return message


def _create_task(
    db_session: Session,
    *,
    session_id: str,
    user_message_id: str,
) -> TaskRunRecord:
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session_id,
        user_message_id=user_message_id,
        status="queued",
        current_step="understanding",
        analysis_type="NDVI",
    )
    db_session.add(task)
    db_session.flush()
    return task


def _create_revision(
    db_session: Session,
    *,
    task_id: str,
    source_message_id: str,
    understanding_summary: str = "使用上传文件作为 AOI。",
) -> TaskSpecRevisionRecord:
    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task_id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=source_message_id,
        change_type="initial_parse",
        is_active=True,
        understanding_intent="new_task",
        understanding_summary=understanding_summary,
        raw_spec_json={
            "aoi_input": "uploaded_aoi",
            "aoi_source_type": "file_upload",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "NDVI",
        },
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="confirm_understanding",
        understanding_trace_json={},
    )
    db_session.add(revision)
    db_session.flush()
    return revision


def _bundle(
    *,
    session_id: str,
    message_id: str,
    latest_active_task_id: str | None,
    latest_active_revision_id: str | None,
    latest_active_revision_summary: str | None,
    explicit_signals: dict[str, object],
    relevant_messages: list[dict[str, object]] | None = None,
    uploaded_files: list[dict[str, object]] | None = None,
    trace: dict[str, object] | None = None,
) -> ConversationContextBundle:
    return ConversationContextBundle(
        session_id=session_id,
        message_id=message_id,
        latest_active_task_id=latest_active_task_id,
        latest_active_revision_id=latest_active_revision_id,
        latest_active_revision_summary=latest_active_revision_summary,
        uploaded_files=uploaded_files or [],
        relevant_messages=relevant_messages or [],
        explicit_signals=explicit_signals,
        trace=trace
        or {
            "session_id": session_id,
            "message_id": message_id,
        },
    )


def test_correction_signal_with_active_revision_promotes_to_task_correction(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    prior_message = _create_message(
        db_session,
        session_id=session.id,
        content="请基于我上传的边界分析 2024 年 6 月 NDVI。",
    )
    task = _create_task(db_session, session_id=session.id, user_message_id=prior_message.id)
    revision = _create_revision(db_session, task_id=task.id, source_message_id=prior_message.id)
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="我不是上传了省.shp吗？里面有江西。",
    )

    def _fake_classify_message_intent(*args, **kwargs) -> IntentResult:  # noqa: ANN001, ANN002
        return IntentResult(intent="task", confidence=0.91, reason="task-like")

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        _fake_classify_message_intent,
    )
    monkeypatch.setattr(
        "packages.domain.services.understanding.parse_task_message",
        lambda *args, **kwargs: ParsedTaskSpec(aoi_input="江西", aoi_source_type="admin_name"),
    )

    understanding = understand_message(
        current_message.content,
        context=_bundle(
            session_id=session.id,
            message_id=current_message.id,
            latest_active_task_id=task.id,
            latest_active_revision_id=revision.id,
            latest_active_revision_summary=revision.understanding_summary,
            explicit_signals={
                "upload_file_hint": True,
                "bbox_hint": False,
                "admin_region_hint": True,
                "confirmation_hint": False,
                "correction_hint": True,
                "revision_field_overlap": {
                    "revision_id": revision.id,
                    "fields": ["aoi_input", "aoi_source_type", "time_range"],
                    "matched_signals": ["correction_hint"],
                },
            },
            relevant_messages=[
                {"id": prior_message.id, "role": "user", "content": prior_message.content}
            ],
        ),
        task_id=task.id,
        db_session=db_session,
    )

    assert understanding.intent == "task_correction"


def test_confirmation_signal_with_active_task_promotes_to_task_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    prior_message = _create_message(
        db_session,
        session_id=session.id,
        content="帮我分析 2024 年 6 月的 NDVI。",
    )
    task = _create_task(db_session, session_id=session.id, user_message_id=prior_message.id)
    revision = _create_revision(db_session, task_id=task.id, source_message_id=prior_message.id)
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="继续。",
    )

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        lambda *args, **kwargs: IntentResult(intent="chat", confidence=0.83, reason="chat-like"),
    )
    monkeypatch.setattr(
        "packages.domain.services.understanding.parse_task_message",
        lambda *args, **kwargs: ParsedTaskSpec(aoi_input="江西", aoi_source_type="admin_name"),
    )

    understanding = understand_message(
        current_message.content,
        context=_bundle(
            session_id=session.id,
            message_id=current_message.id,
            latest_active_task_id=task.id,
            latest_active_revision_id=revision.id,
            latest_active_revision_summary=revision.understanding_summary,
            explicit_signals={
                "upload_file_hint": False,
                "bbox_hint": False,
                "admin_region_hint": False,
                "confirmation_hint": True,
                "correction_hint": False,
                "revision_field_overlap": {
                    "revision_id": revision.id,
                    "fields": [],
                    "matched_signals": [],
                },
            },
            relevant_messages=[
                {"id": prior_message.id, "role": "user", "content": prior_message.content}
            ],
        ),
        task_id=task.id,
        db_session=db_session,
    )

    assert understanding.intent == "task_confirmation"


def test_understanding_persists_context_builder_trace_details(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    message = _create_message(
        db_session,
        session_id=session.id,
        content="请分析 2024 年 6 月 NDVI。",
    )
    upload_file_id = "file_123"
    context_trace = {
        "session_id": session.id,
        "message_id": message.id,
        "selected_files": [
            {
                "file_id": upload_file_id,
                "original_name": "clip.geojson",
                "reason": "preferred_file_ids",
            }
        ],
        "selected_messages": [
            {
                "message_id": "msg_prior",
                "role": "user",
                "reason": "nearby recent session message",
            }
        ],
    }

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        lambda *args, **kwargs: IntentResult(intent="chat", confidence=0.88, reason="chat-like"),
    )

    understanding = understand_message(
        message.content,
        context=_bundle(
            session_id=session.id,
            message_id=message.id,
            latest_active_task_id=None,
            latest_active_revision_id=None,
            latest_active_revision_summary=None,
            explicit_signals={
                "upload_file_hint": False,
                "bbox_hint": False,
                "admin_region_hint": False,
                "confirmation_hint": False,
                "correction_hint": False,
                "revision_field_overlap": {
                    "revision_id": None,
                    "fields": [],
                    "matched_signals": [],
                },
            },
            relevant_messages=[{"id": message.id, "role": "user", "content": message.content}],
            uploaded_files=[{"id": upload_file_id, "original_name": "clip.geojson"}],
            trace=context_trace,
        ),
        db_session=db_session,
    )

    row = persist_message_understanding(
        db_session,
        message_id=message.id,
        session_id=session.id,
        understanding=understanding,
    )

    assert row.context_trace_json["context_builder"]["selected_files"][0]["file_id"] == upload_file_id
    assert row.context_trace_json["context_builder"]["selected_messages"][0]["message_id"] == "msg_prior"


def test_understanding_orders_history_chronologically_before_classification(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    older_message = _create_message(
        db_session,
        session_id=session.id,
        content="更早的上下文。",
    )
    newer_message = _create_message(
        db_session,
        session_id=session.id,
        content="更晚的上下文。",
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="你好，今天怎么样？",
    )

    captured_history: list[dict[str, str]] = []

    def _fake_classify_message_intent(message: str, *, history, task_id=None, db_session=None) -> IntentResult:  # noqa: ANN001, ANN002
        captured_history.extend(history)
        return IntentResult(intent="chat", confidence=0.88, reason="chat-like")

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        _fake_classify_message_intent,
    )

    understanding = understand_message(
        current_message.content,
        context=_bundle(
            session_id=session.id,
            message_id=current_message.id,
            latest_active_task_id=None,
            latest_active_revision_id=None,
            latest_active_revision_summary=None,
            explicit_signals={
                "upload_file_hint": False,
                "bbox_hint": False,
                "admin_region_hint": False,
                "confirmation_hint": False,
                "correction_hint": False,
                "revision_field_overlap": {
                    "revision_id": None,
                    "fields": [],
                    "matched_signals": [],
                },
            },
            relevant_messages=[
                {
                    "id": newer_message.id,
                    "role": "assistant",
                    "content": newer_message.content,
                    "created_at": datetime(2024, 6, 2, tzinfo=timezone.utc).isoformat(),
                },
                {
                    "id": older_message.id,
                    "role": "user",
                    "content": older_message.content,
                    "created_at": datetime(2024, 6, 1, tzinfo=timezone.utc).isoformat(),
                },
            ],
        ),
        db_session=db_session,
    )

    assert understanding.intent == "chat"
    assert [item["content"] for item in captured_history] == [
        older_message.content,
        newer_message.content,
    ]


def test_task_like_understanding_calls_parser_and_builds_field_confidences(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="请把 /tmp/source.tif 按 /tmp/clip.geojson 裁剪，输出 GeoTIFF。",
    )
    task = _create_task(db_session, session_id=session.id, user_message_id=current_message.id)
    upload = {
        "id": "file_123",
        "original_name": "clip.geojson",
        "file_type": "geojson",
        "storage_key": "/tmp/clip.geojson",
        "size_bytes": 128,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    parser_calls: list[dict[str, object]] = []

    def _fake_classify_message_intent(*args, **kwargs) -> IntentResult:  # noqa: ANN001, ANN002
        return IntentResult(intent="task", confidence=0.9, reason="task-like")

    def _fake_parse_task_message(message: str, has_upload: bool, **kwargs) -> ParsedTaskSpec:
        parser_calls.append(
            {"message": message, "has_upload": has_upload, "kwargs": kwargs}
        )
        return ParsedTaskSpec(
            aoi_input="uploaded_aoi",
            aoi_source_type="file_upload",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="CLIP",
            preferred_output=["geotiff"],
            operation_params={"source_path": "/tmp/source.tif", "clip_path": "/tmp/clip.geojson"},
            need_confirmation=False,
            missing_fields=[],
        )

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        _fake_classify_message_intent,
    )
    monkeypatch.setattr(
        "packages.domain.services.understanding.parse_task_message",
        _fake_parse_task_message,
    )

    understanding = understand_message(
        current_message.content,
        context=_bundle(
            session_id=session.id,
            message_id=current_message.id,
            latest_active_task_id=task.id,
            latest_active_revision_id=None,
            latest_active_revision_summary=None,
            explicit_signals={
                "upload_file_hint": True,
                "bbox_hint": False,
                "admin_region_hint": False,
                "confirmation_hint": False,
                "correction_hint": False,
                "revision_field_overlap": {
                    "revision_id": None,
                    "fields": ["aoi_input"],
                    "matched_signals": ["upload_file_hint"],
                },
            },
            uploaded_files=[upload],
            relevant_messages=[
                {"id": current_message.id, "role": "user", "content": current_message.content}
            ],
        ),
        task_id=task.id,
        db_session=db_session,
    )

    assert parser_calls == [
        {
            "message": current_message.content,
            "has_upload": True,
            "kwargs": {"task_id": task.id, "db_session": db_session},
        }
    ]
    assert understanding.parsed_spec is not None
    assert understanding.parsed_spec.aoi_input == "uploaded_aoi"
    assert understanding.field_confidences["aoi_input"].level == "high"
    assert understanding.field_confidences["time_range"].evidence
    assert "aoi_input" in understanding.parsed_candidates


def test_persist_message_understanding_writes_and_upserts_row(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    message = _create_message(
        db_session,
        session_id=session.id,
        content="请分析 2024 年 6 月 NDVI。",
    )
    task = _create_task(db_session, session_id=session.id, user_message_id=message.id)

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        lambda *args, **kwargs: IntentResult(intent="task", confidence=0.93, reason="task-like"),
    )
    monkeypatch.setattr(
        "packages.domain.services.understanding.parse_task_message",
        lambda *args, **kwargs: ParsedTaskSpec(
            aoi_input="北京",
            aoi_source_type="place_alias",
            time_range={"start": "2024-06-01", "end": "2024-06-30"},
            analysis_type="NDVI",
            preferred_output=["png_map"],
        ),
    )

    understanding = understand_message(
        message.content,
        context=_bundle(
            session_id=session.id,
            message_id=message.id,
            latest_active_task_id=None,
            latest_active_revision_id=None,
            latest_active_revision_summary=None,
            explicit_signals={
                "upload_file_hint": False,
                "bbox_hint": False,
                "admin_region_hint": True,
                "confirmation_hint": False,
                "correction_hint": False,
                "revision_field_overlap": {
                    "revision_id": None,
                    "fields": [],
                    "matched_signals": [],
                },
            },
            relevant_messages=[
                {"id": message.id, "role": "user", "content": message.content}
            ],
        ),
        task_id=task.id,
        db_session=db_session,
    )

    row1 = persist_message_understanding(
        db_session,
        message_id=message.id,
        session_id=session.id,
        task_id=task.id,
        derived_revision_id="rev_001",
        understanding=understanding,
        response_mode="confirm_understanding",
    )
    assert row1.message_id == message.id
    assert row1.intent == "new_task"
    assert row1.response_mode == "confirm_understanding"

    updated_understanding = replace(
        understanding,
        intent="task_followup",
        intent_confidence=0.71,
        understanding_summary="updated summary",
    )
    row2 = persist_message_understanding(
        db_session,
        message_id=message.id,
        session_id=session.id,
        task_id=task.id,
        derived_revision_id="rev_002",
        understanding=updated_understanding,
        response_mode="show_revision",
    )

    rows = (
        db_session.query(MessageUnderstandingRecord)
        .filter(MessageUnderstandingRecord.message_id == message.id)
        .all()
    )
    assert len(rows) == 1
    assert row2.id == rows[0].id
    assert rows[0].intent == "task_followup"
    assert rows[0].derived_revision_id == "rev_002"


def test_chat_like_message_persists_without_parsed_spec(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    message = _create_message(
        db_session,
        session_id=session.id,
        content="你好，今天怎么样？",
    )

    monkeypatch.setattr(
        "packages.domain.services.understanding.classify_message_intent",
        lambda *args, **kwargs: IntentResult(intent="chat", confidence=0.88, reason="chat-like"),
    )
    monkeypatch.setattr(
        "packages.domain.services.understanding.parse_task_message",
        lambda *args, **kwargs: pytest.fail("parser should not be called for chat-like messages"),
    )

    understanding = understand_message(
        message.content,
        context=_bundle(
            session_id=session.id,
            message_id=message.id,
            latest_active_task_id=None,
            latest_active_revision_id=None,
            latest_active_revision_summary=None,
            explicit_signals={
                "upload_file_hint": False,
                "bbox_hint": False,
                "admin_region_hint": False,
                "confirmation_hint": False,
                "correction_hint": False,
                "revision_field_overlap": {
                    "revision_id": None,
                    "fields": [],
                    "matched_signals": [],
                },
            },
            relevant_messages=[{"id": message.id, "role": "user", "content": message.content}],
        ),
        db_session=db_session,
    )

    assert understanding.intent == "chat"
    assert understanding.parsed_spec is None

    row = persist_message_understanding(
        db_session,
        message_id=message.id,
        session_id=session.id,
        understanding=understanding,
    )
    assert row.intent == "chat"
    assert row.field_confidences_json == {}
    assert row.field_evidence_json == {}
