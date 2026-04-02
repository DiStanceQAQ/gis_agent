from apps.worker.celery_app import celery_app
from packages.domain.services.agent_runtime import run_task_runtime


@celery_app.task(name="gis_agent.run_task")
def run_task_async(task_id: str) -> None:
    run_task_runtime(task_id)
