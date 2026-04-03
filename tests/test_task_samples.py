from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.domain.config import get_settings
from packages.domain.database import SessionLocal
from packages.domain.errors import ErrorCode
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
    UploadedFileRecord,
)
from packages.domain.services import agent_runtime, orchestrator
from packages.domain.services.catalog import CatalogCandidate
from packages.domain.services.graph import runtime_helpers
from packages.domain.services.graph.runner import run_task_graph
from packages.domain.services.planner import PLAN_STATUS_NEEDS_CLARIFICATION, TaskPlan
from packages.schemas.message import MessageCreateRequest
from packages.schemas.task import ParsedTaskSpec


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tasks"
TASK_SUITE_PATH = FIXTURES_DIR / "task_suite.json"
LEGACY_SAMPLE_FILES = {
    "fallback_real_pipeline_to_baseline.json": "fallback",
    "failure_generate_outputs.json": "failure",
}


@pytest.fixture(autouse=True)
def _default_parser_mode_for_task_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PARSER_LEGACY_FALLBACK", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GIS_AGENT_LLM_PLANNER_LEGACY_FALLBACK", "true")
    monkeypatch.delenv("GIS_AGENT_LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _load_task_sample(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def _load_task_suite() -> list[dict[str, Any]]:
    return json.loads(TASK_SUITE_PATH.read_text(encoding="utf-8"))


def _fake_candidates() -> list[CatalogCandidate]:
    return [
        CatalogCandidate(
            dataset_name="sentinel2",
            collection_id="sentinel-2-l2a",
            scene_count=5,
            coverage_ratio=0.92,
            effective_pixel_ratio_estimate=0.78,
            cloud_metric_summary={"median": 12.0, "p75": 19.0},
            spatial_resolution=10,
            temporal_density_note="high",
            suitability_score=0.88,
            recommendation_rank=1,
            summary_json={"source": "sample_fixture"},
        ),
        CatalogCandidate(
            dataset_name="landsat89",
            collection_id="landsat-c2-l2",
            scene_count=3,
            coverage_ratio=0.84,
            effective_pixel_ratio_estimate=0.69,
            cloud_metric_summary={"median": 18.0, "p75": 26.0},
            spatial_resolution=30,
            temporal_density_note="medium",
            suitability_score=0.74,
            recommendation_rank=2,
            summary_json={"source": "sample_fixture"},
        ),
    ]


def _cleanup_session(session_id: str) -> None:
    with SessionLocal() as db:
        task_ids = [
            task_id
            for (task_id,) in db.query(TaskRunRecord.id).filter(TaskRunRecord.session_id == session_id).all()
        ]

        if task_ids:
            db.query(ArtifactRecord).filter(ArtifactRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskEventRecord).filter(TaskEventRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskStepRecord).filter(TaskStepRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(DatasetCandidateRecord).filter(DatasetCandidateRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(AOIRecord).filter(AOIRecord.task_id.in_(task_ids)).delete(synchronize_session=False)
            db.query(TaskSpecRecord).filter(TaskSpecRecord.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )
            db.query(TaskRunRecord).filter(TaskRunRecord.id.in_(task_ids)).delete(synchronize_session=False)

        db.query(MessageRecord).filter(MessageRecord.session_id == session_id).delete(synchronize_session=False)
        db.query(UploadedFileRecord).filter(UploadedFileRecord.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
        db.commit()


def _patch_sample_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    scenario: str,
) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)
    for module in (agent_runtime, runtime_helpers):
        monkeypatch.setattr(
            module,
            "build_artifact_path",
            lambda task_id, filename: str(tmp_path / f"{task_id}_{filename}"),
        )
        monkeypatch.setattr(module, "search_candidates", lambda spec, aoi: _fake_candidates())
        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: SimpleNamespace(real_pipeline_enabled=False),
        )

    if scenario == "real_pipeline_to_baseline_fallback":
        for module in (agent_runtime, runtime_helpers):
            monkeypatch.setattr(
                module,
                "get_settings",
                lambda: SimpleNamespace(real_pipeline_enabled=True),
            )

        def _raise_real_pipeline(**_: object) -> object:
            raise RuntimeError("simulated real pipeline failure")

        monkeypatch.setattr(agent_runtime, "run_real_ndvi_pipeline", _raise_real_pipeline)
        monkeypatch.setattr(runtime_helpers, "run_real_ndvi_pipeline", _raise_real_pipeline)
    elif scenario == "generate_outputs_failure":

        def _raise_persist_artifact_file(
            task_id: str,
            filename: str,
            source_path: str,
            *,
            content_type: str | None = None,
        ) -> tuple[str, int, str]:
            del task_id, filename, source_path, content_type
            raise RuntimeError("simulated artifact metadata failure")

        monkeypatch.setattr(agent_runtime, "persist_artifact_file", _raise_persist_artifact_file)
        monkeypatch.setattr(runtime_helpers, "persist_artifact_file", _raise_persist_artifact_file)


def _create_task(db, session_id: str, message: str) -> object:
    payload = MessageCreateRequest(session_id=session_id, content=message, file_ids=[])
    return orchestrator.create_message_and_task(db, payload)


def _capture_task_state(task_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        detail = orchestrator.get_task_detail(db, task_id)
        _, events = orchestrator.list_task_events(db, task_id=task_id, since_id=0)
        task = db.get(TaskRunRecord, task_id)
        assert task is not None

        return {
            "detail": detail,
            "task": {
                "task_id": task.id,
                "parent_task_id": task.parent_task_id,
                "status": task.status,
                "error_code": task.error_code,
                "error_message": task.error_message,
                "fallback_used": task.fallback_used,
                "selected_dataset": task.selected_dataset,
            },
            "step_status": {step.step_name: step.status for step in task.steps},
            "event_types": [event.event_type for event in sorted(events, key=lambda item: item.id)],
            "event_detail": {
                event.event_type: event.detail_json for event in sorted(events, key=lambda item: item.id)
            },
            "artifact_types": [artifact.artifact_type for artifact in detail.artifacts],
        }


def _assert_expected(result: dict[str, Any]) -> None:
    sample = result["sample"]
    expected = sample["expected"]
    detail = result["detail"]
    task_state = result["task"]
    detail_task_spec = detail.task_spec or {}

    assert detail.status == expected["status"]

    if "task" in expected:
        expected_task = expected["task"]
        if "selected_dataset" in expected_task:
            assert task_state["selected_dataset"] == expected_task["selected_dataset"]
        if "fallback_used" in expected_task:
            assert task_state["fallback_used"] is expected_task["fallback_used"]
        if "error_code" in expected_task:
            assert task_state["error_code"] == expected_task["error_code"]

    if "error_code" in expected:
        assert detail.error_code == expected["error_code"]

    if expected.get("parent_task") == "initial":
        assert detail.parent_task_id == result["initial_task_id"]

    if "task_spec" in expected:
        task_spec = expected["task_spec"]
        if "aoi_input" in task_spec:
            assert detail_task_spec.get("aoi_input") == task_spec["aoi_input"]
        if "aoi_source_type" in task_spec:
            assert detail_task_spec.get("aoi_source_type") == task_spec["aoi_source_type"]
        if "analysis_type" in task_spec:
            assert detail_task_spec.get("analysis_type") == task_spec["analysis_type"]
        if "requested_dataset" in task_spec:
            assert detail_task_spec.get("requested_dataset") == task_spec["requested_dataset"]
        if "user_priority" in task_spec:
            assert detail_task_spec.get("user_priority") == task_spec["user_priority"]
        if "time_range" in task_spec:
            assert detail_task_spec.get("time_range") == task_spec["time_range"]
        if "operation_params" in task_spec:
            assert detail_task_spec.get("operation_params") == task_spec["operation_params"]
        if "operation_params_contains" in task_spec:
            actual_params = detail_task_spec.get("operation_params") or {}
            for param_key, param_value in task_spec["operation_params_contains"].items():
                assert actual_params.get(param_key) == param_value
        for output in task_spec.get("preferred_output_contains", []):
            assert output in (detail_task_spec.get("preferred_output") or [])
        for missing_field in task_spec.get("missing_fields_contains", []):
            assert missing_field in (detail_task_spec.get("missing_fields") or [])

    for step_name, step_status in expected.get("step_status", {}).items():
        assert result["step_status"][step_name] == step_status

    for event_type in expected.get("event_types", []):
        assert event_type in result["event_types"]

    for artifact_type in expected.get("artifacts_include", []):
        assert artifact_type in result["artifact_types"]

    if "clarification_message_contains" in expected:
        assert expected["clarification_message_contains"] in (detail.clarification_message or "")

    for event_type, detail_expectation in expected.get("event_detail", {}).items():
        assert event_type in result["event_detail"]
        event_detail = result["event_detail"][event_type] or {}
        for key, value in detail_expectation.items():
            assert event_detail.get(key) == value


@pytest.fixture
def sample_task_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    created_session_ids: list[str] = []

    def _run(sample_input: str | dict[str, Any]) -> dict[str, Any]:
        sample = _load_task_sample(sample_input) if isinstance(sample_input, str) else sample_input
        _patch_sample_environment(monkeypatch, tmp_path, scenario=sample["scenario"])

        initial_task_id: str | None = None
        target_task_id: str
        session_id: str

        with SessionLocal() as db:
            session = orchestrator.create_session(db)
            created_session_ids.append(session.session_id)
            session_id = session.session_id

            if sample["scenario"] in {"create_only", "create_and_run", "real_pipeline_to_baseline_fallback", "generate_outputs_failure"}:
                response = _create_task(db, session_id, sample["message"])
                target_task_id = response.task_id
            elif sample["scenario"] == "followup_message":
                initial_response = _create_task(db, session_id, sample["initial_message"])
                initial_task_id = initial_response.task_id
                followup_response = _create_task(db, session_id, sample["followup_message"])
                target_task_id = followup_response.task_id
            elif sample["scenario"] == "rerun_task":
                initial_response = _create_task(db, session_id, sample["initial_message"])
                initial_task_id = initial_response.task_id
                rerun_detail = orchestrator.rerun_task(db, initial_task_id, sample["override"])
                target_task_id = rerun_detail.task_id
            else:  # pragma: no cover - defensive guard
                raise AssertionError(f"Unsupported sample scenario: {sample['scenario']}")

        should_run_pipeline = sample.get(
            "run_pipeline",
            sample["scenario"] in {"real_pipeline_to_baseline_fallback", "generate_outputs_failure"},
        )
        if should_run_pipeline:
            run_task_graph(target_task_id)

        snapshot = _capture_task_state(target_task_id)
        snapshot["sample"] = sample
        snapshot["session_id"] = session_id
        snapshot["initial_task_id"] = initial_task_id
        return snapshot

    yield _run

    for session_id in created_session_ids:
        _cleanup_session(session_id)


def test_task_sample_suite_has_required_coverage() -> None:
    suite = _load_task_suite()
    categories = {sample["category"] for sample in suite} | set(LEGACY_SAMPLE_FILES.values())
    analysis_types: set[str] = set()
    for sample in suite:
        expected_task_spec = (sample.get("expected") or {}).get("task_spec") or {}
        if expected_task_spec.get("analysis_type"):
            analysis_types.add(str(expected_task_spec["analysis_type"]))

        override = sample.get("override") or {}
        raw_analysis_type = override.get("analysis_type")
        if raw_analysis_type:
            analysis_types.add(str(raw_analysis_type).strip().upper().replace("-", "_"))

    assert len(suite) + len(LEGACY_SAMPLE_FILES) >= 20
    assert {"success", "failure", "fallback", "followup", "export"}.issubset(categories)
    assert {"NDWI", "SLOPE_ASPECT", "BUFFER"}.issubset(analysis_types)


@pytest.mark.parametrize("sample", _load_task_suite(), ids=lambda sample: sample["id"])
def test_task_sample_suite(sample_task_runner, sample: dict[str, Any]) -> None:
    result = sample_task_runner(sample)
    _assert_expected(result)


def test_sample_task_fallback_real_pipeline_to_baseline(sample_task_runner) -> None:
    result = sample_task_runner("fallback_real_pipeline_to_baseline.json")
    _assert_expected(result)
    assert "fallback" in (result["detail"].methods_text or "").lower()
    assert "fallback" in (result["detail"].summary_text or "").lower()


def test_sample_task_failure_generate_outputs(sample_task_runner) -> None:
    result = sample_task_runner("failure_generate_outputs.json")
    _assert_expected(result)
    assert "simulated artifact metadata failure" in (result["task"]["error_message"] or "")


def test_task_detail_exposes_candidate_compare_fields(sample_task_runner) -> None:
    sample = next(sample for sample in _load_task_suite() if sample["id"] == "success_bbox_geotiff")

    result = sample_task_runner(sample)
    detail = result["detail"]

    assert len(detail.candidates) >= 2
    first_candidate = detail.candidates[0]

    assert first_candidate.collection_id == "sentinel-2-l2a"
    assert first_candidate.cloud_metric_summary == {"median": 12.0, "p75": 19.0}
    assert first_candidate.summary_json == {"source": "sample_fixture"}
    assert detail.aoi_bbox_bounds == [116.1, 39.8, 116.5, 40.1]
    assert detail.aoi_area_km2 is not None
    assert detail.task_plan is not None
    assert detail.task_plan.mode == "agent_driven_gis_workspace"
    assert [step.step_name for step in detail.task_plan.steps][:2] == ["plan_task", "normalize_aoi"]
    assert any(event_type == "task_plan_built" for event_type in result["event_types"])


def test_runtime_skips_clarification_tasks_without_failing(sample_task_runner) -> None:
    sample = next(sample for sample in _load_task_suite() if sample["id"] == "clarification_missing_time")
    sample = {**sample, "run_pipeline": True}

    result = sample_task_runner(sample)

    assert result["detail"].status == "waiting_clarification"
    assert "task_waiting_clarification" in result["event_types"]
    assert "task_failed" not in result["event_types"]


def test_create_task_writes_back_parser_schema_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)

    with SessionLocal() as db:
        session = orchestrator.create_session(db)
        session_id = session.session_id
        db.commit()

    try:
        monkeypatch.setattr(
            orchestrator,
            "parse_task_message",
            lambda message, has_upload: ParsedTaskSpec(  # noqa: ARG005
                need_confirmation=True,
                missing_fields=["aoi", "time_range"],
                clarification_message="任务解析失败，请补充 AOI 与时间范围后重试。",
                created_from="llm_parse_failed:schema_validation_failed",
            ),
        )

        with SessionLocal() as db:
            response = _create_task(db, session_id, "测试 parser 失败回写")
            detail = orchestrator.get_task_detail(db, response.task_id)

        assert detail.status == "waiting_clarification"
        assert detail.error_code == ErrorCode.TASK_LLM_PARSER_SCHEMA_VALIDATION_FAILED
        assert detail.error_message is not None
    finally:
        _cleanup_session(session_id)


def test_create_task_writes_back_planner_schema_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_queue_or_run", lambda task_id: None)

    with SessionLocal() as db:
        session = orchestrator.create_session(db)
        session_id = session.session_id
        db.commit()

    try:
        monkeypatch.setattr(
            orchestrator,
            "parse_task_message",
            lambda message, has_upload: ParsedTaskSpec(  # noqa: ARG005
                aoi_input="bbox(116.1,39.8,116.5,40.1)",
                aoi_source_type="bbox",
                time_range={"start": "2024-06-01", "end": "2024-06-30"},
                need_confirmation=False,
                missing_fields=[],
            ),
        )
        monkeypatch.setattr(
            orchestrator,
            "build_task_plan",
            lambda parsed, task_id=None: TaskPlan(  # noqa: ARG005
                version="agent-v2",
                mode="llm_plan_execute_gis_workspace",
                status=PLAN_STATUS_NEEDS_CLARIFICATION,
                objective="planner failed",
                reasoning_summary="planner schema failed",
                missing_fields=["planning_context"],
                steps=[],
                error_code=ErrorCode.TASK_LLM_PLANNER_SCHEMA_VALIDATION_FAILED,
                error_message="Planner LLM 输出未通过 schema 校验，任务进入待澄清状态。",
            ),
        )

        with SessionLocal() as db:
            response = _create_task(db, session_id, "测试 planner 失败回写")
            detail = orchestrator.get_task_detail(db, response.task_id)

        assert detail.status == "waiting_clarification"
        assert detail.error_code == ErrorCode.TASK_LLM_PLANNER_SCHEMA_VALIDATION_FAILED
        assert detail.error_message is not None
    finally:
        _cleanup_session(session_id)


def test_runtime_writes_back_recommendation_schema_error_code(
    sample_task_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module in (agent_runtime, runtime_helpers):
        monkeypatch.setattr(
            module,
            "build_recommendation",
            lambda *args, **kwargs: {  # noqa: ARG005
                "primary_dataset": "unknown",
                "backup_dataset": None,
                "scores": {},
                "reason": "推荐阶段失败（schema_validation_failed）",
                "risk_note": "当前无法生成可靠推荐结果。",
                "confidence": None,
                "error_code": ErrorCode.TASK_LLM_RECOMMENDATION_SCHEMA_VALIDATION_FAILED,
                "error_message": "Recommendation LLM 输出未通过 schema 校验。",
            },
        )

    sample = next(sample for sample in _load_task_suite() if sample["id"] == "success_bbox_geotiff")
    sample = {**sample, "run_pipeline": True}
    result = sample_task_runner(sample)

    assert result["detail"].status == "failed"
    assert result["task"]["error_code"] == ErrorCode.TASK_LLM_RECOMMENDATION_SCHEMA_VALIDATION_FAILED
    assert "task_failed" in result["event_types"]
