"""Carpeta de ingesta: artes y KV grandes sin pasar por el navegador.

Los PSD de KV pesan 60–100 MB cada uno. Subirlos por el uploader del navegador es
lento y frágil, así que se pueden dejar en `data/ingest/` (un volumen ya montado) y
crear el proyecto desde ahí. Solo se listan y aceptan rutas dentro de esa carpeta.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from PIL import Image

from ..config import settings
from ..models import IngestFile, IngestListResponse
from ..services.security import ALLOWED_IMAGE_EXTENSIONS, ALLOWED_PSD_EXTENSIONS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sistema"])

SUPPORTED = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_PSD_EXTENSIONS
MAX_DEPTH = 3
MAX_FILES = 200


@router.get(
    "/ingest",
    response_model=IngestListResponse,
    summary="Listar artes disponibles en la carpeta de ingesta",
)
def list_ingest() -> IngestListResponse:
    root = settings.ingest_dir
    root.mkdir(parents=True, exist_ok=True)
    files: list[IngestFile] = []
    warnings: list[str] = []

    for path in sorted(root.rglob("*")):
        if len(files) >= MAX_FILES:
            warnings.append(f"Se listan solo los primeros {MAX_FILES} archivos.")
            break
        if not path.is_file() or path.name.startswith("."):
            continue
        relative = path.relative_to(root)
        if len(relative.parts) > MAX_DEPTH:
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            with Image.open(path) as image:
                fmt = (image.format or path.suffix.lstrip(".")).upper()
                width, height = image.size
        except Exception as exc:  # noqa: BLE001 - archivo corrupto: se informa y sigue
            warnings.append(f"{relative}: no se pudo leer ({exc.__class__.__name__}).")
            continue
        files.append(
            IngestFile(
                path=str(relative),
                name=path.name,
                format=fmt,
                width=width,
                height=height,
                size_mb=round(path.stat().st_size / (1024 * 1024), 1),
            )
        )

    if not files and not warnings:
        warnings.append(
            f"No hay archivos en {root}. Copie ahí los PSD/JPG/PNG (admite subcarpetas)."
        )
    return IngestListResponse(directory=str(root), files=files, warnings=warnings)
