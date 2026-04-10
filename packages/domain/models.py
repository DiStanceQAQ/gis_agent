from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.domain.database import Base


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    messages: Mapped[list[MessageRecord]] = relationship(back_populates="session")
    tasks: Mapped[list[TaskRunRecord]] = relationship(back_populates="session")
    message_understandings: Mapped[list[MessageUnderstandingRecord]] = relationship(
        back_populates="session"
    )
    session_memory_events: Mapped[list[SessionMemoryEventRecord]] = relationship(
        back_populates="session"
    )
    session_state_snapshots: Mapped[list[SessionStateSnapshotRecord]] = relationship(
        back_populates="session"
    )
    session_memory_summaries: Mapped[list[SessionMemorySummaryRecord]] = relationship(
        back_populates="session"
    )
    session_memory_links: Mapped[list[SessionMemoryLinkRecord]] = relationship(
        back_populates="session"
    )
    session_memory_retrieval_cache_entries: Mapped[list[SessionMemoryRetrievalCacheRecord]] = (
        relationship(back_populates="session")
    )


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    content: Mapped[str] = mapped_column(Text)
    linked_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(back_populates="messages")
    understanding: Mapped[MessageUnderstandingRecord | None] = relationship(
        back_populates="message", uselist=False
    )
    session_memory_events: Mapped[list[SessionMemoryEventRecord]] = relationship(
        back_populates="message"
    )
    session_memory_retrieval_cache_entries: Mapped[list[SessionMemoryRetrievalCacheRecord]] = (
        relationship(back_populates="message")
    )


class UploadedFileRecord(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskRunRecord(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_type: Mapped[str] = mapped_column(String(32), default="NDVI")
    requested_time_range: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actual_time_range: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    selected_dataset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recommendation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    methods_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    interaction_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_understanding_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_response_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    session: Mapped[SessionRecord] = relationship(back_populates="tasks")
    task_spec: Mapped[TaskSpecRecord | None] = relationship(back_populates="task", uselist=False)
    aoi: Mapped[AOIRecord | None] = relationship(back_populates="task", uselist=False)
    candidates: Mapped[list[DatasetCandidateRecord]] = relationship(back_populates="task")
    steps: Mapped[list[TaskStepRecord]] = relationship(back_populates="task")
    events: Mapped[list[TaskEventRecord]] = relationship(back_populates="task")
    artifacts: Mapped[list[ArtifactRecord]] = relationship(back_populates="task")
    llm_calls: Mapped[list[LLMCallLogRecord]] = relationship(back_populates="task")
    revisions: Mapped[list[TaskSpecRevisionRecord]] = relationship(back_populates="task")
    message_understandings: Mapped[list[MessageUnderstandingRecord]] = relationship(
        back_populates="task"
    )
    session_memory_events: Mapped[list[SessionMemoryEventRecord]] = relationship(
        back_populates="task"
    )


class LLMCallLogRecord(Base):
    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    phase: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[TaskRunRecord | None] = relationship(back_populates="llm_calls")


class TaskSpecRecord(Base):
    __tablename__ = "task_specs"

    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    aoi_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    aoi_source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preferred_output: Mapped[list | None] = mapped_column(JSON, nullable=True)
    user_priority: Mapped[str] = mapped_column(String(32), default="balanced")
    need_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_spec_json: Mapped[dict] = mapped_column(JSON)

    task: Mapped[TaskRunRecord] = relationship(back_populates="task_spec")


class TaskSpecRevisionRecord(Base):
    __tablename__ = "task_spec_revisions"
    __table_args__ = (
        Index("ix_task_spec_revisions_task_active", "task_id", "is_active"),
        Index("ix_task_spec_revisions_task_created", "task_id", "created_at"),
        Index("ux_task_spec_revisions_task_revision", "task_id", "revision_number", unique=True),
        Index(
            "ux_task_spec_revisions_active",
            "task_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    base_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    lineage_root_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    parent_message_understanding_id: Mapped[str | None] = mapped_column(
        ForeignKey("message_understandings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    change_type: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    understanding_intent: Mapped[str] = mapped_column(String(32))
    understanding_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_spec_json: Mapped[dict] = mapped_column(JSON)
    field_confidences_json: Mapped[dict] = mapped_column(JSON, default=dict)
    ranked_candidates_json: Mapped[dict] = mapped_column(JSON, default=dict)
    response_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    response_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    understanding_trace_json: Mapped[dict] = mapped_column(JSON, default=dict)
    history_features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    user_revision_count: Mapped[int] = mapped_column(Integer, default=0)
    user_last_revision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[TaskRunRecord] = relationship(back_populates="revisions")
    source_message: Mapped[MessageRecord] = relationship()
    parent_message_understanding: Mapped[MessageUnderstandingRecord | None] = relationship(
        back_populates="parent_revisions"
    )
    session_memory_events: Mapped[list[SessionMemoryEventRecord]] = relationship(
        back_populates="revision"
    )


class MessageUnderstandingRecord(Base):
    __tablename__ = "message_understandings"
    __table_args__ = (
        Index("ix_message_understandings_session_created", "session_id", "created_at"),
        Index("ix_message_understandings_task_created", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True
    )
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id"), nullable=True, index=True
    )
    derived_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_state_snapshots.id"),
        nullable=True,
        index=True,
    )
    summary_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_memory_summaries.id"),
        nullable=True,
        index=True,
    )
    lineage_root_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(32))
    intent_confidence: Mapped[float] = mapped_column(Float)
    understanding_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    field_confidences_json: Mapped[dict] = mapped_column(JSON, default=dict)
    field_evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    context_trace_json: Mapped[dict] = mapped_column(JSON, default=dict)
    history_features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[MessageRecord] = relationship(back_populates="understanding")
    session: Mapped[SessionRecord] = relationship(back_populates="message_understandings")
    task: Mapped[TaskRunRecord | None] = relationship(back_populates="message_understandings")
    snapshot: Mapped[SessionStateSnapshotRecord | None] = relationship(
        back_populates="message_understandings"
    )
    summary: Mapped[SessionMemorySummaryRecord | None] = relationship(
        back_populates="message_understandings"
    )
    parent_revisions: Mapped[list[TaskSpecRevisionRecord]] = relationship(
        back_populates="parent_message_understanding"
    )


class AOIRecord(Base):
    __tablename__ = "aois"
    __table_args__ = (
        Index("ix_aois_geom", "geom", postgresql_using="gist"),
        Index("ix_aois_bbox", "bbox", postgresql_using="gist"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), unique=True)
    source_file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    geom: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    bbox: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    bbox_bounds_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    area_km2: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[TaskRunRecord] = relationship(back_populates="aoi")


class SessionMemoryEventRecord(Base):
    __tablename__ = "session_memory_events"
    __table_args__ = (
        Index("ix_session_memory_events_session_created", "session_id", "created_at"),
        Index("ix_session_memory_events_message_id", "message_id"),
        Index("ix_session_memory_events_revision_id", "revision_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True
    )
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_spec_revisions.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    event_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(back_populates="session_memory_events")
    message: Mapped[MessageRecord | None] = relationship(back_populates="session_memory_events")
    task: Mapped[TaskRunRecord | None] = relationship(back_populates="session_memory_events")
    revision: Mapped[TaskSpecRevisionRecord | None] = relationship(
        back_populates="session_memory_events"
    )


class SessionStateSnapshotRecord(Base):
    __tablename__ = "session_state_snapshots"
    __table_args__ = (Index("ux_session_state_snapshots_session", "session_id", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    active_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active_revision_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_understanding_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    open_missing_fields_json: Mapped[list] = mapped_column(JSON, default=list)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_summary_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    field_history_rollup_json: Mapped[dict] = mapped_column(JSON, default=dict)
    user_preference_profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(back_populates="session_state_snapshots")
    message_understandings: Mapped[list[MessageUnderstandingRecord]] = relationship(
        back_populates="snapshot"
    )


class SessionMemorySummaryRecord(Base):
    __tablename__ = "session_memory_summaries"
    __table_args__ = (
        Index("ix_session_memory_summaries_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    summary_kind: Mapped[str] = mapped_column(String(32))
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_event_range_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(back_populates="session_memory_summaries")
    message_understandings: Mapped[list[MessageUnderstandingRecord]] = relationship(
        back_populates="summary"
    )


class SessionMemoryLinkRecord(Base):
    __tablename__ = "session_memory_links"
    __table_args__ = (
        Index("ix_session_memory_links_session_source", "session_id", "source_type", "source_id"),
        Index("ix_session_memory_links_session_target", "session_id", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(32))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(32))
    link_type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(back_populates="session_memory_links")


class SessionMemoryRetrievalCacheRecord(Base):
    __tablename__ = "session_memory_retrieval_cache"
    __table_args__ = (
        Index("ix_session_memory_retrieval_cache_session_created", "session_id", "created_at"),
        Index("ix_session_memory_retrieval_cache_message_id", "message_id"),
        Index("ix_session_memory_retrieval_cache_query_fingerprint", "query_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    query_fingerprint: Mapped[str] = mapped_column(String(128))
    retrieval_result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(
        back_populates="session_memory_retrieval_cache_entries"
    )
    message: Mapped[MessageRecord] = relationship(
        back_populates="session_memory_retrieval_cache_entries"
    )


class DatasetCandidateRecord(Base):
    __tablename__ = "dataset_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    dataset_name: Mapped[str] = mapped_column(String(32))
    collection_id: Mapped[str] = mapped_column(String(128))
    scene_count: Mapped[int] = mapped_column(Integer)
    coverage_ratio: Mapped[float] = mapped_column(Float)
    effective_pixel_ratio_estimate: Mapped[float] = mapped_column(Float)
    cloud_metric_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    spatial_resolution: Mapped[int] = mapped_column(Integer)
    temporal_density_note: Mapped[str] = mapped_column(String(32))
    suitability_score: Mapped[float] = mapped_column(Float)
    recommendation_rank: Mapped[int] = mapped_column(Integer)
    summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    task: Mapped[TaskRunRecord] = relationship(back_populates="candidates")


class TaskStepRecord(Base):
    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    step_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    task: Mapped[TaskRunRecord] = relationship(back_populates="steps")


class TaskEventRecord(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    step_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[TaskRunRecord] = relationship(back_populates="events")


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(128))
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[TaskRunRecord] = relationship(back_populates="artifacts")
