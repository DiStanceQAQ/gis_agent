from geoalchemy2.elements import WKTElement

from packages.domain.models import AOIRecord, TaskRunRecord, TaskSpecRecord, UploadedFileRecord
from packages.domain.services.input_slots import classify_uploaded_inputs
from packages.domain.services.orchestrator import (
    _build_initial_operation_plan,
    _build_initial_revision_understanding,
    _merge_task_spec,
    should_inherit_original_aoi,
)
from packages.schemas.task import ParsedTaskSpec, TaskPlanResponse


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
