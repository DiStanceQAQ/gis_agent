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
from packages.domain.services.conversation_context import build_conversation_context
from packages.domain.services.task_revisions import get_active_revision
from packages.domain.utils import make_id


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as db:
        yield db


def _create_session(db_session: Session, *, session_id: str | None = None) -> SessionRecord:
    session = SessionRecord(
        id=session_id or make_id("ses"),
        title="context-tests",
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
    created_at: datetime | None = None,
) -> MessageRecord:
    message = MessageRecord(
        id=make_id("msg"),
        session_id=session_id,
        role=role,
        content=content,
        linked_task_id=linked_task_id,
        created_at=created_at or datetime.now(timezone.utc),
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
    storage_key: str,
    created_at: datetime | None = None,
) -> UploadedFileRecord:
    uploaded_file = UploadedFileRecord(
        id=make_id("file"),
        session_id=session_id,
        original_name=original_name,
        file_type=file_type,
        storage_key=storage_key,
        size_bytes=128,
        checksum=f"checksum-{original_name}",
        created_at=created_at or datetime.now(timezone.utc),
    )
    db_session.add(uploaded_file)
    db_session.flush()
    return uploaded_file


def _seed_active_task(
    db_session: Session,
    *,
    session: SessionRecord,
    source_message: MessageRecord,
    summary: str = "使用上传边界文件作为 AOI。",
    raw_spec_json: dict[str, object] | None = None,
) -> tuple[TaskRunRecord, TaskSpecRevisionRecord]:
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session.id,
        user_message_id=source_message.id,
        current_step="parse_task",
        status="queued",
        analysis_type="NDVI",
    )
    db_session.add(task)
    db_session.flush()

    task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json=raw_spec_json
        or {
            "aoi_input": "uploaded_aoi",
            "aoi_source_type": "file_upload",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "upload_slots": {"input_vector_file_id": "file_preferred"},
        },
    )
    db_session.add(task_spec)
    db_session.flush()

    revision = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=source_message.id,
        change_type="initial_parse",
        is_active=True,
        understanding_intent="new_task",
        understanding_summary=summary,
        raw_spec_json=dict(task_spec.raw_spec_json),
        field_confidences_json={},
        ranked_candidates_json={},
        response_mode="confirm_understanding",
        understanding_trace_json={},
    )
    db_session.add(revision)
    db_session.flush()
    return task, revision


def test_build_conversation_context_prioritizes_active_revision_and_preferred_uploads(
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="请基于我刚上传的边界文件分析 2024 年 6 月 NDVI。",
    )
    task, revision = _seed_active_task(db_session, session=session, source_message=source_message)
    preferred_upload = _create_upload(
        db_session,
        session_id=session.id,
        original_name="preferred.shp",
        file_type="shp",
        storage_key="/tmp/preferred.shp",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    recent_upload = _create_upload(
        db_session,
        session_id=session.id,
        original_name="recent.geojson",
        file_type="geojson",
        storage_key="/tmp/recent.geojson",
        created_at=datetime.now(timezone.utc),
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="继续用刚才那份边界文件。",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    bundle = build_conversation_context(
        db_session,
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
        preferred_file_ids=[preferred_upload.id],
    )

    assert bundle.latest_active_task_id == task.id
    assert bundle.latest_active_revision_id == revision.id
    assert bundle.latest_active_revision_summary == "使用上传边界文件作为 AOI。"
    assert [item["id"] for item in bundle.uploaded_files[:2]] == [preferred_upload.id, recent_upload.id]
    assert source_message.id in {item["id"] for item in bundle.relevant_messages}
    assert bundle.trace["latest_active_task"]["task_id"] == task.id
    assert bundle.trace["selected_files"][0]["file_id"] == preferred_upload.id


def test_build_conversation_context_backfills_latest_legacy_task_revision(
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="请基于我刚上传的边界文件分析 2024 年 6 月 NDVI。",
    )
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session.id,
        user_message_id=source_message.id,
        current_step="parse_task",
        status="queued",
        analysis_type="NDVI",
    )
    db_session.add(task)
    db_session.flush()
    task_spec = TaskSpecRecord(
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
            "upload_slots": {"input_vector_file_id": "file_preferred"},
        },
    )
    db_session.add(task_spec)
    db_session.flush()
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="继续用刚才那份边界文件。",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    bundle = build_conversation_context(
        db_session,
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
    )

    active_revision = get_active_revision(db_session, task.id)
    assert active_revision is not None
    assert bundle.latest_active_task_id == task.id
    assert bundle.latest_active_revision_id == active_revision.id
    assert bundle.latest_active_revision_summary is None
    assert bundle.trace["latest_active_task"]["task_id"] == task.id
    assert bundle.trace["latest_active_revision"]["revision_id"] == active_revision.id


def test_build_conversation_context_keeps_preferred_uploads_even_when_older(
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="请基于我刚上传的边界文件分析 2024 年 6 月 NDVI。",
    )
    _seed_active_task(db_session, session=session, source_message=source_message)
    preferred_upload = _create_upload(
        db_session,
        session_id=session.id,
        original_name="preferred-old.shp",
        file_type="shp",
        storage_key="/tmp/preferred-old.shp",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="newer-1.geojson",
        file_type="geojson",
        storage_key="/tmp/newer-1.geojson",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=4),
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="newer-2.geojson",
        file_type="geojson",
        storage_key="/tmp/newer-2.geojson",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=3),
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="newer-3.geojson",
        file_type="geojson",
        storage_key="/tmp/newer-3.geojson",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="newer-4.geojson",
        file_type="geojson",
        storage_key="/tmp/newer-4.geojson",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    _create_upload(
        db_session,
        session_id=session.id,
        original_name="newer-5.geojson",
        file_type="geojson",
        storage_key="/tmp/newer-5.geojson",
        created_at=datetime.now(timezone.utc),
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="继续用刚才那份边界文件。",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    bundle = build_conversation_context(
        db_session,
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
        preferred_file_ids=[preferred_upload.id],
    )

    assert bundle.uploaded_files[0]["id"] == preferred_upload.id
    assert bundle.trace["selected_files"][0]["file_id"] == preferred_upload.id


def test_build_conversation_context_sets_explicit_signals_and_overlap(
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="请基于我刚上传的边界文件分析 2024 年 6 月 NDVI。",
    )
    _, revision = _seed_active_task(
        db_session,
        session=session,
        source_message=source_message,
        raw_spec_json={
            "aoi_input": "江西",
            "aoi_source_type": "admin_name",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "upload_slots": {"input_vector_file_id": "file_upload"},
        },
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="我不是上传了省.shp吗？把时间改成 2023 年 6 月，里面有江西。",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    bundle = build_conversation_context(
        db_session,
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
    )

    assert bundle.explicit_signals["upload_file_hint"] is True
    assert bundle.explicit_signals["admin_region_hint"] is True
    assert bundle.explicit_signals["correction_hint"] is True
    assert bundle.explicit_signals["confirmation_hint"] is False
    assert "aoi_input" in bundle.explicit_signals["revision_field_overlap"]["fields"]
    assert "aoi_source_type" in bundle.explicit_signals["revision_field_overlap"]["fields"]
    assert bundle.explicit_signals["revision_field_overlap"]["revision_id"] == revision.id
    assert bundle.trace["signal_evidence"]["correction_hint"]


def test_build_conversation_context_pulls_confirmation_context(
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    source_message = _create_message(
        db_session,
        session_id=session.id,
        content="帮我分析 2024 年 6 月 bbox(116.1,39.8,116.5,40.1) 的 NDVI，并先生成计划。",
    )
    task, revision = _seed_active_task(db_session, session=session, source_message=source_message)
    confirmation_prompt = _create_message(
        db_session,
        session_id=session.id,
        content="已生成执行计划，请确认后开始执行。",
        role="assistant",
        linked_task_id=task.id,
    )
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="好的，继续",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    bundle = build_conversation_context(
        db_session,
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
    )

    assert bundle.explicit_signals["confirmation_hint"] is True
    relevant_ids = {item["id"] for item in bundle.relevant_messages}
    assert confirmation_prompt.id in relevant_ids
    assert source_message.id in relevant_ids
    assert bundle.trace["selected_messages"][0]["reason"]


def test_build_conversation_context_returns_empty_bundle_for_minimal_session(
    db_session: Session,
) -> None:
    session = _create_session(db_session)
    current_message = _create_message(
        db_session,
        session_id=session.id,
        content="你好",
    )

    bundle = build_conversation_context(
        db_session,
        session_id=session.id,
        message_id=current_message.id,
        message_content=current_message.content,
    )

    assert bundle.latest_active_task_id is None
    assert bundle.latest_active_revision_id is None
    assert bundle.latest_active_revision_summary is None
    assert bundle.uploaded_files == []
    assert bundle.relevant_messages == []
    assert bundle.explicit_signals["upload_file_hint"] is False
    assert bundle.explicit_signals["bbox_hint"] is False
    assert bundle.explicit_signals["admin_region_hint"] is False
    assert bundle.explicit_signals["confirmation_hint"] is False
    assert bundle.explicit_signals["correction_hint"] is False
    assert bundle.trace["selected_files"] == []
    assert bundle.trace["selected_messages"] == []
