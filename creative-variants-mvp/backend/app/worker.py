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
    warnings: list[str] = []

    # Un arte plano solo se puede colocar entero, así que en una proporción
    # distinta a la suya sale encogido sobre un relleno de color. Antes de dar
    # eso por imposible se intenta lo que el motor sabe hacer: separarlo en
    # capas (copy con OCR, objetos con SAM, fondo reconstruido) para poder
    # recomponerlo de verdad. Solo se hace si hace falta para lo que se pidió.
    from app.services import background_expand, layout_engine, separation

    pedidos = list(getattr(request, "formats", None) or [])
    if pedidos and separation.needs_separation(project):
        if layout_engine.formats_needing_recompose(project, pedidos):
            self.update_state(
                state="PROGRESS",
                meta={
                    "progress": 8,
                    "status": "Separando el arte en capas para poder recomponerlo…",
                },
            )
            piezas, separation_warnings = separation.separate(project)
            warnings.extend(separation_warnings)
            storage.save_project(project)
            warnings.append(
                f"El arte llegó plano y los formatos pedidos necesitan recomponerlo: "
                f"se separó en {piezas} elemento(s) antes de generar."
            )

    # El fondo de los formatos que el arte no cubre se extiende **antes** de
    # componer: es una llamada de red por lienzo, y hacerla dentro del render
    # sería una por pieza. Se guarda en disco, así que una tanda de veinte
    # productos sobre el mismo KV la paga una vez por formato.
    from app.models.schemas import SUPPORTED_FORMATS

    lienzos = {
        SUPPORTED_FORMATS[fmt] for fmt in pedidos if fmt in SUPPORTED_FORMATS
    }
    por_extender = [
        (ancho, alto)
        for ancho, alto in sorted(lienzos)
        if background_expand.cached(project, ancho, alto) is None
        and background_expand.cover_upscale(
            (project.canvas.width, project.canvas.height), ancho, alto
        )
        > background_expand.SHARP_UPSCALE
    ]
    for indice, (ancho, alto) in enumerate(por_extender):
        self.update_state(
            state="PROGRESS",
            meta={
                "progress": 12 + int(8 * indice / max(1, len(por_extender))),
                "status": f"Extendiendo el fondo a {ancho}x{alto}…",
            },
        )
        _, expand_warnings = background_expand.expand(project, ancho, alto)
        warnings.extend(expand_warnings)

    variants, generate_warnings = generate_variants(project, request)
    warnings.extend(generate_warnings)
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
