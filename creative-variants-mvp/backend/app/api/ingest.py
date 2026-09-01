"""Carpeta de ingesta: artes y KV grandes sin pasar por el navegador.

Los PSD de KV pesan 60–100 MB cada uno. Subirlos por el uploader del navegador es
lento y frágil, así que se pueden dejar en `data/ingest/` (un volumen ya montado) y
crear el proyecto desde ahí. Solo se listan y aceptan rutas dentro de esa carpeta.
"""
from __future__ import annotations

import io
import logging

from fastapi import APIRouter, Query, Response
from PIL import Image

from ..config import settings
from ..models import (
    IngestFile,
    IngestListResponse,
    PsdPieceInfo,
    PsdPiecesResponse,
)
from ..services import psd_import
from ..services.security import ALLOWED_IMAGE_EXTENSIONS, ALLOWED_PSD_EXTENSIONS
from .deps import as_http_error, bad_request, resolve_ingest

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
def list_ingest(
    with_pieces: bool = Query(
        False,
        description=(
            "Cuenta las piezas (artboards) de cada PSD. Abre el árbol de capas de "
            "cada archivo: en carpetas con muchos PSD grandes el listado tarda más."
        ),
    ),
) -> IngestListResponse:
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
        pieces = 1
        if with_pieces and fmt == "PSD":
            try:
                # analyse_pixels=False: solo artboards, sin aplanar el PSD.
                detected, _ = psd_import.detect_pieces(path, analyse_pixels=False)
                pieces = max(1, len(detected))
            except Exception as exc:  # noqa: BLE001 - contar piezas no es crítico
                warnings.append(f"{relative}: no se pudieron contar las piezas ({exc}).")
        files.append(
            IngestFile(
                path=str(relative),
                name=path.name,
                format=fmt,
                width=width,
                height=height,
                size_mb=round(path.stat().st_size / (1024 * 1024), 1),
                pieces=pieces,
            )
        )

    if not files and not warnings:
        warnings.append(
            f"No hay archivos en {root}. Copie ahí los PSD/JPG/PNG (admite subcarpetas)."
        )
    return IngestListResponse(directory=str(root), files=files, warnings=warnings)


@router.get(
    "/ingest/pieces",
    response_model=PsdPiecesResponse,
    summary="Piezas que contiene un PSD del pliego (artboards o detección geométrica)",
)
def ingest_pieces(
    source: str = Query(description="Ruta relativa devuelta por GET /ingest."),
    analyse_pixels: bool = Query(
        True,
        description=(
            "Si el PSD no usa artboards, buscar las piezas por geometría. Requiere "
            "aplanar el archivo, así que es más lento."
        ),
    ),
) -> PsdPiecesResponse:
    path = resolve_ingest(source)
    if path.suffix.lower() not in ALLOWED_PSD_EXTENSIONS:
        raise bad_request("Solo los PSD pueden contener varias piezas.")
    try:
        pieces, warnings = psd_import.detect_pieces(path, analyse_pixels=analyse_pixels)
        with Image.open(path) as image:
            width, height = image.size
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return PsdPiecesResponse(
        source=source,
        width=width,
        height=height,
        pieces=[
            PsdPieceInfo(
                index=piece.index,
                name=piece.name,
                x=piece.x,
                y=piece.y,
                width=piece.width,
                height=piece.height,
                origin=piece.origin,
            )
            for piece in pieces
        ],
        warnings=warnings,
    )


@router.get(
    "/ingest/pieces/preview",
    summary="Miniatura PNG de una pieza del PSD, para elegirla antes de importar",
)
def ingest_piece_preview(
    source: str = Query(description="Ruta relativa devuelta por GET /ingest."),
    index: int = Query(0, ge=0, description="Índice de la pieza en GET /ingest/pieces."),
    max_side: int = Query(420, ge=80, le=1200),
) -> Response:
    path = resolve_ingest(source)
    if path.suffix.lower() not in ALLOWED_PSD_EXTENSIONS:
        raise bad_request("Solo los PSD pueden contener varias piezas.")
    try:
        pieces, _ = psd_import.detect_pieces(path)
        match = next((piece for piece in pieces if piece.index == index), None)
        if match is None:
            raise bad_request(f"El PSD no tiene una pieza con índice {index}.")
        image = psd_import.piece_preview(path, match, max_side=max_side)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=600"},
    )
