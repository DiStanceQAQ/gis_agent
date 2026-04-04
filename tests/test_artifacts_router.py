from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.database import SessionLocal
from packages.domain.models import ArtifactRecord, MessageRecord, SessionRecord, TaskRunRecord
from packages.domain.services.storage import persist_artifact_file
from packages.domain.utils import make_id


def _cleanup_records(*, artifact_id: str, task_id: str, message_id: str, session_id: str) -> None:
    with SessionLocal() as db:
        db.query(ArtifactRecord).filter(ArtifactRecord.id == artifact_id).delete(synchronize_session=False)
        db.query(TaskRunRecord).filter(TaskRunRecord.id == task_id).delete(synchronize_session=False)
        db.query(MessageRecord).filter(MessageRecord.id == message_id).delete(synchronize_session=False)
        db.query(SessionRecord).filter(SessionRecord.id == session_id).delete(synchronize_session=False)
        db.commit()


def test_artifact_download_and_metadata_endpoint(tmp_path: Path) -> None:
    session_id = make_id("ses")
    message_id = make_id("msg")
    task_id = make_id("task")
    artifact_id = make_id("art")

    source_file = tmp_path / "report.csv"
    source_file.write_text("name,value\nndvi,0.42\n", encoding="utf-8")
    storage_key, size_bytes, checksum = persist_artifact_file(
        task_id,
        "report.csv",
        str(source_file),
        content_type="text/csv",
    )

    with SessionLocal() as db:
        db.add(SessionRecord(id=session_id, title="artifact-test", status="active"))
        db.add(
            MessageRecord(
                id=message_id,
                session_id=session_id,
                role="user",
                content="artifact route test",
            )
        )
        db.add(
            TaskRunRecord(
                id=task_id,
                session_id=session_id,
                user_message_id=message_id,
                status="success",
                analysis_type="NDVI",
            )
        )
        db.add(
            ArtifactRecord(
                id=artifact_id,
                task_id=task_id,
                artifact_type="csv",
                storage_key=storage_key,
                mime_type="text/csv",
                size_bytes=size_bytes,
                checksum=checksum,
                metadata_json={
                    "projection": "EPSG:4326",
                    "source_step": "export_report",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        )
        db.commit()

    try:
        with TestClient(app) as client:
            download_response = client.get(f"/api/v1/artifacts/{artifact_id}")
            assert download_response.status_code == 200
            assert download_response.headers["x-artifact-type"] == "csv"
            assert download_response.headers["x-artifact-checksum"] == checksum
            assert download_response.headers["x-artifact-source-step"] == "export_report"
            assert download_response.headers["x-artifact-projection"] == "EPSG:4326"
            assert "attachment;" in download_response.headers["content-disposition"]
            assert download_response.text == "name,value\nndvi,0.42\n"

            metadata_response = client.get(f"/api/v1/artifacts/{artifact_id}/metadata")
            assert metadata_response.status_code == 200
            payload = metadata_response.json()
            assert payload["artifact_id"] == artifact_id
            assert payload["artifact_type"] == "csv"
            assert payload["checksum"] == checksum
            assert payload["metadata"]["source_step"] == "export_report"
            assert payload["download_url"] == f"/api/v1/artifacts/{artifact_id}"
    finally:
        _cleanup_records(
            artifact_id=artifact_id,
            task_id=task_id,
            message_id=message_id,
            session_id=session_id,
        )
