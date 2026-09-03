"""Subidas válidas e inválidas y creación de proyectos."""
from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient
from PIL import Image

import pytest
from pydantic import ValidationError

from app.config import settings
from app.models.project import Canvas
from app.services import storage
from app.services.security import FileValidationError, validate_image_bytes
from tests.conftest import make_artwork


def test_health_ok(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["providers"]["inpainting"]["opencv_available"] is True


def test_create_project_png(client: TestClient, artwork_png: bytes):
    response = client.post(
        "/projects",
        data={"name": "Campaña Q3"},
        files={"artwork": ("arte.png", artwork_png, "image/png")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Campaña Q3"
    assert body["canvas"] == {"width": 600, "height": 600}
    assert body["source"]["format"] == "PNG"
    # El nombre interno es seguro y no reutiliza el subido.
    assert body["source"]["path"].startswith("original/")
    assert "arte.png" not in body["source"]["path"]


def test_create_project_jpeg_and_references(client: TestClient):
    artwork = make_artwork(640, 800, fmt="JPEG")
    kv = make_artwork(320, 320, fmt="PNG")
    response = client.post(
        "/projects",
        data={"name": "Con KV"},
        files={
            "artwork": ("arte.jpg", artwork, "image/jpeg"),
            "kv": ("kv.png", kv, "image/png"),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"]["format"] == "JPEG"
    assert body["references"]["kv"]["path"].startswith("references/")


def test_reject_svg(client: TestClient):
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    response = client.post(
        "/projects",
        data={"name": "SVG"},
        files={"artwork": ("logo.svg", svg, "image/svg+xml")},
    )
    assert response.status_code == 400
    assert "SVG" in response.json()["detail"]


def test_reject_fake_extension(client: TestClient):
    """Un archivo de texto con extensión .png debe rechazarse."""
    response = client.post(
        "/projects",
        data={"name": "Falso"},
        files={"artwork": ("falso.png", b"no soy una imagen" * 20, "image/png")},
    )
    assert response.status_code == 400


def test_reject_too_small(client: TestClient):
    tiny = io.BytesIO()
    Image.new("RGB", (32, 32), (0, 0, 0)).save(tiny, format="PNG")
    response = client.post(
        "/projects",
        data={"name": "Pequeña"},
        files={"artwork": ("tiny.png", tiny.getvalue(), "image/png")},
    )
    assert response.status_code == 400
    assert "pequeñas" in response.json()["detail"].lower()


def test_reject_unsupported_extension(client: TestClient, artwork_png: bytes):
    response = client.post(
        "/projects",
        data={"name": "GIF"},
        files={"artwork": ("arte.gif", artwork_png, "image/gif")},
    )
    assert response.status_code == 400


def test_validate_image_bytes_size_limit():
    payload = make_artwork(200, 200)
    try:
        validate_image_bytes(payload, "arte.png", max_bytes=100)
    except FileValidationError as exc:
        assert "límite" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Debió rechazar por tamaño")


def test_project_json_persisted_and_readable(client: TestClient, project: dict):
    project_id = project["project_id"]
    path = storage.project_json_path(project_id)
    assert path.exists()
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    assert raw["project_id"] == project_id
    assert raw["canvas"]["width"] == 600

    reloaded = storage.load_project(project_id)
    assert reloaded.project_id == project_id
    assert reloaded.name == "Proyecto de prueba"

    # La estructura de carpetas del proyecto existe.
    base = storage.project_dir(project_id)
    for folder in ("original", "layers", "masks", "backgrounds", "variants", "exports"):
        assert (base / folder).is_dir()


def test_original_file_never_modified(client: TestClient, project: dict, artwork_png: bytes):
    project_id = project["project_id"]
    original = storage.abs_path(project_id, project["source"]["path"])
    before = original.read_bytes()

    client.post(f"/projects/{project_id}/analyze", json={"run_ocr": False})
    client.post(f"/projects/{project_id}/extract", json={})
    client.post(f"/projects/{project_id}/reconstruct-background", json={})

    assert original.read_bytes() == before == artwork_png


def test_get_and_delete_project(client: TestClient, project: dict):
    project_id = project["project_id"]
    assert client.get(f"/projects/{project_id}").status_code == 200

    listing = client.get("/projects").json()
    assert any(item["project_id"] == project_id for item in listing)

    response = client.delete(f"/projects/{project_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"/projects/{project_id}").status_code == 404


def test_invalid_project_id_is_rejected(client: TestClient):
    assert client.get("/projects/not-a-uuid").status_code == 400


# ------------------------------------------------------- pliegos y tamaño real
# Un pliego de agencia es ancho y bajo: 11700x3100 son 36 Mpx, menos que un
# cuadrado de 8000x8000. Mientras el tope se midió por el lado más largo, el
# formato para el que existe el corte en piezas era justo el que no entraba.


def test_a_sheet_longer_than_it_is_heavy_is_accepted(client: TestClient):
    """Un arte muy ancho entra: lo que se mide es el área, no el lado."""
    artwork = make_artwork(9000, 300)
    response = client.post(
        "/projects",
        data={"name": "Pliego"},
        files={"artwork": ("pliego.png", artwork, "image/png")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["canvas"] == {"width": 9000, "height": 300}


def test_the_canvas_accepts_the_size_that_used_to_be_rejected():
    """11700x3100 es el pliego real que devolvía 'dimensiones demasiado grandes'."""
    canvas = Canvas(width=11700, height=3100)
    assert (canvas.width, canvas.height) == (11700, 3100)


def test_too_many_pixels_is_rejected_with_its_own_message(monkeypatch):
    """El rechazo por área dice cuántos Mpx trae y cuántos caben."""
    monkeypatch.setattr(settings, "max_image_megapixels", 1)
    payload = make_artwork(1600, 1000)  # 1,6 Mpx
    with pytest.raises(FileValidationError) as excinfo:
        validate_image_bytes(payload, "arte.png")
    assert "Mpx" in str(excinfo.value)


def test_the_canvas_and_the_upload_share_the_same_ceiling(monkeypatch):
    """El lienzo no puede aceptar lo que la subida rechaza, ni al revés.

    Eran dos números escritos por separado y esa es la avería que se arregla:
    subir el de las subidas dejaba el arte aceptado y reventando después.
    """
    monkeypatch.setattr(settings, "max_image_megapixels", 1)
    with pytest.raises(ValidationError):
        Canvas(width=1600, height=1000)


def test_the_ingest_folder_ignores_the_upload_limit(client: TestClient, monkeypatch):
    """Un archivo puesto a mano en el servidor no ha subido por ningún sitio.

    Es el camino para los pliegos que no caben por el navegador, así que medirlo
    contra el tope de subida dejaba sin salida justo el caso para el que existe.
    """
    monkeypatch.setattr(settings, "max_upload_mb", 0)  # nada cabría por subida
    path = settings.ingest_dir / "pliego_ingesta.png"
    path.write_bytes(make_artwork(900, 400))
    try:
        # Por el navegador no pasa ni un byte...
        rejected = client.post(
            "/projects",
            data={"name": "Por subida"},
            files={"artwork": ("arte.png", make_artwork(400, 400), "image/png")},
        )
        assert rejected.status_code == 400
        assert "límite" in rejected.json()["detail"]

        # ...y desde la carpeta del servidor entra igual.
        accepted = client.post(
            "/projects/from-ingest", json={"source": "pliego_ingesta.png"}
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["canvas"] == {"width": 900, "height": 400}
    finally:
        path.unlink(missing_ok=True)
