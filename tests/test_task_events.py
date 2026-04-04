from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.domain.config import get_settings
from packages.domain.database import SessionLocal
from packages.domain.models import (
    AOIRecord,
    ArtifactRecord,
    DatasetCandidateRecord,
    MessageRecord,
    SessionRecord,
    TaskEventRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskStepRecord,
)
from packages.domain.services.catalog import CatalogCandidate
from packages.domain.services.graph import runtime_helpers
from packages.domain.services.graph.runner import run_task_graph
from packages.domain.services.orchestrator import list_task_events
from packages.domain.utils import make_id
from packages.domain.errors import ErrorCode
from packages.schemas.agent import LLMReactStepDecision


@pytest.fixture
def db_session():
    with SessionLocal() as db:
        yield db


@pytest.fixture
def seeded_task_id() -> str:
    session_id = make_id("ses")
    message_id = make_id("msg")
    task_id = make_id("task")

    with SessionLocal() as db:
        db.add(SessionRecord(id=session_id, title="events", status="active"))
        db.add(MessageRecord(id=message_id, session_id=session_id, role="user", content="hello"))
        db.add(TaskRunRecord(id=task_id, session_id=session_id, user_message_id=message_id, status="queued"))
        db.flush()

        db.add(TaskEventRecord(task_id=task_id, event_type="task_created", status="queued"))
        db.add(TaskEventRecord(task_id=task_id, event_type="task_queued", status="queued"))
        db.add(TaskEventRecord(task_id=task_id, event_type="task_started", status="running"))
        db.commit()

    yield task_id

    with SessionLocal() as db:
        db.query(TaskEventRecord).filter(TaskEventRecord.task_id == task_id).delete(synchronize_session=False)
        db.query(TaskRunRecord).filter(TaskRunRecord.id == task_id).delete(synchronize_session=False)
        db.query(MessageRecord).filter(MessageRecord.id == message_id).delete(synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
        db.commit()


def test_task_events_are_monotonic_by_id(db_session, seeded_task_id: str) -> None:  # noqa: ANN001
    _, events = list_task_events(db_session, task_id=seeded_task_id, since_id=0)
    ids = [event.id for event in events]
    assert ids == sorted(ids)


def _fake_candidates() -> list[CatalogCandidate]:
    return [
        CatalogCandidate(
            dataset_name="sentinel2",
            collection_id="sentinel-2-l2a",
            scene_count=6,
            coverage_ratio=0.94,
            effective_pixel_ratio_estimate=0.8,
            cloud_metric_summary={"median": 10.0, "p75": 17.0},
            spatial_resolution=10,
            temporal_density_note="high",
            suitability_score=0.9,
            recommendation_rank=1,
            summary_json={"source": "sample_fixture"},
        ),
        CatalogCandidate(
            dataset_name="landsat89",
            collection_id="landsat-c2-l2",
            scene_count=4,
            coverage_ratio=0.87,
            effective_pixel_ratio_estimate=0.72,
            cloud_metric_summary={"median": 16.0, "p75": 24.0},
            spatial_resolution=30,
            temporal_density_note="medium",
            suitability_score=0.78,
            recommendation_rank=2,
            summary_json={"source": "sample_fixture"},
        ),
    ]


def _seed_runtime_task() -> tuple[str, str]:
    session_id = make_id("ses")
    message_id = make_id("msg")
    task_id = make_id("task")
    aoi_id = make_id("aoi")

    task_spec = {
        "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
        "aoi_source_type": "bbox",
        "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
        "requested_dataset": None,
        "analysis_type": "NDVI",
        "preferred_output": ["png_map", "geotiff"],
        "user_priority": "balanced",
        "operation_params": {},
        "need_confirmation": False,
        "missing_fields": [],
        "clarification_message": None,
        "created_from": "tests",
    }

    with SessionLocal() as db:
        db.add(SessionRecord(id=session_id, title="runtime-events", status="active"))
        db.add(MessageRecord(id=message_id, session_id=session_id, role="user", content="run runtime"))
        db.add(
            TaskRunRecord(
                id=task_id,
                session_id=session_id,
                user_message_id=message_id,
                status="queued",
                current_step="parse_task",
                analysis_type="NDVI",
                requested_time_range=task_spec["time_range"],
            )
        )
        db.add(
            TaskSpecRecord(
                task_id=task_id,
                aoi_input=str(task_spec["aoi_input"]),
                aoi_source_type=str(task_spec["aoi_source_type"]),
                preferred_output=list(task_spec["preferred_output"]),
                user_priority=str(task_spec["user_priority"]),
                need_confirmation=False,
                raw_spec_json=task_spec,
            )
        )
        db.add(
            AOIRecord(
                id=aoi_id,
                task_id=task_id,
                name="test-bbox",
                bbox_bounds_json=[116.1, 39.8, 116.5, 40.1],
                area_km2=120.5,
                is_valid=True,
            )
        )
        db.commit()

    return session_id, task_id


def _cleanup_session(session_id: str) -> None:
    with SessionLocal() as db:
        task_ids = [
            task_id
            for (task_id,) in db.query(TaskRunRecord.id).filter(TaskRunRecord.session_id == session_id).all()
        ]

        if task_ids:
            db.query(ArtifactRecord).filter(ArtifactRecord.task_id.in_(task_ids)).delete(synchronize_session=False)
            db.query(TaskEventRecord).filter(TaskEventRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskStepRecord).filter(TaskStepRecord.task_id.in_(task_ids)).delete(synchronize_session=False)
            db.query(DatasetCandidateRecord).filter(DatasetCandidateRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(AOIRecord).filter(AOIRecord.task_id.in_(task_ids)).delete(synchronize_session=False)
            db.query(TaskSpecRecord).filter(TaskSpecRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskRunRecord).filter(TaskRunRecord.id.in_(task_ids)).delete(synchronize_session=False)

        db.query(MessageRecord).filter(MessageRecord.session_id == session_id).delete(synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
        db.commit()


def test_langgraph_runtime_success_event_sequence_is_ordered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_ENABLED", "false")
    get_settings.cache_clear()

    session_id, task_id = _seed_runtime_task()

    try:
        monkeypatch.setattr(runtime_helpers, "search_candidates", lambda spec, aoi: _fake_candidates())
        monkeypatch.setattr(
            runtime_helpers,
            "build_artifact_path",
            lambda task_id, filename: str(tmp_path / f"{task_id}_{filename}"),
        )
        monkeypatch.setattr(
            runtime_helpers,
            "get_settings",
            lambda: SimpleNamespace(real_pipeline_enabled=False),
        )

        run_task_graph(task_id)

        with SessionLocal() as db:
            _, events = list_task_events(db, task_id=task_id, since_id=0)
            task = db.get(TaskRunRecord, task_id)
            assert task is not None
            search_step_detail = next(
                step.detail_json for step in task.steps if step.step_name == "search_candidates"
            )

        assert len(events) > 0
        event_types = [event.event_type for event in events]
        assert "task_started" in event_types
        assert "task_completed" in event_types
        assert event_types[-1] == "task_completed"

        started_index = event_types.index("task_started")
        completed_index = event_types.index("task_completed")
        assert started_index < completed_index

        catalog_event = next(event for event in events if event.event_type == "task_catalog_candidates_ready")
        assert catalog_event.step_name == "search_candidates"
        assert isinstance(catalog_event.detail_json, dict)
        assert "candidate_summaries" in catalog_event.detail_json
        assert "qc" in catalog_event.detail_json
        first_summary = (catalog_event.detail_json.get("candidate_summaries") or [{}])[0]
        assert first_summary.get("quality") is not None
        assert first_summary["quality"]["cloud_metric_summary"]["median"] >= 0
        assert first_summary["quality"]["coverage_ratio"] >= 0
        assert first_summary["quality"]["temporal_density_note"] in {"none", "low", "medium", "high"}

        assert search_step_detail == catalog_event.detail_json

        expected_steps = [
            "normalize_aoi",
            "search_candidates",
            "recommend_dataset",
            "run_processing_pipeline",
            "generate_outputs",
        ]
        react_decision_events = [event for event in events if event.event_type == "step_react_decision"]
        react_observation_events = [event for event in events if event.event_type == "step_react_observation"]
        function_call_requested_events = [
            event for event in events if event.event_type == "function_call_requested"
        ]
        function_call_completed_events = [
            event for event in events if event.event_type == "function_call_completed"
        ]

        assert len(react_decision_events) == len(expected_steps)
        assert len(react_observation_events) == len(expected_steps)
        assert len(function_call_requested_events) == len(expected_steps)
        assert len(function_call_completed_events) == len(expected_steps)

        for step_name in expected_steps:
            react_decision_index = next(
                index
                for index, event in enumerate(events)
                if event.event_type == "step_react_decision" and event.step_name == step_name
            )
            function_call_requested_index = next(
                index
                for index, event in enumerate(events)
                if event.event_type == "function_call_requested" and event.step_name == step_name
            )
            step_started = next(
                index
                for index, event in enumerate(events)
                if event.event_type == "tool_execution_started" and event.step_name == step_name
            )
            step_completed = next(
                index
                for index, event in enumerate(events)
                if event.event_type == "tool_execution_completed" and event.step_name == step_name
            )
            function_call_completed_index = next(
                index
                for index, event in enumerate(events)
                if event.event_type == "function_call_completed" and event.step_name == step_name
            )
            react_observation_index = next(
                index
                for index, event in enumerate(events)
                if event.event_type == "step_react_observation" and event.step_name == step_name
            )
            assert react_decision_index < function_call_requested_index < step_started
            assert step_started < step_completed
            assert step_completed <= function_call_completed_index < react_observation_index
            assert started_index < step_started < completed_index
    finally:
        _cleanup_session(session_id)
        get_settings.cache_clear()


def test_langgraph_runtime_fails_on_illegal_function_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "false")
    monkeypatch.setenv("GIS_AGENT_LLM_RECOMMENDATION_ENABLED", "false")
    get_settings.cache_clear()

    session_id, task_id = _seed_runtime_task()

    try:
        monkeypatch.setattr(
            runtime_helpers,
            "build_artifact_path",
            lambda task_id, filename: str(tmp_path / f"{task_id}_{filename}"),
        )
        monkeypatch.setattr(
            runtime_helpers,
            "_build_step_react_decision",
            lambda **kwargs: LLMReactStepDecision(
                decision="continue",
                function_name="unknown.tool",
                arguments={"step_name": kwargs.get("step_name")},
                reasoning_summary="模拟非法 function call",
            ),
        )

        run_task_graph(task_id)

        with SessionLocal() as db:
            task = db.get(TaskRunRecord, task_id)
            assert task is not None
            assert task.status == "failed"
            assert task.error_code == ErrorCode.TASK_RUNTIME_UNKNOWN_TOOL

            _, events = list_task_events(db, task_id=task_id, since_id=0)
            event_types = [event.event_type for event in events]
            assert "step_react_decision" in event_types
            assert "function_call_requested" not in event_types
            assert "task_failed" in event_types
    finally:
        _cleanup_session(session_id)
        get_settings.cache_clear()
