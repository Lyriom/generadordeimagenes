"""Persistencia en disco: carpetas por proyecto y project.json."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

from ..config import settings
from ..models import Project
from .security import resolve_inside, validate_project_id

SUBDIRS = (
    "original",
    "references",
    "layers",
    "masks",
    "backgrounds",
    "variants",
    "exports",
    "tmp",
)


class ProjectNotFoundError(KeyError):
    """No existe el proyecto solicitado."""


# Sesión del navegador que está haciendo la petición en curso. La rellena la
# dependencia `bind_session` de la API; en el worker de Celery queda vacía, y
# ahí no hace falta porque el proyecto ya viene etiquetado de su creación.
_current_session: ContextVar[str | None] = ContextVar("current_session", default=None)


def set_current_session(session_id: str | None) -> None:
    _current_session.set((session_id or "").strip() or None)


def current_session() -> str | None:
    return _current_session.get()


def projects_root() -> Path:
    root = settings.projects_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_dir(project_id: str) -> Path:
    safe_id = validate_project_id(project_id)
    return projects_root() / safe_id


def ensure_project_dirs(project_id: str) -> Path:
    base = project_dir(project_id)
    for name in SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def project_json_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def save_project(project: Project) -> Path:
    """Escritura atómica de project.json."""
    base = ensure_project_dirs(project.project_id)
    # Se etiqueta una sola vez, al crearlo: así el proyecto sigue siendo de la
    # sesión que lo subió aunque más tarde lo toque el worker o otra pestaña.
    if not project.meta.get("session_id"):
        session = current_session()
        if session:
            project.meta["session_id"] = session
    project.touch()
    target = base / "project.json"
    payload = project.model_dump(mode="json")
    fd, tmp_name = tempfile.mkstemp(dir=str(base), prefix=".project-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def load_project(project_id: str) -> Project:
    path = project_json_path(project_id)
    if not path.exists():
        raise ProjectNotFoundError(project_id)
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return Project.model_validate(data)


def list_projects() -> list[Project]:
    projects: list[Project] = []
    for entry in sorted(projects_root().iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "project.json"
        if not manifest.exists():
            continue
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                projects.append(Project.model_validate(json.load(handle)))
        except Exception:  # noqa: BLE001 - proyecto corrupto: se ignora en el listado
            continue
    return sorted(projects, key=lambda item: item.created_at, reverse=True)


def delete_project(project_id: str) -> bool:
    base = project_dir(project_id)
    if not base.exists():
        return False
    shutil.rmtree(base, ignore_errors=True)
    return not base.exists()


def _last_touched(entry: Path) -> float:
    """Momento de la última escritura del proyecto.

    Se usa `project.json`, que se reescribe en cada cambio, y no la carpeta:
    en algunos sistemas de archivos el mtime del directorio no se actualiza al
    escribir dentro, y un proyecto en uso parecería abandonado.
    """
    manifest = entry / "project.json"
    try:
        return manifest.stat().st_mtime if manifest.exists() else entry.stat().st_mtime
    except OSError:
        return 0.0


def purge_expired_projects(
    retention_hours: int | None = None,
    max_kept: int | None = None,
) -> list[str]:
    """Borra el trabajo que ya no pertenece a ninguna sesión viva.

    Dos criterios, ambos por antigüedad y nunca por sesión: un usuario no puede
    borrar el trabajo en curso de otro por el simple hecho de abrir la página.

      1. Todo proyecto sin tocar desde hace más de `retention_hours`.
      2. Si aun así quedan más de `max_kept`, los más antiguos hasta el tope.

    Devuelve los identificadores borrados.
    """
    hours = settings.project_retention_hours if retention_hours is None else retention_hours
    keep = settings.max_projects_kept if max_kept is None else max_kept

    try:
        entries = [entry for entry in projects_root().iterdir() if entry.is_dir()]
    except OSError:
        return []

    # Del más reciente al más antiguo, para que los recortes caigan por el final.
    entries.sort(key=_last_touched, reverse=True)
    cutoff = time.time() - max(0, hours) * 3600
    doomed: list[Path] = []
    survivors: list[Path] = []

    for entry in entries:
        if hours > 0 and _last_touched(entry) < cutoff:
            doomed.append(entry)
        else:
            survivors.append(entry)

    if keep > 0 and len(survivors) > keep:
        doomed.extend(survivors[keep:])

    removed: list[str] = []
    for entry in doomed:
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry.name)
    return removed


def delete_all_projects() -> list[str]:
    """Vacía la carpeta de proyectos. Es lo que pide el botón de borrar todo."""
    removed: list[str] = []
    try:
        entries = [entry for entry in projects_root().iterdir() if entry.is_dir()]
    except OSError:
        return []
    for entry in entries:
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry.name)
    return removed


def disk_usage_mb() -> float:
    """Cuánto ocupan los proyectos, para poder avisar antes de que sea tarde."""
    total = 0
    for path in projects_root().rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return round(total / (1024 * 1024), 1)


def abs_path(project_id: str, relative: str) -> Path:
    """Ruta absoluta segura para un recurso relativo del proyecto."""
    return resolve_inside(project_dir(project_id), relative)


def write_bytes(project_id: str, relative: str, payload: bytes) -> Path:
    target = abs_path(project_id, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def clear_dir(project_id: str, relative: str) -> None:
    target = abs_path(project_id, relative)
    if target.exists() and target.is_dir():
        for child in target.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child, ignore_errors=True)


def purge_tmp(project_id: str) -> None:
    """Elimina de forma segura archivos temporales del proyecto."""
    clear_dir(project_id, "tmp")
