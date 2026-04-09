import json
from pathlib import Path

from geoalchemy2.elements import WKTElement
import pytest
from sqlalchemy.orm import Session

from packages.domain.database import SessionLocal
from packages.domain.models import (
    AOIRecord,
    MessageRecord,
    SessionMemoryEventRecord,
    SessionRecord,
    TaskRunRecord,
    TaskSpecRecord,
    TaskSpecRevisionRecord,
    UploadedFileRecord,
)
from packages.domain.services.input_slots import classify_uploaded_inputs
from packages.domain.services.orchestrator import (
    _build_task,
    _build_initial_operation_plan,
    _build_initial_revision_understanding,
    _merge_task_spec,
    create_message_and_task,
    get_task_detail,
    should_inherit_original_aoi,
)
from packages.domain.services.planner import TaskPlan, TaskPlanStep
from packages.domain.services.task_revisions import get_active_revision
from packages.domain.utils import make_id
from packages.schemas.message import MessageCreateRequest
from packages.schemas.task import ParsedTaskSpec, TaskPlanResponse


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as db:
        yield db


def _seed_task_with_revision_history(
    db_session: Session,
    *,
    interaction_state: str = "understanding",
    last_response_mode: str = "show_revision",
    blocked_active_revision: bool = False,
) -> TaskRunRecord:
    session = SessionRecord(id=make_id("ses"), title="detail-tests", status="active")
    first_message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="江西")
    second_message = MessageRecord(
        id=make_id("msg"),
        session_id=session.id,
        role="user",
        content="改成北京",
    )
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session.id,
        user_message_id=first_message.id,
        interaction_state=interaction_state,
        last_response_mode=last_response_mode,
        status="waiting_clarification",
        analysis_type="WORKFLOW",
    )
    task.task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="北京",
        aoi_source_type="admin_name",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={"aoi_input": "北京", "aoi_source_type": "admin_name"},
    )
    rev1 = TaskSpecRevisionRecord(
        id=make_id("rev"),
        task_id=task.id,
        revision_number=1,
        base_revision_id=None,
        source_message_id=first_message.id,
        change_type="initial_parse",
        is_active=False,
        understanding_intent="new_task",
        understanding_summary="江西作为 AOI。",
        raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
        field_confidences_json={"aoi_input": {"score": 0.61, "level": "medium", "evidence": []}},
        ranked_candidates_json={"aoi_input": [{"value": "江西", "score": 0.61}]},
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
        is_active=True,
        understanding_intent="task_correction",
        understanding_summary="北京作为 AOI。",
        raw_spec_json={"aoi_input": "北京", "aoi_source_type": "admin_name"},
        field_confidences_json={"aoi_input": {"score": 0.97, "level": "high", "evidence": []}},
        ranked_candidates_json={"aoi_input": [{"value": "北京", "score": 0.97}]},
        response_mode="show_revision",
        understanding_trace_json={},
        execution_blocked=blocked_active_revision,
        execution_blocked_reason=(
            "AOI normalization failed: uploaded boundary is invalid"
            if blocked_active_revision
            else None
        ),
    )
    db_session.add_all([session, first_message, second_message, task, rev1, rev2])
    db_session.flush()
    return task


def test_should_inherit_original_aoi_when_only_time_changes() -> None:
    original_task = TaskRunRecord(id="task_1", session_id="ses_1", user_message_id="msg_1")
    original_task.task_spec = TaskSpecRecord(
        task_id="task_1",
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={},
    )
    original_task.aoi = AOIRecord(
        id="aoi_1",
        task_id="task_1",
        geom=WKTElement(
            "MULTIPOLYGON(((116.1 39.8,116.5 39.8,116.5 40.1,116.1 40.1,116.1 39.8)))",
            srid=4326,
        ),
        bbox=WKTElement("POLYGON((116.1 39.8,116.5 39.8,116.5 40.1,116.1 40.1,116.1 39.8))", srid=4326),
        bbox_bounds_json=[116.1, 39.8, 116.5, 40.1],
        is_valid=True,
    )

    parsed = ParsedTaskSpec(
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        time_range={"start": "2023-06-01", "end": "2023-08-31"},
    )

    assert should_inherit_original_aoi(original_task, parsed, {"time_range": parsed.time_range}) is True


def test_should_not_inherit_original_aoi_when_aoi_changes() -> None:
    original_task = TaskRunRecord(id="task_1", session_id="ses_1", user_message_id="msg_1")
    original_task.task_spec = TaskSpecRecord(
        task_id="task_1",
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={},
    )
    original_task.aoi = AOIRecord(
        id="aoi_1",
        task_id="task_1",
        bbox_bounds_json=[116.1, 39.8, 116.5, 40.1],
        is_valid=True,
    )

    parsed = ParsedTaskSpec(
        aoi_input="bbox(117.0, 39.7, 117.2, 39.9)",
        aoi_source_type="bbox",
        time_range={"start": "2023-06-01", "end": "2023-08-31"},
    )

    assert should_inherit_original_aoi(original_task, parsed, {"aoi_input": parsed.aoi_input}) is False


def test_merge_task_spec_preserves_existing_outputs_when_followup_adds_geotiff() -> None:
    merged = _merge_task_spec(
        {
            "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
            "aoi_source_type": "bbox",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "NDVI",
            "preferred_output": ["png_map", "methods_text"],
            "user_priority": "balanced",
            "need_confirmation": False,
            "missing_fields": [],
            "clarification_message": None,
            "created_from": "2026-04-01",
        },
        {"preferred_output": ["geotiff"]},
    )

    assert merged.preferred_output == ["png_map", "methods_text", "geotiff"]


def test_merge_task_spec_applies_followup_time_override() -> None:
    merged = _merge_task_spec(
        {
            "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
            "aoi_source_type": "bbox",
            "time_range": {"start": "2024-06-01", "end": "2024-06-30"},
            "analysis_type": "NDVI",
            "preferred_output": ["png_map", "methods_text"],
            "user_priority": "balanced",
            "need_confirmation": False,
            "missing_fields": [],
            "clarification_message": None,
            "created_from": "2026-04-01",
        },
        {"time_range": {"start": "2023-06-01", "end": "2023-06-30"}},
    )

    assert merged.time_range == {"start": "2023-06-01", "end": "2023-06-30"}


def test_build_initial_revision_understanding_uses_canonical_followup_intent() -> None:
    understanding = _build_initial_revision_understanding(
        task_spec_raw={
            "aoi_input": "bbox(116.1,39.8,116.5,40.1)",
            "aoi_source_type": "bbox",
            "upload_slots": {"input_vector_file_id": "file_123"},
        },
        parent_task_id="task_parent",
    )

    assert understanding.intent == "task_followup"


def test_classify_uploaded_inputs_prefers_raster_and_vector_slots() -> None:
    files = [
        type("F", (), {"id": "f1", "file_type": "raster_tiff"})(),
        type("F", (), {"id": "f2", "file_type": "shp"})(),
    ]
    slots = classify_uploaded_inputs(files)
    assert slots["input_raster_file_id"] == "f1"
    assert slots["input_vector_file_id"] == "f2"


def test_build_initial_operation_plan_prefers_planner_operation_nodes() -> None:
    task_plan = TaskPlanResponse(
        version="agent-v2",
        mode="llm_plan_execute_gis_workspace",
        status="ready",
        objective="test",
        reasoning_summary="test",
        missing_fields=[],
        steps=[],
        operation_plan_nodes=[
            {
                "step_id": "step_1_raster_reproject",
                "op_name": "raster.reproject",
                "depends_on": [],
                "inputs": {},
                "params": {"source_path": "/tmp/source.tif", "target_crs": "EPSG:3857"},
                "outputs": {"raster": "r_proj"},
                "retry_policy": {"max_retries": 0},
            },
            {
                "step_id": "step_2_artifact_export",
                "op_name": "artifact.export",
                "depends_on": ["step_1_raster_reproject"],
                "inputs": {"primary": "r_proj"},
                "params": {"formats": ["geotiff"]},
                "outputs": {"artifact": "a_out"},
                "retry_policy": {"max_retries": 0},
            },
        ],
    )
    parsed = ParsedTaskSpec(
        aoi_input="bbox(116.1,39.8,116.5,40.1)",
        aoi_source_type="bbox",
        time_range={"start": "2024-06-01", "end": "2024-06-30"},
    )

    operation_plan = _build_initial_operation_plan(task_plan, parsed)
    assert [node.op_name for node in operation_plan.nodes] == ["raster.reproject", "artifact.export"]


def test_build_initial_operation_plan_injects_uploaded_source_paths() -> None:
    task_plan = TaskPlanResponse(
        version="agent-v2",
        mode="llm_plan_execute_gis_workspace",
        status="ready",
        objective="clip",
        reasoning_summary="clip",
        missing_fields=[],
        steps=[],
        operation_plan_nodes=[
            {
                "step_id": "step_1_upload_raster",
                "op_name": "input.upload_raster",
                "depends_on": [],
                "inputs": {"upload_id": "file_raster"},
                "params": {},
                "outputs": {"raster": "r_src"},
                "retry_policy": {"max_retries": 0},
            },
            {
                "step_id": "step_2_upload_vector",
                "op_name": "input.upload_vector",
                "depends_on": [],
                "inputs": {"upload_id": "file_vector"},
                "params": {},
                "outputs": {"vector": "v_clip"},
                "retry_policy": {"max_retries": 0},
            },
            {
                "step_id": "step_3_clip",
                "op_name": "raster.clip",
                "depends_on": ["step_1_upload_raster", "step_2_upload_vector"],
                "inputs": {"raster": "r_src"},
                "params": {"crop": True},
                "outputs": {"raster": "r_out"},
                "retry_policy": {"max_retries": 0},
            },
        ],
    )
    parsed = ParsedTaskSpec(
        analysis_type="CLIP",
        aoi_input="uploaded_aoi",
        aoi_source_type="file_upload",
        operation_params={"source_path": "/tmp/source.tif"},
    )
    uploaded_files = [
        UploadedFileRecord(
            id="file_raster",
            session_id="ses_1",
            original_name="source.tif",
            file_type="raster_tiff",
            storage_key="/tmp/source.tif",
            size_bytes=1,
            checksum="checksum_raster",
        ),
        UploadedFileRecord(
            id="file_vector",
            session_id="ses_1",
            original_name="clip.geojson",
            file_type="geojson",
            storage_key="/tmp/clip.geojson",
            size_bytes=1,
            checksum="checksum_vector",
        ),
    ]

    operation_plan = _build_initial_operation_plan(
        task_plan,
        parsed,
        uploaded_files=uploaded_files,
        upload_slots={"input_raster_file_id": "file_raster", "input_vector_file_id": "file_vector"},
    )
    nodes = operation_plan.model_dump()["nodes"]

    assert nodes[0]["params"]["source_path"] == "/tmp/source.tif"
    assert nodes[1]["params"]["source_path"] == "/tmp/clip.geojson"
    assert nodes[2]["params"]["aoi_path"] == "/tmp/clip.geojson"


def test_build_initial_operation_plan_injects_clip_paths_without_uploads() -> None:
    task_plan = TaskPlanResponse(
        version="agent-v2",
        mode="llm_plan_execute_gis_workspace",
        status="ready",
        objective="clip",
        reasoning_summary="clip",
        missing_fields=[],
        steps=[],
        operation_plan_nodes=[
            {
                "step_id": "step_1_upload_raster",
                "op_name": "input.upload_raster",
                "depends_on": [],
                "inputs": {"upload_id": "external_raster"},
                "params": {},
                "outputs": {"raster": "r_src"},
                "retry_policy": {"max_retries": 0},
            },
            {
                "step_id": "step_2_upload_vector",
                "op_name": "input.upload_vector",
                "depends_on": [],
                "inputs": {"upload_id": "external_vector"},
                "params": {},
                "outputs": {"vector": "v_clip"},
                "retry_policy": {"max_retries": 0},
            },
            {
                "step_id": "step_3_clip",
                "op_name": "raster.clip",
                "depends_on": ["step_1_upload_raster", "step_2_upload_vector"],
                "inputs": {"raster": "r_src"},
                "params": {"crop": True},
                "outputs": {"raster": "r_out"},
                "retry_policy": {"max_retries": 0},
            },
        ],
    )
    parsed = ParsedTaskSpec(
        analysis_type="CLIP",
        operation_params={
            "source_path": "/Users/ljn/gis_data/CACD-2020.tif",
            "clip_path": "/Users/ljn/gis_data/区划/xinjiang.geojson",
        },
    )

    operation_plan = _build_initial_operation_plan(task_plan, parsed, uploaded_files=None)
    nodes = operation_plan.model_dump()["nodes"]

    assert nodes[0]["params"]["source_path"].endswith("CACD-2020.tif")
    assert nodes[1]["params"]["source_path"].endswith("xinjiang.geojson")
    assert nodes[2]["params"]["aoi_path"].endswith("xinjiang.geojson")


def test_get_task_detail_returns_active_revision_and_revision_history(
    db_session: Session,
) -> None:
    task = _seed_task_with_revision_history(db_session)

    detail = get_task_detail(db_session, task.id)

    assert detail.interaction_state == "understanding"
    assert detail.last_response_mode == "show_revision"
    assert detail.active_revision is not None
    assert detail.active_revision.revision_id
    assert detail.active_revision.revision_number == 2
    assert detail.active_revision.change_type == "user_edit"
    assert detail.active_revision.created_at is not None
    assert detail.active_revision.understanding_summary == "北京作为 AOI。"
    assert detail.active_revision.execution_blocked is False
    assert detail.active_revision.execution_blocked_reason is None
    assert detail.active_revision.field_confidences["aoi_input"].score == pytest.approx(0.97)
    assert detail.active_revision.ranked_candidates["aoi_input"][0]["value"] == "北京"
    assert [revision.revision_number for revision in detail.revisions] == [1, 2]
    assert detail.revisions[0].change_type == "initial_parse"
    assert detail.revisions[0].created_at is not None
    assert detail.revisions[1].revision_id == detail.active_revision.revision_id


def test_get_task_detail_surfaces_execution_blocked_active_revision(
    db_session: Session,
) -> None:
    task = _seed_task_with_revision_history(
        db_session,
        interaction_state="execution_blocked",
        last_response_mode="ask_missing_fields",
        blocked_active_revision=True,
    )

    detail = get_task_detail(db_session, task.id)

    assert detail.interaction_state == "execution_blocked"
    assert detail.last_response_mode == "ask_missing_fields"
    assert detail.active_revision is not None
    assert detail.active_revision.execution_blocked is True
    assert detail.active_revision.execution_blocked_reason == (
        "AOI normalization failed: uploaded boundary is invalid"
    )


def test_get_task_detail_includes_lazy_materialized_initial_revision(
    db_session: Session,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="lazy-backfill", status="active")
    message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="江西")
    task = TaskRunRecord(
        id=make_id("task"),
        session_id=session.id,
        user_message_id=message.id,
        status="waiting_clarification",
        analysis_type="WORKFLOW",
    )
    task.task_spec = TaskSpecRecord(
        task_id=task.id,
        aoi_input="江西",
        aoi_source_type="admin_name",
        preferred_output=["png_map"],
        user_priority="balanced",
        need_confirmation=False,
        raw_spec_json={"aoi_input": "江西", "aoi_source_type": "admin_name"},
    )
    db_session.add_all([session, message, task])
    db_session.commit()

    detail = get_task_detail(db_session, task.id)

    assert detail.active_revision is not None
    assert detail.active_revision.revision_number == 1
    assert detail.active_revision.change_type == "legacy_backfill"
    assert [revision.revision_number for revision in detail.revisions] == [1]


def test_build_task_marks_invalid_uploaded_aoi_as_execution_blocked(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="invalid-upload", status="active")
    message = MessageRecord(id=make_id("msg"), session_id=session.id, role="user", content="上传边界")
    invalid_geojson = tmp_path / "invalid.geojson"
    invalid_geojson.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "bad-aoi"},
                        "geometry": {"type": "Point", "coordinates": [116.4, 39.9]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    uploaded = UploadedFileRecord(
        id=make_id("file"),
        session_id=session.id,
        original_name="invalid.geojson",
        file_type="geojson",
        storage_key=str(invalid_geojson),
        size_bytes=invalid_geojson.stat().st_size,
        checksum="checksum",
    )
    db_session.add_all([session, message, uploaded])
    db_session.flush()

    monkeypatch.setattr(
        "packages.domain.services.orchestrator.build_task_plan",
        lambda parsed, task_id=None, db_session=None: TaskPlan(  # noqa: ARG005
            version="agent-v2",
            mode="llm_plan_execute_gis_workspace",
            status="ready",
            objective="clip",
            reasoning_summary="clip",
            missing_fields=[],
            steps=[
                TaskPlanStep(
                    step_name="plan_task",
                    tool_name="planner.build",
                    title="规划任务",
                    purpose="生成任务计划",
                )
            ],
        ),
    )

    task = _build_task(
        db=db_session,
        session_id=session.id,
        user_message_id=message.id,
        parsed=ParsedTaskSpec(
            aoi_input="uploaded_aoi",
            aoi_source_type="file_upload",
            analysis_type="WORKFLOW",
            preferred_output=["png_map"],
            user_priority="balanced",
        ),
        uploaded_files=[uploaded],
        require_approval=False,
    )

    active_revision = get_active_revision(db_session, task.id)

    assert task.status == "waiting_clarification"
    assert task.interaction_state == "execution_blocked"
    assert task.task_spec is not None
    assert task.task_spec.need_confirmation is True
    assert "AOI normalization failed" in (task.task_spec.raw_spec_json.get("clarification_message") or "")
    assert active_revision is not None
    assert active_revision.execution_blocked is True
    assert active_revision.response_mode == "ask_missing_fields"


def test_create_message_and_task_surfaces_blocked_uploaded_aoi_as_clarification(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="blocked-response", status="active")
    invalid_geojson = tmp_path / "invalid.geojson"
    invalid_geojson.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "bad-aoi"},
                        "geometry": {"type": "Point", "coordinates": [116.4, 39.9]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    uploaded = UploadedFileRecord(
        id=make_id("file"),
        session_id=session.id,
        original_name="invalid.geojson",
        file_type="geojson",
        storage_key=str(invalid_geojson),
        size_bytes=invalid_geojson.stat().st_size,
        checksum="checksum",
    )
    db_session.add_all([session, uploaded])
    db_session.commit()

    monkeypatch.setattr(
        "packages.domain.services.orchestrator.parse_task_message",
        lambda message, has_upload, task_id=None, db_session=None: ParsedTaskSpec(  # noqa: ARG005
            aoi_input="uploaded_aoi",
            aoi_source_type="file_upload",
            analysis_type="WORKFLOW",
            preferred_output=["png_map"],
            user_priority="balanced",
        ),
    )
    monkeypatch.setattr(
        "packages.domain.services.orchestrator.build_task_plan",
        lambda parsed, task_id=None, db_session=None: TaskPlan(  # noqa: ARG005
            version="agent-v2",
            mode="llm_plan_execute_gis_workspace",
            status="ready",
            objective="clip",
            reasoning_summary="clip",
            missing_fields=[],
            steps=[
                TaskPlanStep(
                    step_name="plan_task",
                    tool_name="planner.build",
                    title="规划任务",
                    purpose="生成任务计划",
                )
            ],
        ),
    )

    response = create_message_and_task(
        db_session,
        MessageCreateRequest(
            session_id=session.id,
            content="用我上传的边界文件作为 AOI",
            file_ids=[uploaded.id],
        ),
        require_approval=False,
    )

    detail = get_task_detail(db_session, response.task_id or "")

    assert response.task_id is not None
    assert response.need_clarification is True
    assert response.task_status == "waiting_clarification"
    assert "AOI normalization failed" in (response.clarification_message or "")
    assert detail.interaction_state == "execution_blocked"
    assert detail.active_revision is not None
    assert detail.active_revision.execution_blocked is True


def test_create_message_and_task_records_message_received_session_memory_event(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SessionRecord(id=make_id("ses"), title="message-ingestion", status="active")
    db_session.add(session)
    db_session.commit()

    monkeypatch.setattr(
        "packages.domain.services.orchestrator.parse_task_message",
        lambda message, has_upload, task_id=None, db_session=None: ParsedTaskSpec(  # noqa: ARG005
            aoi_input="江西",
            aoi_source_type="admin_name",
            analysis_type="WORKFLOW",
            preferred_output=["png_map"],
            user_priority="balanced",
        ),
    )
    monkeypatch.setattr(
        "packages.domain.services.orchestrator.build_task_plan",
        lambda parsed, task_id=None, db_session=None: TaskPlan(  # noqa: ARG005
            version="agent-v2",
            mode="llm_plan_execute_gis_workspace",
            status="ready",
            objective="ndvi",
            reasoning_summary="ndvi",
            missing_fields=[],
            steps=[],
        ),
    )

    response = create_message_and_task(
        db_session,
        MessageCreateRequest(session_id=session.id, content="请分析江西植被"),
        require_approval=False,
    )

    assert response.message_id is not None
    event = (
        db_session.query(SessionMemoryEventRecord)
        .filter(
            SessionMemoryEventRecord.session_id == session.id,
            SessionMemoryEventRecord.message_id == response.message_id,
            SessionMemoryEventRecord.event_type == "message_received",
        )
        .one_or_none()
    )
    assert event is not None
