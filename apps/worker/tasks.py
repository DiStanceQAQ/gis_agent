from apps.worker.celery_app import celery_app
from packages.domain.services.graph.runner import run_task_graph


@celery_app.task(name="gis_agent.run_task")
def run_task_async(task_id: str) -> None:
    run_task_graph(task_id)
