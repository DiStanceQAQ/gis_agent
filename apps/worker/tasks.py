from apps.worker.celery_app import celery_app
from packages.domain.services.mock_pipeline import run_mock_task


@celery_app.task(name="gis_agent.run_mock_task")
def run_mock_task_async(task_id: str) -> None:
    run_mock_task(task_id)

