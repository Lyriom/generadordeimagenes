"""PNG con transparencia: el alfa se usa como máscara y nunca se aplana en negro."""
from __future__ import annotations

import io

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.services import storage
from app.services.imaging import load_alpha, load_flat_rgb, read_bgr_flat


def make_cutout(width: int = 600, height: int = 600) -> bytes:
    """Recorte de producto: círculo rojo opaco sobre fondo 100% transparente."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        [int(width * 0.25), int(height * 0.25), int(width * 0.75), int(height * 0.75)],
        fill=(200, 40, 40, 255),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_flatten_uses_white_not_black(tmp_path):
    path = tmp_path / "cutout.png"
    path.write_bytes(make_cutout())

    flat = load_flat_rgb(path)
    assert flat.mode == "RGB"
    assert flat.getpixel((5, 5)) == (255, 255, 255), "el alfa debe aplanarse en blanco"

    bgr, alpha = read_bgr_flat(path)
    assert alpha is not None
    assert tuple(int(v) for v in bgr[5, 5]) == (255, 255, 255)
    assert alpha[5, 5] == 0
    assert alpha[300, 300] == 255


def test_load_alpha_returns_none_for_opaque_images(tmp_path):
    path = tmp_path / "opaco.png"
    Image.new("RGB", (300, 300), (10, 20, 30)).save(path)
    assert load_alpha(path) is None


def test_analyze_uses_alpha_channel_as_product_mask(client: TestClient):
    response = client.post(
        "/projects",
        data={"name": "Recorte"},
        files={"artwork": ("zapato.png", make_cutout(), "image/png")},
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["project_id"]

    analysis = client.post(f"/projects/{project_id}/analyze", json={"run_ocr": False}).json()
    layers = analysis["layers"]
    product = next(layer for layer in layers if layer["category"] == "product")

    # El alfa da una máscara exacta: confianza alta y bbox ceñido al círculo.
    assert product["confidence"] >= 0.9
    assert product["meta"].get("from_alpha") is True
    assert 140 <= product["x"] <= 155 and 140 <= product["y"] <= 155
    assert 290 <= product["width"] <= 310
    # Se extrae en el mismo paso: la capa queda lista para componer.
    assert product["extracted"] is True and product["src"]

    # El aviso debe decir qué es y a dónde ir, no solo que hay transparencia.
    assert any("producto recortado" in warning for warning in analysis["warnings"])
    assert any("Cambiar el producto del KV" in warning for warning in analysis["warnings"])
    assert any("capas de texto" in warning for warning in analysis["warnings"])

    with Image.open(storage.abs_path(project_id, product["src"])) as png:
        alpha = np.asarray(png.convert("RGBA").split()[-1])
    assert alpha.max() == 255 and alpha.min() == 0  # transparencia preservada


def test_variants_from_transparent_source_are_not_black(client: TestClient):
    """El bug original: un PNG con alfa producía variantes con fondo negro."""
    project_id = client.post(
        "/projects",
        data={"name": "Recorte"},
        files={"artwork": ("zapato.png", make_cutout(), "image/png")},
    ).json()["project_id"]
    client.post(f"/projects/{project_id}/analyze", json={"run_ocr": False})

    response = client.post(
        f"/projects/{project_id}/generate",
        json={"count": 4, "seed": 7, "formats": ["1080x1080"], "intensity": "moderate"},
    )
    assert response.status_code == 200, response.text
    variants = response.json()["variants"]

    for variant in variants:
        with Image.open(storage.abs_path(project_id, variant["image"])) as image:
            arr = np.asarray(image.convert("RGB"), dtype=np.float32)
        corners = np.concatenate([arr[:20, :20].reshape(-1, 3), arr[-20:, -20:].reshape(-1, 3)])
        assert corners.mean() > 60, "el fondo no debe quedar negro por el alfa"


def test_empty_composition_is_not_scored_100(client: TestClient):
    """Una sola capa y sin texto debe puntuar bajo y explicar por qué."""
    project_id = client.post(
        "/projects",
        data={"name": "Recorte"},
        files={"artwork": ("zapato.png", make_cutout(), "image/png")},
    ).json()["project_id"]
    client.post(f"/projects/{project_id}/analyze", json={"run_ocr": False})

    variants = client.post(
        f"/projects/{project_id}/generate",
        json={"count": 4, "seed": 3, "formats": ["1080x1080"], "intensity": "conservative"},
    ).json()["variants"]

    for variant in variants:
        quality = variant["quality"]
        assert quality["score"] < 90, quality
        assert quality["metrics"]["text_elements"] == 0.0
        assert any("no tiene texto" in warning for warning in quality["warnings"])
        assert any("elemento" in warning for warning in quality["warnings"])
