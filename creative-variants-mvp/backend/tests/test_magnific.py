"""Contrato del proveedor Magnific: catálogo, payloads y recomposición.

Ninguna prueba sale a internet ni consume saldo: el transporte HTTP se simula.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from app.providers import magnific
from app.providers.base import ProviderUnavailableError
from app.providers.magnific import (
    CATALOG,
    MODELS,
    MagnificInpaintProvider,
    closest_ratio,
    compose_inpaint,
    model_catalog,
    resolve_model,
)


# ---------------------------------------------------------------------- datos
def _artwork(path, size=(320, 240), color=(200, 40, 40)) -> str:
    image = Image.new("RGB", size, (30, 60, 90))
    image.paste(Image.new("RGB", (80, 80), color), (120, 80))
    image.save(path)
    return str(path)


def _mask(path, size=(320, 240)) -> str:
    mask = Image.new("L", size, 0)
    mask.paste(Image.new("L", (80, 80), 255), (120, 80))
    mask.save(path)
    return str(path)


def _png_bytes(size=(320, 240), color=(10, 200, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


# -------------------------------------------------------------------- catálogo
def test_catalog_ids_are_unique_and_endpoints_are_absolute():
    ids = [model.id for model in CATALOG]
    assert len(ids) == len(set(ids))
    assert all(model.endpoint.startswith("/v1/ai/") for model in CATALOG)
    assert all(model.mode in {
        "mask", "structure", "image", "references", "reference_objects"
    } for model in CATALOG)


def test_catalog_is_exposed_for_the_interface():
    catalogue = model_catalog()
    assert {"id", "label", "description", "provider", "supports_mask"} <= set(catalogue[0])
    assert any(item["supports_mask"] for item in catalogue)
    assert all(item["provider"] == "magnific" for item in catalogue)


def test_unknown_model_is_rejected_with_the_available_options():
    with pytest.raises(ProviderUnavailableError) as error:
        resolve_model("no-existe")
    assert "ideogram-image-edit" in str(error.value)


def test_provider_requires_key():
    assert MagnificInpaintProvider(api_key="").available() is False
    assert MagnificInpaintProvider(api_key="clave-no-se-envia").available() is True


def test_provider_with_unknown_model_is_not_available():
    provider = MagnificInpaintProvider(api_key="clave", model="inventado")
    assert provider.available() is False


# ------------------------------------------------------------------ auxiliares
def test_closest_ratio_matches_the_artwork_shape():
    ratios = ("square_1_1", "widescreen_16_9", "social_story_9_16")
    assert closest_ratio(1080, 1080, ratios) == "square_1_1"
    assert closest_ratio(1920, 1080, ratios) == "widescreen_16_9"
    assert closest_ratio(1080, 1920, ratios) == "social_story_9_16"


def test_mask_is_inverted_because_ideogram_edits_the_black_area(tmp_path):
    path = _mask(tmp_path / "mask.png", size=(4, 4))
    inverted = Image.open(io.BytesIO(magnific._invert_mask_png(path))).convert("L")
    # Fuera de la máscara (blanco para nosotros = conservar) debe quedar blanco.
    assert inverted.getpixel((0, 0)) == 255


def test_compose_keeps_everything_outside_the_mask(tmp_path):
    art = _artwork(tmp_path / "art.png")
    mask = _mask(tmp_path / "mask.png")
    generated = Image.new("RGB", (320, 240), (10, 200, 10))

    composed = compose_inpaint(art, mask, generated, feather=0)

    assert composed.getpixel((5, 5)) == (30, 60, 90)  # fondo original intacto
    assert composed.getpixel((160, 120)) == (10, 200, 10)  # hueco repintado


# -------------------------------------------------------------------- payloads
def test_mask_model_payload_carries_image_and_mask():
    provider = MagnificInpaintProvider(api_key="clave", model="ideogram-image-edit")
    payload = provider._payload(
        provider.model, "IMG", "MASK", "instrucción", (1080, 1080)
    )
    assert payload["image"] == "IMG"
    assert payload["mask"] == "MASK"
    assert payload["rendering_speed"] in {"TURBO", "DEFAULT", "QUALITY"}


def test_mystic_payload_uses_structure_reference_and_closest_ratio():
    provider = MagnificInpaintProvider(api_key="clave", model="mystic")
    payload = provider._payload(provider.model, "IMG", None, "instrucción", (1920, 1080))
    assert payload["structure_reference"] == "IMG"
    assert payload["aspect_ratio"] == "widescreen_16_9"
    assert payload["resolution"] in {"1k", "2k", "4k"}
    assert "mask" not in payload


def test_seedream_payload_uses_reference_images_list():
    provider = MagnificInpaintProvider(api_key="clave", model="seedream-v4-5-edit")
    payload = provider._payload(provider.model, "IMG", None, "instrucción", (900, 1350))
    assert payload["reference_images"] == ["IMG"]


def test_nano_banana_payload_uses_objects_with_mime_type():
    provider = MagnificInpaintProvider(api_key="clave", model="nano-banana-pro")
    payload = provider._payload(provider.model, "https://cdn/x.png", None, "x", (1080, 1080))
    assert payload["reference_images"] == [
        {"image": "https://cdn/x.png", "mime_type": "image/png"}
    ]


def test_flux_2_pro_payload_uses_explicit_pixels():
    provider = MagnificInpaintProvider(api_key="clave", model="flux-2-pro")
    payload = provider._payload(provider.model, "IMG", None, "x", (4000, 2000))
    assert payload["width"] == 2048 and payload["height"] == 1024


# ------------------------------------------------------------ llamada completa
def _mock_client(monkeypatch, recorder: list, model_id: str):
    """Sustituye httpx.Client por uno que responde el ciclo tarea → imagen."""
    endpoint = MODELS[model_id].endpoint

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == endpoint and request.method == "POST":
            recorder.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"task_id": "t1", "status": "CREATED"}})
        if request.url.path == f"{endpoint}/t1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "task_id": "t1",
                        "status": "COMPLETED",
                        "generated": ["https://cdn.test/out.png"],
                    }
                },
            )
        if str(request.url) == "https://cdn.test/out.png":
            return httpx.Response(200, content=_png_bytes())
        return httpx.Response(404, json={"message": f"sin ruta {request.url}"})

    real_client = httpx.Client  # httpx es un módulo compartido: guardar el real.

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(magnific.httpx, "Client", factory)
    monkeypatch.setattr(magnific.time, "sleep", lambda _seconds: None)


def test_fill_with_mask_model_sends_image_and_mask_and_composes(tmp_path, monkeypatch):
    art = _artwork(tmp_path / "art.png")
    mask = _mask(tmp_path / "mask.png")
    sent: list = []
    _mock_client(monkeypatch, sent, "ideogram-image-edit")

    provider = MagnificInpaintProvider(api_key="clave", model="ideogram-image-edit")
    output = provider.fill(art, mask, prompt="fondo azul", output_path=str(tmp_path / "bg.png"))

    assert sent[0]["image"].startswith("data:image/png;base64,")
    assert sent[0]["mask"].startswith("data:image/png;base64,")
    assert "fondo azul" in sent[0]["prompt"]
    with Image.open(output) as result:
        assert result.size == (320, 240)
        assert result.convert("RGB").getpixel((5, 5)) == (30, 60, 90)


def test_fill_without_mask_model_cleans_locally_first(tmp_path, monkeypatch):
    art = _artwork(tmp_path / "art.png")
    mask = _mask(tmp_path / "mask.png")
    sent: list = []
    _mock_client(monkeypatch, sent, "mystic")

    provider = MagnificInpaintProvider(api_key="clave", model="mystic")
    output = provider.fill(art, mask, output_path=str(tmp_path / "bg.png"))

    assert "structure_reference" in sent[0]
    # La referencia limpiada en local es temporal y no debe quedar en disco.
    assert not (tmp_path / "magnific_reference.png").exists()
    with Image.open(output) as result:
        assert result.size == (320, 240)


def test_failed_task_raises_so_the_orchestrator_can_fall_back(tmp_path, monkeypatch):
    art = _artwork(tmp_path / "art.png")
    mask = _mask(tmp_path / "mask.png")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"task_id": "t1", "status": "CREATED"}})
        return httpx.Response(200, json={"data": {"task_id": "t1", "status": "FAILED"}})

    real_client = httpx.Client
    monkeypatch.setattr(
        magnific.httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(magnific.time, "sleep", lambda _seconds: None)

    provider = MagnificInpaintProvider(api_key="clave", model="ideogram-image-edit")
    with pytest.raises(ProviderUnavailableError):
        provider.fill(art, mask, output_path=str(tmp_path / "bg.png"))
