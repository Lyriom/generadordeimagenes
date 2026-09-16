"""Persistencia de marcas y plantillas. Lo único del sistema que no caduca.

`storage.py` guarda proyectos y los barre cada pocas horas, que es lo correcto
para archivos de cientos de megas que solo sirven mientras dura una sesión. Este
módulo es su opuesto deliberado: escribe en `data/brands/`, que **ningún
barrido toca**, porque lo que guarda no es un archivo pesado sino una decisión
—qué cambia en cada arte y dónde va— que costó una revisión humana.

Mismo esqueleto que `storage`: escritura atómica, identificadores validados como
uuid antes de tocar el disco y rutas resueltas dentro de la carpeta.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from ..config import settings
from ..models.template import Brand, Template
from .security import resolve_inside, validate_uuid

#: Subcarpetas de una plantilla. `assets` guarda los PNG congelados de capas y
#: campos; `masters`, la vista previa aprobada de cada formato.
TEMPLATE_SUBDIRS = ("assets", "masters")

#: Subcarpetas de una marca. `references` es donde aterrizan los artes de redes
#: (etapa 4) y `assets` los logos y recursos que no son de un KV concreto.
BRAND_SUBDIRS = ("assets", "references", "templates")


class BrandNotFoundError(KeyError):
    """No existe esa marca."""


class TemplateNotFoundError(KeyError):
    """No existe esa plantilla en esa marca."""


def brands_root() -> Path:
    root = settings.brands_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def brand_dir(brand_id: str) -> Path:
    return brands_root() / validate_uuid(brand_id, "brand_id")


def template_dir(brand_id: str, template_id: str) -> Path:
    return brand_dir(brand_id) / "templates" / validate_uuid(template_id, "template_id")


def ensure_brand_dirs(brand_id: str) -> Path:
    base = brand_dir(brand_id)
    for name in BRAND_SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def ensure_template_dirs(brand_id: str, template_id: str) -> Path:
    base = template_dir(brand_id, template_id)
    for name in TEMPLATE_SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def _write_json(target: Path, payload: dict) -> None:
    """Escritura atómica: o está el archivo entero o está el anterior.

    Una plantilla a medio escribir es peor que ninguna: se descubre semanas
    después, cuando alguien abre la biblioteca para producir.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


# ----------------------------------------------------------------------- marca
def save_brand(brand: Brand) -> Path:
    base = ensure_brand_dirs(brand.brand_id)
    brand.touch()
    target = base / "brand.json"
    _write_json(target, brand.model_dump(mode="json"))
    return target


def load_brand(brand_id: str) -> Brand:
    path = brand_dir(brand_id) / "brand.json"
    if not path.exists():
        raise BrandNotFoundError(brand_id)
    with path.open("r", encoding="utf-8") as handle:
        return Brand.model_validate(json.load(handle))


def list_brands() -> list[Brand]:
    brands: list[Brand] = []
    for entry in sorted(brands_root().iterdir()):
        manifest = entry / "brand.json"
        if not entry.is_dir() or not manifest.exists():
            continue
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                brands.append(Brand.model_validate(json.load(handle)))
        except Exception:  # noqa: BLE001 - una marca corrupta no tumba el listado
            continue
    return sorted(brands, key=lambda item: item.name.lower())


def delete_brand(brand_id: str) -> bool:
    """Borra la marca **y toda su biblioteca**. No hay papelera."""
    base = brand_dir(brand_id)
    if not base.exists():
        return False
    shutil.rmtree(base, ignore_errors=True)
    return not base.exists()


# ------------------------------------------------------------------- plantilla
def save_template(template: Template) -> Path:
    base = ensure_template_dirs(template.brand_id, template.template_id)
    template.touch()
    target = base / "template.json"
    _write_json(target, template.model_dump(mode="json"))
    return target


def load_template(brand_id: str, template_id: str) -> Template:
    path = template_dir(brand_id, template_id) / "template.json"
    if not path.exists():
        raise TemplateNotFoundError(template_id)
    with path.open("r", encoding="utf-8") as handle:
        return Template.model_validate(json.load(handle))


def list_templates(brand_id: str) -> list[Template]:
    root = brand_dir(brand_id) / "templates"
    if not root.exists():
        return []
    templates: list[Template] = []
    for entry in sorted(root.iterdir()):
        manifest = entry / "template.json"
        if not entry.is_dir() or not manifest.exists():
            continue
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                templates.append(Template.model_validate(json.load(handle)))
        except Exception:  # noqa: BLE001
            continue
    return sorted(templates, key=lambda item: item.created_at, reverse=True)


def delete_template(brand_id: str, template_id: str) -> bool:
    base = template_dir(brand_id, template_id)
    if not base.exists():
        return False
    shutil.rmtree(base, ignore_errors=True)
    return not base.exists()


# --------------------------------------------------------------------- archivos
def template_path(brand_id: str, template_id: str, relative: str) -> Path:
    """Ruta absoluta segura de un recurso dentro de la plantilla."""
    return resolve_inside(template_dir(brand_id, template_id), relative)


def freeze_file(brand_id: str, template_id: str, origin: Path, relative: str) -> str | None:
    """Copia un archivo del proyecto dentro de la plantilla y devuelve su ruta.

    **Copia, no enlaza.** Un enlace al proyecto deja una biblioteca que se rompe
    sola en cuanto el barrido de retención hace su trabajo, que es justo el
    defecto que este subsistema existe para arreglar.
    """
    if origin is None or not Path(origin).exists():
        return None
    target = template_path(brand_id, template_id, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, target)
    return relative


def disk_usage_mb() -> float:
    total = 0
    for path in brands_root().rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return round(total / (1024 * 1024), 1)
