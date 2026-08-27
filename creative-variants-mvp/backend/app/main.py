"""Punto de entrada de FastAPI. Toda la lógica vive en `app/services`."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.health import router as health_router
from .api.ingest import router as ingest_router
from .api.projects import router as projects_router
from .config import settings
from .providers import provider_status
from .services import storage
from .services.security import FileValidationError, PathTraversalError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger(__name__)

DESCRIPTION = """
API para generar variaciones automáticas de artes publicitarios a partir de un
JPG/PNG **aplanado**.

Un arte aplanado no contiene las capas originales, por lo que el flujo es una
**descomposición asistida**:

0. `GET /ingest` + `POST /projects/from-ingest` — para PSD grandes dejados en
   `data/ingest/`: se importan sus **capas reales** (exactas, sin adivinar).
1. `POST /projects` — sube el arte (y opcionalmente KV, logo y tipografía).
2. `POST /projects/{id}/analyze` — detección automática (segmentación + OCR) con
   confianza y advertencias.
3. `POST /projects/{id}/layers`, `PUT /projects/{id}/layers`,
   `POST /projects/{id}/layers/mask` — corrección manual.
4. `POST /projects/{id}/extract` — capas como PNG RGBA con píxeles originales.
5. `POST /projects/{id}/reconstruct-background` — fondo aproximado (inpainting).
6. `POST /projects/{id}/generate` — variantes reorganizando la composición.
7. `GET /projects/{id}/export` — ZIP con las variantes.

Funciona sin GPU y sin APIs externas: los proveedores opcionales (SAM, PaddleOCR,
FLUX, Adobe) degradan a alternativas locales.
"""

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=DESCRIPTION,
    openapi_tags=[
        {"name": "sistema", "description": "Estado del servicio y capacidades."},
        {"name": "proyectos", "description": "Ciclo completo de un proyecto creativo."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP local; restringir en producción
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(ingest_router)
app.include_router(projects_router)


@app.exception_handler(FileValidationError)
async def _file_validation_handler(_: Request, exc: FileValidationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.exception_handler(PathTraversalError)
async def _path_traversal_handler(_: Request, exc: PathTraversalError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.exception_handler(storage.ProjectNotFoundError)
async def _not_found_handler(_: Request, exc: storage.ProjectNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": f"Proyecto no encontrado: {exc}"},
    )


@app.on_event("startup")
def _startup() -> None:
    storage.projects_root()
    status_info = provider_status()
    logger.info("Datos en %s", settings.data_dir)
    logger.info(
        "Proveedores → segmentación=%s | ocr=%s | inpainting=%s",
        status_info["segmentation"]["active"],
        status_info["ocr"]["active"],
        status_info["inpainting"]["active"],
    )
    if not status_info["ocr"]["available"]:
        logger.warning("OCR no disponible: %s", status_info["ocr"]["detail"])


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
    }
