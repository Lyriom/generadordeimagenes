"""Catálogo permanente por cliente, incluido en la imagen de la aplicación.

La limpieza de proyectos nunca toca estos originales. Cada KV recibe una copia
con huella propia para conservar su fuente incluso al actualizar el catálogo.
"""
from __future__ import annotations

import json
from pathlib import Path

from .security import FileValidationError, validate_font_bytes

ROOT = Path(__file__).resolve().parents[1] / "assets" / "client_fonts"


def catalog() -> list[dict]:
    clients = []
    for path in sorted(ROOT.glob("*/catalog.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        clients.append({"id": data["id"], "name": data["name"], "fonts": [
            {"id": font["id"], "name": font["name"]} for font in data["fonts"]
        ]})
    return clients


def read_font(client_id: str, font_id: str) -> tuple[bytes, str]:
    # Los identificadores se buscan en el catálogo; nunca son rutas del usuario.
    for path in ROOT.glob("*/catalog.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["id"] != client_id:
            continue
        for font in data["fonts"]:
            if font["id"] == font_id:
                payload = (path.parent / font["file"]).read_bytes()
                return payload, validate_font_bytes(payload, font["file"])
    raise FileValidationError("No existe esa tipografía en el catálogo del cliente.")
