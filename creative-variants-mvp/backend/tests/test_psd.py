"""Importación de PSD y carpeta de ingesta."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.models import LayerCategory
from app.services import psd_import, storage
from tests.conftest import await_task, make_artwork
from tests.psd_fixture import sample_kv

pytest.importorskip("psd_tools", reason="psd-tools no instalado")


def make_psd(path, width: int = 900, height: int = 660):
    """KV multicapa de prueba (fondo + logo + producto + legales)."""
    return sample_kv(path, width, height)


@pytest.fixture()
def psd_bytes(tmp_path) -> bytes:
    return make_psd(tmp_path / "kv.psd").read_bytes()


# ------------------------------------------------------------------ validación
def test_psd_is_detected_and_accepted_as_artwork(client: TestClient, psd_bytes: bytes):
    response = client.post(
        "/projects",
        data={"name": "KV editable"},
        files={"artwork": ("kv.psd", psd_bytes, "image/vnd.adobe.photoshop")},
    )
    assert response.status_code == 201, response.text
    project = response.json()

    # La fuente del proyecto es el PSD aplanado en PNG: el resto del pipeline usa raster.
    assert project["source"]["format"] == "PNG"
    assert project["source"]["path"].endswith("_flat.png")
    assert project["canvas"] == {"width": 900, "height": 660}
    assert project["meta"]["psd_source"].endswith(".psd")
    assert storage.abs_path(project["project_id"], project["meta"]["psd_source"]).exists()


def test_psd_layers_are_imported_with_exact_crops(client: TestClient, psd_bytes: bytes):
    """El PSD trae las capas: nada se adivina, todo se importa con su recorte real."""
    project = client.post(
        "/projects",
        data={"name": "KV editable"},
        files={"artwork": ("kv.psd", psd_bytes, "image/vnd.adobe.photoshop")},
    ).json()
    project_id = project["project_id"]
    layers = [layer for layer in project["layers"] if layer["category"] != "background"]
    assert len(layers) == 3, [layer["name"] for layer in layers]

    by_category = {layer["category"]: layer for layer in layers}
    # El nombre de capa manda cuando dice algo; la geometría cubre "Capa 5".
    assert "logo" in by_category and "legal" in by_category and "product" in by_category
    assert by_category["logo"]["name"] == "LOGO MARATHON"
    assert by_category["logo"]["locked"] is True
    assert by_category["product"]["locked"] is True
    assert by_category["logo"]["meta"]["mandatory_art"] is True
    assert by_category["legal"]["meta"]["mandatory_art"] is True

    logo = by_category["logo"]
    assert (logo["x"], logo["y"]) == (45, 26)
    assert (logo["width"], logo["height"]) == (252, 79)
    assert logo["source"] == "upload" and logo["extracted"] is True

    import numpy as np

    with Image.open(storage.abs_path(project_id, logo["src"])) as png:
        assert png.mode == "RGBA"
        assert png.size == (252, 79)
        alpha = np.asarray(png.split()[-1])
    assert alpha.max() == 255 and alpha.min() == 0  # alfa del PSD preservado

    # El orden de las capas del PSD se conserva.
    assert [layer["z_index"] for layer in layers] == sorted(
        layer["z_index"] for layer in layers
    )
    assert any("importaron 3 capas" in warning for warning in project["warnings"])


def test_psd_background_layer_becomes_the_plate(client: TestClient, psd_bytes: bytes):
    """La capa de relleno del PSD es el fondo: no hace falta inpainting."""
    project = client.post(
        "/projects",
        data={"name": "KV editable"},
        files={"artwork": ("kv.psd", psd_bytes, "image/vnd.adobe.photoshop")},
    ).json()
    background = project["background"]
    assert background["provider"] == "psd"
    path = storage.abs_path(project["project_id"], background["path"])
    assert path.exists()
    with Image.open(path) as plate:
        assert plate.size == (900, 660)
        assert plate.convert("RGB").getpixel((10, 10)) == (14, 30, 60)
    assert any("no hace falta" in warning for warning in background["warnings"])
    # Ninguna capa de fondo queda como capa movible.
    assert [layer for layer in project["layers"] if layer["category"] == "background"][0][
        "movable"
    ] is False


def test_psd_rejected_for_logo_field(client: TestClient, psd_bytes: bytes):
    response = client.post(
        "/projects",
        data={"name": "Logo psd"},
        files={
            "artwork": ("arte.png", make_artwork(), "image/png"),
            "logo": ("logo.psd", psd_bytes, "image/vnd.adobe.photoshop"),
        },
    )
    assert response.status_code == 400
    assert ".psd" in response.json()["detail"].lower()


def test_psd_without_layer_import(client: TestClient, psd_bytes: bytes):
    project = client.post(
        "/projects",
        data={"name": "Solo aplanado", "import_layers": "false"},
        files={"artwork": ("kv.psd", psd_bytes, "image/vnd.adobe.photoshop")},
    ).json()
    assert project["layers"] == []
    assert any("sin importar capas" in warning for warning in project["warnings"])


def test_psd_classification_helpers():
    assert psd_import.classify_layer_name("LOGO MARATHON")[0] == LayerCategory.LOGO
    assert psd_import.classify_layer_name("logo cece")[0] == LayerCategory.DECORATION
    assert psd_import.classify_layer_name("PRECIO 60%")[0] == LayerCategory.PRICE
    assert psd_import.classify_layer_name("legales")[0] == LayerCategory.LEGAL
    assert psd_import.classify_layer_name("Relleno de color 1")[0] == LayerCategory.BACKGROUND
    assert psd_import.classify_layer_name("Capa 5")[0] is None
    assert psd_import._is_generic("Objeto inteligente vectorial copia") is True
    assert psd_import._is_generic("LOGO MARATHON") is False


# --------------------------------------------------------------------- ingesta
def test_ingest_listing_and_import(client: TestClient, tmp_path):
    ingest = settings.ingest_dir
    (ingest / "MS").mkdir(parents=True, exist_ok=True)
    png_path = ingest / "MS" / "arte_desde_ingesta.png"
    png_path.write_bytes(make_artwork(700, 700))
    psd_path = ingest / "kv_ingesta.psd"
    make_psd(psd_path, 640, 640)

    listing = client.get("/ingest").json()
    names = {item["path"] for item in listing["files"]}
    assert "MS/arte_desde_ingesta.png" in names
    assert "kv_ingesta.psd" in names
    entry = next(item for item in listing["files"] if item["path"] == "kv_ingesta.psd")
    assert entry["format"] == "PSD" and entry["width"] == 640

    response = client.post(
        "/projects/from-ingest",
        json={"source": "kv_ingesta.psd", "name": "Desde ingesta", "import_layers": True},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["name"] == "Desde ingesta"
    assert project["canvas"] == {"width": 640, "height": 640}
    assert project["meta"]["ingest_source"] == "kv_ingesta.psd"
    # El PSD grande no se copia dentro del proyecto: solo su versión aplanada.
    assert "psd_source" not in project["meta"]
    assert [layer for layer in project["layers"] if layer["category"] != "background"]

    png_path.unlink(missing_ok=True)
    psd_path.unlink(missing_ok=True)


def test_ingest_blocks_path_traversal(client: TestClient):
    for evil in ("../../etc/passwd", "/etc/passwd", "../project.json"):
        response = client.post("/projects/from-ingest", json={"source": evil})
        assert response.status_code == 400, evil


def test_ingest_import_of_flat_png_keeps_manual_flow(client: TestClient):
    ingest = settings.ingest_dir
    path = ingest / "plano.png"
    path.write_bytes(make_artwork(600, 600))
    try:
        project = client.post(
            "/projects/from-ingest", json={"source": "plano.png"}
        ).json()
        assert project["layers"] == []
        assert any("aproximada" in warning for warning in project["warnings"])
        # El PNG sí se copia al proyecto (es liviano).
        assert project["source"]["path"].startswith("original/")
        assert storage.abs_path(project["project_id"], project["source"]["path"]).exists()
    finally:
        path.unlink(missing_ok=True)


def test_flatten_falls_back_to_pillow(monkeypatch, tmp_path):
    """Sin psd-tools, el PSD se aplana igual con Pillow."""
    source = tmp_path / "kv.psd"
    make_psd(source, 400, 400)
    monkeypatch.setattr(psd_import, "psd_available", lambda: (False, "simulado"))
    target = tmp_path / "flat.png"
    width, height = psd_import.flatten_psd(source, target)
    assert (width, height) == (400, 400)
    with Image.open(target) as image:
        assert image.mode == "RGB"


def test_import_layers_without_psd_tools_warns(monkeypatch, tmp_path):
    from app.models import Canvas, Project, SourceImage

    project = Project(
        canvas=Canvas(width=100, height=100),
        source=SourceImage(
            path="original/a.png", width=100, height=100, format="PNG",
            original_filename="a.png", bytes=1,
        ),
    )
    monkeypatch.setattr(psd_import, "psd_available", lambda: (False, "simulado"))
    layers, warnings = psd_import.import_psd_layers(project, tmp_path / "x.psd")
    assert layers == []
    assert warnings == ["simulado"]


def test_generate_variants_from_psd_project(client: TestClient, psd_bytes: bytes):
    """Flujo completo: PSD → capas importadas → variantes, sin corrección manual."""
    project = client.post(
        "/projects",
        data={"name": "KV editable"},
        files={"artwork": ("kv.psd", psd_bytes, "image/vnd.adobe.photoshop")},
    ).json()
    project_id = project["project_id"]

    client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Titular",
            "category": "headline",
            "type": "text",
            "x": 60,
            "y": 60,
            "width": 500,
            "height": 90,
            "content": "Conoce lo nuevo",
        },
    )
    response = client.post(
        f"/projects/{project_id}/generate",
        json={"count": 4, "seed": 5, "formats": ["1080x1350"], "intensity": "moderate"},
    )
    variants = await_task(client, project_id, response)["variants"]
    assert len(variants) == 4
    for variant in variants:
        assert storage.abs_path(project_id, variant["image"]).exists()


def test_psd_reported_in_health(client: TestClient):
    body = client.get("/health").json()
    assert body["providers"]["psd"]["available"] is True
    capabilities = client.get("/capabilities").json()
    assert capabilities["segmentation"]["psd_import"] is True


def test_psd_flatten_matches_composite(tmp_path):
    """El aplanado debe coincidir con el tamaño declarado por el PSD."""
    source = tmp_path / "kv.psd"
    make_psd(source, 512, 384)
    target = tmp_path / "flat.png"
    assert psd_import.flatten_psd(source, target) == (512, 384)
    buffer = io.BytesIO(target.read_bytes())
    with Image.open(buffer) as image:
        assert image.size == (512, 384)
