from geoalchemy2.elements import WKTElement

from packages.domain.models import AOIRecord, TaskRunRecord, TaskSpecRecord
from packages.domain.services.orchestrator import should_inherit_original_aoi
from packages.schemas.task import ParsedTaskSpec


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
