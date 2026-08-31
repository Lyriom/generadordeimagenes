"""Tareas Celery: la generación puede tardar minutos y no debe bloquear la API."""
import os
from typing import Any, Dict

from celery import Celery

broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

# En las pruebas no hay worker ni Redis: las tareas se ejecutan en el momento.
EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "").strip().lower() in {
    "1", "true", "yes", "on",
}

celery_app = Celery(
    "creative_variants",
    broker=broker_url,
    backend=result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_always_eager=EAGER,
    task_eager_propagates=EAGER,
    # Sin esto AsyncResult no encontraría el resultado de una tarea eager.
    task_store_eager_result=EAGER,
)


@celery_app.task(bind=True, name="generate_variants_task")
def generate_variants_task(self, project_id: str, generation_request_dict: Dict[str, Any]):
    from app.models.schemas import GenerateRequest
    from app.services import storage
    from app.services.variants import generate_variants

    request = GenerateRequest(**generation_request_dict)
    self.update_state(
        state="PROGRESS", meta={"progress": 0, "status": "Iniciando generación…"}
    )

    project = storage.load_project(project_id)
    variants, warnings = generate_variants(project, request)
    storage.save_project(project)
    return {
        "status": "COMPLETED",
        "project_id": project_id,
        "variants": [variant.model_dump(mode="json") for variant in variants],
        "variants_count": len(variants),
        "warnings": warnings,
    }


@celery_app.task(bind=True, name="auto_task")
def auto_task(self, project_id: str, auto_request_dict: Dict[str, Any]):
    """Modo automático completo: detectar, recortar, fondo (IA) y componer."""
    from app.models.schemas import AutoRequest
    from app.services import autopilot, storage

    request = AutoRequest(**auto_request_dict)
    self.update_state(
        state="PROGRESS", meta={"progress": 5, "status": "Preparando el proyecto…"}
    )

    project = storage.load_project(project_id)
    steps, variants, warnings = autopilot.run(project, request)
    storage.save_project(project)
    return {
        "status": "COMPLETED",
        "project_id": project_id,
        "steps": steps,
        "variants": [variant.model_dump(mode="json") for variant in variants],
        "warnings": warnings,
    }
