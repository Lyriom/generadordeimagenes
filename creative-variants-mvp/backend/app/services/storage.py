"""Persistencia en disco: carpetas por proyecto y project.json."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
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
