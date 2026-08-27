"""Fixtures de pruebas. Usa imágenes sintéticas y un DATA_DIR temporal.

No requiere GPU, modelos descargados ni claves de API.
"""
from __future__ import annotations

import io
import os
import pathlib
import shutil
import tempfile

# El entorno debe quedar configurado ANTES de importar la app (settings es singleton).
_TMP_DATA = tempfile.mkdtemp(prefix="cvmvp-tests-")
os.environ["DATA_DIR"] = _TMP_DATA
os.environ["ENABLE_OCR"] = "false"
os.environ["SEGMENTATION_PROVIDER"] = "local"
os.environ["INPAINTING_PROVIDER"] = "opencv"
os.environ["MIN_IMAGE_SIDE"] = "64"
os.environ["INGEST_DIR"] = str(pathlib.Path(_TMP_DATA) / "ingest")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app.main import app  # noqa: E402


def make_artwork(width: int = 600, height: int = 600, fmt: str = "PNG") -> bytes:
    """Arte sintético: fondo plano, producto, logo y dos bloques de texto."""
    image = Image.new("RGB", (width, height), (238, 238, 240))
    draw = ImageDraw.Draw(image)

    # Fondo con una banda para que el inpainting tenga algo que reconstruir.
    draw.rectangle([0, int(height * 0.62), width, height], fill=(210, 222, 236))

    # "Producto": bloque rojo centrado.
    draw.rounded_rectangle(
        [int(width * 0.30), int(height * 0.30), int(width * 0.70), int(height * 0.72)],
        radius=int(width * 0.04),
        fill=(198, 40, 40),
    )
    # "Logo": bloque azul arriba a la izquierda.
    draw.rectangle(
        [int(width * 0.05), int(height * 0.04), int(width * 0.22), int(height * 0.12)],
        fill=(21, 101, 192),
    )
    # Textos.
    draw.text((int(width * 0.30), int(height * 0.14)), "CONOCE LO NUEVO", fill=(20, 20, 20))
    draw.text((int(width * 0.08), int(height * 0.95)), "Aplican terminos y condiciones", fill=(60, 60, 60))

    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_data():
    yield
    shutil.rmtree(_TMP_DATA, ignore_errors=True)


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def artwork_png() -> bytes:
    return make_artwork()


@pytest.fixture()
def project(client: TestClient, artwork_png: bytes) -> dict:
    """Proyecto creado por la API (sin analizar)."""
    response = client.post(
        "/projects",
        data={"name": "Proyecto de prueba"},
        files={"artwork": ("arte.png", artwork_png, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_manual_layers(client: TestClient, project_id: str) -> dict[str, dict]:
    """Crea las capas mínimas (producto, logo, titular, CTA, legal) a mano."""
    payloads = [
        {
            "name": "Producto",
            "category": "product",
            "type": "image",
            "x": 180,
            "y": 180,
            "width": 240,
            "height": 250,
            "locked": True,
            "auto_segment": True,
        },
        {
            "name": "Logo",
            "category": "logo",
            "type": "image",
            "x": 30,
            "y": 24,
            "width": 102,
            "height": 48,
            "locked": True,
            "auto_segment": False,
        },
        {
            "name": "Titular",
            "category": "headline",
            "type": "text",
            "x": 180,
            "y": 84,
            "width": 300,
            "height": 60,
            "content": "Conoce lo nuevo",
            "auto_segment": False,
        },
        {
            "name": "CTA",
            "category": "cta",
            "type": "text",
            "x": 200,
            "y": 500,
            "width": 200,
            "height": 44,
            "content": "Comprar ahora",
            "auto_segment": False,
        },
        {
            "name": "Texto legal",
            "category": "legal",
            "type": "text",
            "x": 40,
            "y": 565,
            "width": 500,
            "height": 24,
            "content": "Aplican terminos y condiciones.",
            "auto_segment": False,
        },
    ]
    created: dict[str, dict] = {}
    for payload in payloads:
        response = client.post(f"/projects/{project_id}/layers", json=payload)
        assert response.status_code == 201, response.text
        layer = response.json()
        created[layer["category"]] = layer
    return created
