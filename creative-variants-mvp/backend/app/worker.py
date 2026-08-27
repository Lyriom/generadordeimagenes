import os
from celery import Celery
from pydantic import BaseModel
from typing import Dict, Any

broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

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
)

@celery_app.task(bind=True, name="generate_variants_task")
def generate_variants_task(self, project_id: str, generation_request_dict: Dict[str, Any]):
    from app.services.variants import generate_variants
    from app.models.schemas import GenerationRequest

    request = GenerationRequest(**generation_request_dict)
    self.update_state(state="PROGRESS", meta={"progress": 0, "status": "Iniciando generación..."})
    
    variants = generate_variants(project_id, request)
    return {"status": "COMPLETED", "variants_count": len(variants)}

@celery_app.task(bind=True, name="auto_task")
def auto_task(self, project_id: str, request_dict: Dict[str, Any]):
    from app.services.storage import get_project, save_project
    from app.services.autopilot import run
    from app.models.schemas import AutoRequest
    
    project = get_project(project_id)
    if not project:
        return {"status": "FAILED", "error": "Project not found"}
        
    request = AutoRequest(**request_dict)
    self.update_state(state="PROGRESS", meta={"progress": 0, "status": "Iniciando modo automático..."})
    
    steps, variants, warnings = run(project, request)
    save_project(project)
    
    return {
        "status": "COMPLETED",
        "variants_count": len(variants),
        "steps": [s.model_dump() for s in steps],
        "warnings": warnings,
        "variants": [v.model_dump() for v in variants]
    }
