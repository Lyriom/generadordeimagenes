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
    # Una tanda puede tardar varios minutos. El broker solo la confirma al
    # terminar y la vuelve a entregar si el proceso muere, en vez de dejar un
    # manifiesto STARTED/PROGRESS huérfano para siempre. Un solo prefetched task
    # por worker además evita que una cola se vea "en curso" mientras aún espera
    # en memoria de otro proceso.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)


def _campaign_job_state(
    task: Any,
    job: Any,
    state: str,
    progress: int,
    detail: str,
    *,
    error: str | None = None,
) -> None:
    """Sincroniza Redis y el manifiesto en disco de una tanda de campaña.

    Redis permite actualizar el porcentaje rápido; el manifiesto es la fuente
    durable para que una recarga, una expiración del result backend o el
    reinicio del worker no borre el estado que ve el equipo.
    """

    from app.services import campaign_store

    job.state = state
    job.progress = max(0, min(100, int(progress)))
    job.detail = detail[:500]
    job.error = error[:2000] if error else None
    campaign_store.save_production_job(job)
    if state in {"STARTED", "PROGRESS"}:
        task.update_state(
            state=state,
            meta={
                "progress": job.progress,
                "status": job.detail,
                "batch_id": job.batch_id,
            },
        )


@celery_app.task(bind=True, name="produce_campaign_batch_task")
def produce_campaign_batch_task(self, client_id: str, campaign_id: str, task_id: str):
    """Renderiza una orden de campaña ya persistida por la API.

    Nunca recibe archivos ni una matriz por Celery: ambos viven en
    ``production/jobs/<task_id>`` antes de llegar aquí. Eso elimina los límites
    de serialización del broker y evita que un timeout HTTP cancele la tanda.
    """

    from app.models.campaign import Campaign
    from app.services import campaign_creative, campaign_store, production_matrix

    job = campaign_store.load_production_job(client_id, campaign_id, task_id)
    if job.state == "COMPLETED" and job.batch_id:
        # Un mensaje redeliverado no debe renderizar una segunda tanda.
        return {"batch_id": job.batch_id, "status": "COMPLETED"}

    _campaign_job_state(self, job, "STARTED", 4, "Preparando la orden de producción…")
    try:
        campaign = Campaign.model_validate(job.campaign_snapshot)
        rows = [production_matrix.MatrixRow.model_validate(row) for row in job.rows]
        products: dict[str, Any] = {}
        for filename, relative_path in job.product_files.items():
            target = campaign_store.campaign_path(client_id, campaign_id, relative_path)
            if not target.exists() or not target.is_file():
                raise campaign_creative.CampaignProductionError(
                    f"La imagen '{filename}' de esta tanda ya no está disponible."
                )
            if filename.casefold() in {name.casefold() for name in products}:
                raise campaign_creative.CampaignProductionError(
                    f"La orden contiene dos imágenes llamadas '{filename}'."
                )
            products[filename] = target

        def report(done: int, total: int, detail: str) -> None:
            # 6–96 deja una cola visual para el empaquetado final y evita que
            # la barra llegue a 100 % antes de que el ZIP exista.
            progress = 6 + int(90 * done / max(1, total))
            _campaign_job_state(self, job, "PROGRESS", progress, detail)

        batch = campaign_creative.produce_batch(
            campaign,
            job.brand_name,
            rows,
            products,
            matrix_filename=job.matrix_filename,
            default_formats=job.default_formats,
            use_ai_copy=job.use_ai_copy,
            on_progress=report,
        )
        job.batch_id = batch.batch_id
        _campaign_job_state(self, job, "COMPLETED", 100, "Entregables listos.")
        return {
            "batch_id": batch.batch_id,
            "status": "COMPLETED",
            "total_pieces": batch.total_pieces,
        }
    except Exception as exc:
        # Se conserva la causa por tanda; el navegador puede mostrarla aun si
        # Redis expira el resultado de Celery después de varios minutos.
        _campaign_job_state(
            self,
            job,
            "FAILED",
            job.progress,
            "La producción no pudo completarse.",
            error=str(exc),
        )
        raise


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
    source_canvas = (project.canvas.width, project.canvas.height)

    def _hace_falta_extender(ancho: int, alto: int) -> bool:
        cobertura = background_expand.cover_upscale(source_canvas, ancho, alto)
        if cobertura > background_expand.SHARP_UPSCALE:
            return True
        # El layout adaptativo reparte los bloques por todo el eje que creció,
        # y ese eje necesita fondo de verdad: recortar la plancha no falla por
        # falta de píxeles sino porque dejaría los bloques de los extremos
        # flotando sobre un recorte arbitrario. Pero solo cuando la plancha se
        # está AMPLIANDO (cobertura >= 1): si sobra de más, recortarla no
        # pierde nada por ningún lado sin importar cuánto cambie la forma.
        # Ver `background_expand.aspect_delta`.
        return cobertura >= 1.0 and (
            background_expand.aspect_delta(source_canvas, ancho, alto)
            > background_expand.ASPECT_TRIGGER
        )

    por_extender = [
        (ancho, alto)
        for ancho, alto in sorted(lienzos)
        if background_expand.cached(project, ancho, alto) is None
        and _hace_falta_extender(ancho, alto)
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
