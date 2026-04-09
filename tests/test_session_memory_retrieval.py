from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from packages.domain.database import SessionLocal
from packages.domain.models import (
    MessageRecord,
    SessionRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskSpecRevisionRecord,
    UploadedFileRecord,
)
from packages.domain.services.session_memory import SessionMemoryService
from packages.domain.services.session_memory_retrieval import SessionMemoryRetrievalService
from packages.domain.utils import make_id


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as db:
        yield db


def _create_session(db_session: Session) -> SessionRecord:
    session = SessionRecord(
        id=make_id("ses"),
        title="retrieval-tests",
        status="active",
    )
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
    created_at: datetime,
) -> MessageRecord:
    message = MessageRecord(
        id=make_id("msg"),
        session_id=session_id,
        role=role,
        content=content,
        linked_task_id=linked_task_id,
        created_at=created_at,
    )
    db_session.add(message)
    db_session.flush()
    return message


def _create_upload(
    db_session: Session,
    *,
    session_id: str,
    original_name: str,
    file_type: str,
    created_at: datetime,
) -> UploadedFileRecord:
    uploaded = UploadedFileRecord(
        id=make_id("file"),
        session_id=session_id,
        original_name=original_name,
        file_type=file_type,
        storage_key=f"/tmp/{original_name}",
        size_bytes=128,
        checksum=f"checksum-{original_name}",
        created_at=created_at,
    )
    db_session.add(uploaded)
    db_session.flush()
    return uploaded


def _create_task_with_revisions(
    db_session: Session,
    *,
    session_id: str,
    source_message: MessageRecord,
) -> tuple[TaskRunRecord, TaskSpecRevisionRecord, TaskSpecRevisionRecord]:
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session_id,
        user_message_id=source_message.id,
        current_step="parse_task",
        status="queued",
        analysis_type="NDVI",
    )
    db_session.add(task)
    db_session.flush()

    spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={
            "aoi_input": "uploaded_aoi",
            "aoi_source_type": "file_upload",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "NDVI",
            "upload_slots": {"input_vector_file_id": "file_seed"},
        },
    )
    db_session.add(spec)
    db_session.flush()

    rev1 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=source_message.id,
        change_type="initial_parse",
        is_active=False,
        understanding_intent="new_task",
        understanding_summary="使用省.shp 作为 AOI，时间 2024 年 6 月。",
        raw_spec_json={
            "aoi_input": "uploaded_aoi",
            "aoi_source_type": "file_upload",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "NDVI",
            "upload_slots": {"input_vector_file_id": "file_seed"},
        },
        field_confidences_json={"aoi_input": {"score": 0.9}},
        ranked_candidates_json={},
        response_mode="confirm_understanding",
        understanding_trace_json={},
        created_at=source_message.created_at + timedelta(minutes=1),
    )
    db_session.add(rev1)
    db_session.flush()

    rev2 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=2,
        base_revision_id=rev1.id,
        source_message_id=source_message.id,
        change_type="correction",
        is_active=True,
        understanding_intent="task_correction",
        understanding_summary="保持省.shp AOI，改成 2023 年 6 月。",
        raw_spec_json={
            "aoi_input": "uploaded_aoi",
            "aoi_source_type": "file_upload",
            "time_range": {"start": "2023-06-01", "end": "2023-06-30"},
            "analysis_type": "NDVI",
            "upload_slots": {"input_vector_file_id": "file_seed"},
        },
        field_confidences_json={"time_range": {"score": 0.95}},
        ranked_candidates_json={},
        response_mode="show_revision",
        understanding_trace_json={},
        created_at=source_message.created_at + timedelta(minutes=2),
    )
    db_session.add(rev2)
    db_session.flush()
    return task, rev1, rev2


def test_session_memory_retrieval_returns_required_sections(db_session: Session) -> None:
    now = datetime.now(timezone.utc)
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="请用省.shp 分析 2024 年 6 月 NDVI。",
        created_at=now - timedelta(minutes=30),
    )
    task, _, active_revision = _create_task_with_revisions(
        db_session,
        session_id=session.id,
        source_message=source_message,
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="省.shp",
        file_type="shp",
        created_at=now - timedelta(minutes=29),
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="我不是已经传了省.shp吗？",
        created_at=now,
    )

    snapshot = SessionMemoryService(db_session).refresh_snapshot(
        session.id,
        task_id=task.id,
        revision_id=active_revision.id,
    )

    result = SessionMemoryRetrievalService(db_session).retrieve(
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
        snapshot=snapshot,
    )

    assert isinstance(result.messages, list)
    assert isinstance(result.revisions, list)
    assert isinstance(result.uploads, list)
    assert isinstance(result.field_value_history, dict)
    assert isinstance(result.trace, dict)
    assert "scoring" in result.trace


def test_session_memory_retrieval_prefers_relevant_old_upload_and_revision_over_recent_noise(
    db_session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    session = _create_session(db_session)

    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="请基于我上传的省.shp 分析 2024 年 6 月 NDVI。",
        created_at=now - timedelta(minutes=40),
    )
    task, _, active_revision = _create_task_with_revisions(
        db_session,
        session_id=session.id,
        source_message=source_message,
    )

    relevant_old_upload = _create_upload(
        db_session,
        session_id=session.id,
        original_name="省.shp",
        file_type="shp",
        created_at=now - timedelta(minutes=39),
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="notes.txt",
        file_type="txt",
        created_at=now - timedelta(minutes=2),
    )

    _create_message(
        db_session,
        session_id=session.id,
        content="今天天气不错。",
        role="assistant",
        created_at=now - timedelta(minutes=1),
    )
    _create_message(
        db_session,
        session_id=session.id,
        content="我们聊点别的吧。",
        role="user",
        created_at=now - timedelta(seconds=30),
    )

    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="我不是已经传了省.shp吗？",
        created_at=now,
    )

    snapshot = SessionMemoryService(db_session).refresh_snapshot(
        session.id,
        task_id=task.id,
        revision_id=active_revision.id,
    )

    result = SessionMemoryRetrievalService(db_session).retrieve(
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
        snapshot=snapshot,
    )

    assert result.uploads[0]["id"] == relevant_old_upload.id
    assert result.revisions[0]["id"] == active_revision.id
    assert source_message.id in {item["id"] for item in result.messages}

    top_upload_score = result.trace["scoring"]["uploads"][0]
    assert top_upload_score["signal_scores"]["structural_relevance"] > 0
    assert top_upload_score["signal_scores"]["semantic_text_hint"] > 0
    assert "recency_decay" in top_upload_score["signal_scores"]
    assert "correction_link_bonus" in top_upload_score["signal_scores"]


def test_session_memory_retrieval_builds_field_value_history_from_revision_chain(
    db_session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="先做初版。",
        created_at=now - timedelta(minutes=20),
    )
    task, rev1, rev2 = _create_task_with_revisions(
        db_session,
        session_id=session.id,
        source_message=source_message,
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="把时间改成 2023 年 6 月。",
        created_at=now,
    )

    snapshot = SessionMemoryService(db_session).refresh_snapshot(
        session.id,
        task_id=task.id,
        revision_id=rev2.id,
    )

    result = SessionMemoryRetrievalService(db_session).retrieve(
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
        snapshot=snapshot,
    )

    history = result.field_value_history
    assert [entry["revision_id"] for entry in history["time_range"][:2]] == [rev2.id, rev1.id]
    assert history["aoi_input"][0]["value"] == "uploaded_aoi"
    assert history["analysis_type"][0]["value"] == "NDVI"
