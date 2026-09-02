"""Dependencias y utilidades compartidas de la API."""
from __future__ import annotations

from pathlib import Path

from fastapi import Header, HTTPException, status

from ..config import settings
from ..models import Project
from ..services import storage
from ..services.security import FileValidationError, PathTraversalError, resolve_inside


async def bind_session(
    x_session_id: str | None = Header(
        None,
        alias="X-Session-Id",
        max_length=64,
        description="Sesión del navegador. El trabajo se etiqueta con ella.",
    ),
) -> str | None:
    """Deja la sesión de la petición al alcance de `storage.save_project`.

    Va como dependencia del router para no tener que añadir el parámetro a cada
    endpoint que crea proyectos: cualquiera que se guarde durante la petición
    queda etiquetado con la sesión que lo pidió.

    Tiene que ser `async`: FastAPI ejecuta las dependencias sincrónicas en un
    hilo del pool, que recibe una copia del contexto, y el ContextVar fijado
    allí no llega al endpoint. Declarada así corre en la misma tarea.
    """
    storage.set_current_session(x_session_id)
    return x_session_id


def load_project_or_404(project_id: str) -> Project:
    try:
        return storage.load_project(project_id)
    except storage.ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Proyecto no encontrado: {project_id}"
        ) from exc
    except PathTraversalError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def resolve_ingest(relative: str) -> Path:
    """Ruta absoluta dentro de la carpeta de ingesta (bloquea path traversal)."""
    path = resolve_inside(settings.ingest_dir, relative)
    if not path.exists() or not path.is_file():
        raise bad_request(f"No existe el archivo en la carpeta de ingesta: {relative}")
    return path


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def as_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (FileValidationError, PathTraversalError, ValueError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error interno: {exc}"
    )
