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


def font_path(client_id: str, font_id: str) -> Path:
    """Devuelve una fuente del catálogo sin convertir un id en una ruta.

    La composición de campañas no debe volver a subir ni copiar las fuentes
    que ya pertenecen al cliente. Este acceso conserva el mismo contrato
    cerrado de :func:`read_font`: ambos identificadores se buscan en el
    catálogo y nunca se interpolan como una ruta aportada por el navegador.
    """

    for path in ROOT.glob("*/catalog.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["id"] != client_id:
            continue
        for font in data["fonts"]:
            if font["id"] == font_id:
                candidate = (path.parent / font["file"]).resolve()
                if candidate.parent != path.parent.resolve() or not candidate.is_file():
                    break
                # Valida también el binario: un catálogo mal empaquetado no
                # puede tumbar la producción al intentar cargar una fuente.
                validate_font_bytes(candidate.read_bytes(), candidate.name)
                return candidate
    raise FileValidationError("No existe esa tipografía en el catálogo del cliente.")
