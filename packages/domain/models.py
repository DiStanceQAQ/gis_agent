from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, func
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


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    content: Mapped[str] = mapped_column(Text)
    linked_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SessionRecord] = relationship(back_populates="messages")


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
    recommendation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    methods_text: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    artifacts: Mapped[list[ArtifactRecord]] = relationship(back_populates="task")


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
