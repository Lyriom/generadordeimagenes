"""Rescate del producto aplanado dentro de la foto del KV.

El caso real: el PSD trae la sala (o la mesa) dentro de una sola fotografía que
cubre el lienzo, así que el importador la toma como fondo y la pieza se queda sin
capa Producto. Aquí se comprueba que el recorte la recupera.

Ninguna prueba sale a internet ni consume saldo: se simula el transporte HTTP.
"""
from __future__ import annotations

import io

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.models import LayerCategory
from app.providers import magnific
from app.providers.base import ProviderUnavailableError
from app.services import product_cutout, storage

CANVAS = (400, 400)
#: El "mueble": un bloque centrado que el recortador debe aislar.
SUBJECT_BOX = (120, 140, 280, 300)


def _room_photo() -> bytes:
    """Foto de ambiente: pared, piso y un mueble encima. Todo en una capa."""
    image = Image.new("RGB", CANVAS, (208, 196, 180))
    image.paste(Image.new("RGB", (400, 150), (150, 130, 110)), (0, 250))
    image.paste(Image.new("RGB", (160, 160), (90, 60, 40)), (120, 140))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _cutout_png(box=SUBJECT_BOX, size=CANVAS) -> bytes:
    """Lo que devolvería Magnific: el sujeto opaco y el resto transparente."""
    cut = Image.new("RGBA", size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    cut.paste(Image.new("RGBA", (x1 - x0, y1 - y0), (90, 60, 40, 255)), (x0, y0))
    buffer = io.BytesIO()
    cut.save(buffer, format="PNG")
    return buffer.getvalue()


def _mock_magnific(monkeypatch, cutout_bytes, calls: list | None = None):
    """Simula subida → remove-background → descarga, y las ediciones de escena.

    `cutout_bytes` puede ser una lista: cada llamada a remove-background consume
    el siguiente recorte, y el último se repite. Así se cubre la foto de ambiente,
    donde primero vuelve la escena entera y después el producto aislado.
    """
    real_client = httpx.Client
    recortes = list(cutout_bytes) if isinstance(cutout_bytes, list) else [cutout_bytes]
    pedidos = {"cut": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/ai/uploads/request-url":
            return httpx.Response(200, json={"files": [{
                "file_id": "upl_1",
                "upload_url": "https://storage.test/put",
                "headers": {"Content-Type": "image/png"},
                "asset_url": "https://cdn.test/asset.png",
            }]})
        if str(request.url) == "https://storage.test/put":
            return httpx.Response(200)
        if path == "/v1/ai/beta/remove-background":
            if calls is not None:
                calls.append(request.content.decode())
            indice = min(pedidos["cut"], len(recortes) - 1)
            pedidos["cut"] += 1
            return httpx.Response(200, json={
                "original": "https://cdn.test/asset.png",
                "high_resolution": f"https://cdn.test/cut{indice}.png",
                "preview": "https://cdn.test/prev.png",
                "url": f"https://cdn.test/cut{indice}.png",
            })
        if request.url.host == "cdn.test" and path.startswith("/cut"):
            return httpx.Response(200, content=recortes[int(path[4:-4])])
        # Edición por instrucción: aislar el producto o vaciar el decorado.
        if path.startswith("/v1/ai/text-to-image/") or path.startswith("/v1/ai/gemini"):
            if calls is not None:
                calls.append(request.content.decode())
            return httpx.Response(200, json={"data": {
                "task_id": "tsk_1",
                "status": "COMPLETED",
                "generated": ["https://cdn.test/editada.png"],
            }})
        if str(request.url) == "https://cdn.test/editada.png":
            return httpx.Response(200, content=_room_photo())
        return httpx.Response(404, json={"message": f"sin ruta {request.url}"})

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(magnific.httpx, "Client", factory)


@pytest.fixture()
def flat_project(client: TestClient) -> dict:
    """Proyecto cuyo arte es una foto de ambiente: sin capas, como el KV real."""
    response = client.post(
        "/projects",
        data={"name": "Sala en una sola foto"},
        files={"artwork": ("sala.png", _room_photo(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ proveedor
def test_cutout_provider_requires_key():
    assert magnific.MagnificCutoutProvider(api_key="").available() is False
    assert magnific.MagnificCutoutProvider(api_key="clave").available() is True


def test_cutout_sends_an_uploaded_url_because_the_endpoint_rejects_base64(
    tmp_path, monkeypatch
):
    photo = tmp_path / "sala.png"
    photo.write_bytes(_room_photo())
    sent: list = []
    _mock_magnific(monkeypatch, _cutout_png(), sent)

    out = magnific.MagnificCutoutProvider(api_key="clave").cutout(
        str(photo), output_path=str(tmp_path / "recorte.png")
    )

    assert "image_url=" in sent[0]
    assert "cdn.test" in sent[0]
    with Image.open(out) as result:
        assert result.mode == "RGBA"
        assert result.size == CANVAS


def test_cutout_is_scaled_back_to_the_original_size(tmp_path, monkeypatch):
    """El servicio puede devolver otro tamaño; la máscara tiene que encajar."""
    photo = tmp_path / "sala.png"
    photo.write_bytes(_room_photo())
    _mock_magnific(monkeypatch, _cutout_png(box=(60, 70, 140, 150), size=(200, 200)))

    out = magnific.MagnificCutoutProvider(api_key="clave").cutout(
        str(photo), output_path=str(tmp_path / "recorte.png")
    )

    with Image.open(out) as result:
        assert result.size == CANVAS


def test_cutout_without_key_is_rejected(tmp_path):
    photo = tmp_path / "sala.png"
    photo.write_bytes(_room_photo())
    with pytest.raises(ProviderUnavailableError):
        magnific.MagnificCutoutProvider(api_key="").cutout(str(photo))


# -------------------------------------------------------------------- servicio
def test_detect_product_creates_a_replaceable_layer(
    client: TestClient, flat_project: dict, monkeypatch
):
    project_id = flat_project["project_id"]
    assert flat_project["layers"] == []
    _mock_magnific(monkeypatch, _cutout_png())

    response = client.post(f"/projects/{project_id}/layers/detect-product", json={})
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["detected"] is True
    layer = payload["layer"]
    assert layer["category"] == LayerCategory.PRODUCT.value
    assert layer["replaceable"] is True
    # La caja tiene que ceñirse al sujeto, no al lienzo.
    x0, y0, x1, y1 = SUBJECT_BOX
    assert (layer["x"], layer["y"]) == (x0, y0)
    assert (layer["width"], layer["height"]) == (x1 - x0, y1 - y0)
    assert storage.abs_path(project_id, layer["src"]).exists()
    assert storage.abs_path(project_id, layer["mask"]).exists()


def test_the_product_becomes_available_for_replacement(
    client: TestClient, flat_project: dict, monkeypatch
):
    """Es el objetivo: que la pieza deje de estar 'sin capa Producto'."""
    project_id = flat_project["project_id"]
    before = client.get(f"/projects/{project_id}/layers/replaceable").json()["layers"]
    assert not [item for item in before if item["category"] == "product"]

    _mock_magnific(monkeypatch, _cutout_png())
    client.post(f"/projects/{project_id}/layers/detect-product", json={})

    after = client.get(f"/projects/{project_id}/layers/replaceable").json()["layers"]
    assert [item for item in after if item["category"] == "product"]


def test_the_hole_left_by_the_product_is_filled(
    client: TestClient, flat_project: dict, monkeypatch
):
    """Tras recortar, detrás no puede quedar el mueble original."""
    project_id = flat_project["project_id"]
    _mock_magnific(monkeypatch, _cutout_png())

    response = client.post(
        f"/projects/{project_id}/layers/detect-product",
        json={"provider": "opencv"},
    )
    assert response.status_code == 200, response.text

    project = client.get(f"/projects/{project_id}").json()
    plate = storage.abs_path(project_id, project["background"]["path"])
    with Image.open(plate) as image:
        arr = np.asarray(image.convert("RGB"), dtype=np.int16)
    x0, y0, x1, y1 = SUBJECT_BOX
    centre = arr[y0 + 40 : y1 - 40, x0 + 40 : x1 - 40].reshape(-1, 3)
    # El mueble era (90, 60, 40): el relleno tiene que haberlo tapado.
    assert np.abs(centre - np.array([90, 60, 40])).sum(axis=1).mean() > 30


def test_a_photo_without_a_separable_subject_is_reported_not_crashed(
    client: TestClient, flat_project: dict, monkeypatch
):
    """Si ni siquiera separando la escena sale un producto, no se crea la capa."""
    project_id = flat_project["project_id"]
    _mock_magnific(monkeypatch, [_cutout_png(box=(0, 0, 400, 400))] * 2)

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()

    assert payload["detected"] is False
    assert payload["layer"] is None
    assert any("escena completa" in w for w in payload["warnings"])


def test_an_ambience_photo_is_separated_instead_of_rejected(
    client: TestClient, flat_project: dict, monkeypatch
):
    """El caso real de la portada: el recorte devolvió el 75 % del arte.

    Era la sala entera —piso y alfombra incluidos—, porque en una habitación
    `remove-background` no tiene fondo que quitar. En vez de rendirse, un modelo
    de edición deja el mueble sobre fondo plano y ahí el recorte sí funciona.
    """
    project_id = flat_project["project_id"]
    # Primero la escena entera (75 %), después el mueble aislado.
    _mock_magnific(monkeypatch, [
        _cutout_png(box=(0, 100, 400, 400)),
        _cutout_png(),
    ])

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()

    assert payload["detected"] is True, payload["warnings"]
    layer = payload["layer"]
    assert layer["category"] == LayerCategory.PRODUCT.value
    x0, y0, x1, y1 = SUBJECT_BOX
    assert (layer["x"], layer["y"], layer["width"], layer["height"]) == (
        x0, y0, x1 - x0, y1 - y0
    )
    assert layer["meta"]["detected_by"] == "magnific-scene"
    assert any("ambiente" in w for w in payload["warnings"])


def test_the_emptied_scene_becomes_the_plate_without_inpainting(
    client: TestClient, flat_project: dict, monkeypatch
):
    """El decorado vacío ya viene generado: no hay que rellenar ningún hueco."""
    project_id = flat_project["project_id"]
    _mock_magnific(monkeypatch, [_cutout_png(box=(0, 100, 400, 400)), _cutout_png()])

    def no_llamar(*args, **kwargs):  # pragma: no cover - debe no ejecutarse
        raise AssertionError("no hay que rellenar: el decorado ya se regeneró")

    monkeypatch.setattr(product_cutout, "_clean_plate", no_llamar)

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()

    assert payload["detected"] is True
    project = client.get(f"/projects/{project_id}").json()
    assert project["background"]["path"] == "backgrounds/background.png"
    assert "magnific-scene" in project["background"]["provider"]


def test_the_kv_graphics_survive_the_regenerated_plate(
    client: TestClient, flat_project: dict, monkeypatch
):
    """El modelo rehace la foto entera; el panel del titular no puede perderse.

    Caso real: la plancha traía un recuadro blanco donde va "Días WOW · 70 %".
    Al vaciar la sala el modelo lo borró. Fuera de la foto mandan los píxeles
    originales.
    """
    project_id = flat_project["project_id"]
    # La foto ocupa de y=100 abajo: de ahí para arriba es gráfico del KV.
    _mock_magnific(monkeypatch, [_cutout_png(box=(0, 100, 400, 400)), _cutout_png()])

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()
    assert payload["detected"] is True, payload["warnings"]

    plate = storage.abs_path(project_id, "backgrounds/background.png")
    with Image.open(plate) as image:
        rehecha = np.asarray(image.convert("RGB"), dtype=np.int16)
    original = np.asarray(
        Image.open(io.BytesIO(_room_photo())).convert("RGB"), dtype=np.int16
    )
    # Franja de gráfico (y < 90, lejos del degradado del borde): intacta.
    assert np.abs(rehecha[:90] - original[:90]).max() <= 2


def test_without_a_scene_model_the_ambience_photo_explains_itself(
    client: TestClient, flat_project: dict, monkeypatch
):
    """Sin modelo de edición no se puede separar: hay que decirlo, no reventar."""
    project_id = flat_project["project_id"]
    monkeypatch.setattr(magnific.settings, "magnific_scene_model", "no-existe")
    _mock_magnific(monkeypatch, _cutout_png(box=(0, 100, 400, 400)))

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()

    assert payload["detected"] is False
    assert any("MAGNIFIC_SCENE_MODEL" in w for w in payload["warnings"])


def test_a_mask_only_model_is_rejected_for_scenes(tmp_path):
    """Ideogram necesita máscara y aquí todavía no hay: hay que avisarlo claro."""
    photo = tmp_path / "sala.png"
    photo.write_bytes(_room_photo())
    scene = magnific.MagnificSceneProvider(api_key="clave", model="ideogram-image-edit")

    with pytest.raises(ProviderUnavailableError) as error:
        scene.isolate(str(photo))
    assert "máscara" in str(error.value)


def test_a_large_but_plausible_product_is_accepted_with_a_warning(
    client: TestClient, flat_project: dict, monkeypatch
):
    """Un producto grande (40 %) sí pasa, pero avisando del riesgo del relleno."""
    project_id = flat_project["project_id"]
    _mock_magnific(monkeypatch, _cutout_png(box=(40, 40, 360, 240)))

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={"provider": "opencv"}
    ).json()

    assert payload["detected"] is True
    assert any("poco fiables" in w for w in payload["warnings"])


def test_an_empty_cutout_is_reported_not_crashed(
    client: TestClient, flat_project: dict, monkeypatch
):
    project_id = flat_project["project_id"]
    _mock_magnific(monkeypatch, _cutout_png(box=(0, 0, 4, 4)))

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()

    assert payload["detected"] is False
    assert any("no encontró un producto" in w for w in payload["warnings"])


def test_a_piece_that_already_has_a_product_is_left_alone(
    client: TestClient, project: dict, monkeypatch
):
    from tests.conftest import create_manual_layers

    project_id = project["project_id"]
    create_manual_layers(client, project_id)
    calls: list = []
    _mock_magnific(monkeypatch, _cutout_png(), calls)

    payload = client.post(
        f"/projects/{project_id}/layers/detect-product", json={}
    ).json()

    assert payload["detected"] is False
    assert calls == [], "no debe gastar créditos si ya hay producto"


def test_without_key_the_endpoint_explains_instead_of_failing_silently(
    client: TestClient, flat_project: dict, monkeypatch
):
    monkeypatch.setattr(magnific.settings, "magnific_api_key", None)
    response = client.post(
        f"/projects/{flat_project['project_id']}/layers/detect-product", json={}
    )
    assert response.status_code == 500
    assert "MAGNIFIC_API_KEY" in response.json()["detail"]


def test_the_original_artwork_is_never_modified(
    client: TestClient, flat_project: dict, monkeypatch
):
    """El relleno va a la plancha de fondo, nunca encima del archivo que subió el usuario."""
    project_id = flat_project["project_id"]
    source_path = storage.abs_path(project_id, flat_project["source"]["path"])
    before = source_path.read_bytes()

    _mock_magnific(monkeypatch, _cutout_png())
    client.post(
        f"/projects/{project_id}/layers/detect-product", json={"provider": "opencv"}
    )

    assert source_path.read_bytes() == before
    project = client.get(f"/projects/{project_id}").json()
    # Y la pieza queda con una plancha propia, distinta del original.
    assert project["background"]["path"] == "backgrounds/background.png"
    assert storage.abs_path(project_id, project["background"]["path"]) != source_path
