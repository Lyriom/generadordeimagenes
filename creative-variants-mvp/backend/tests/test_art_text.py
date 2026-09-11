"""Reescribir el copy del arte y quitar elementos (logos, sellos) de la pieza.

Un KV llega con el precio y el logo como píxeles. Estas pruebas cubren lo que hace
falta para producir la fila de artes de una promoción: cambiar ese texto sin mover
el diseño, quitar una marca del arte sin dejarla fantasma en el fondo, y que cada
tanda de un catálogo escriba su propio copy sin heredar el del producto anterior.
"""
from __future__ import annotations

import io
import itertools
import pathlib

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw

from app.services import art_text, storage

from .conftest import await_task
from .psd_fixture import write_psd


def _face(weight: str) -> str | None:
    """Ruta real de la cara pedida, con los mismos respaldos que el renderer."""
    from app.services.renderer import resolve_font_path

    from app.models import ProjectReferences

    stub = type("P", (), {"references": ProjectReferences(), "project_id": ""})()
    return resolve_font_path(stub, weight)


def _text_layer_image(text: str, size: int, fill: tuple[int, int, int], weight="bold"):
    """Capa RGBA con texto real: tinta opaca sobre transparencia, como un PSD."""
    from app.services.renderer import load_font

    probe = Image.new("RGBA", (1600, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    draw.multiline_text((20, 20), text, font=load_font(_face(weight), size), fill=(*fill, 255))
    return probe.crop(probe.getbbox())


def make_kv(path, width: int = 900, height: int = 660):
    """KV de prueba con la planta de un arte real de retail.

    Importa que el nombre del producto y el precio compartan banda horizontal:
    es la situación en la que un precio más largo se come a su vecino, y sin
    ella la regla que lo impide no se estaría probando.
    """
    layers = [
        {
            "name": "Relleno de color 1",
            "image": Image.new("RGBA", (width, height), (245, 245, 248, 255)),
            "position": (0, 0),
        },
        {
            "name": "LOGO MARCA",
            "image": Image.new("RGBA", (200, 70), (20, 90, 200, 255)),
            "position": (int(width * 0.05), int(height * 0.05)),
        },
        {
            "name": "Capa 5",
            "image": Image.new("RGBA", (300, 260), (200, 40, 40, 255)),
            "position": (int(width * 0.35), int(height * 0.18)),
        },
        {
            "name": "titular nombre producto",
            "image": _text_layer_image("COCINA A GAS\n40 20P CROMA", 26, (25, 25, 30)),
            "position": (60, 400),
        },
        {
            "name": "precio oferta",
            "image": _text_layer_image("$235.00", 56, (220, 30, 40)),
            "position": (520, 400),
        },
    ]
    return write_psd(path, (width, height), layers)


def psd_project(client: TestClient, tmp_path) -> dict:
    source = tmp_path / "kv.psd"
    make_kv(source)
    response = client.post(
        "/projects",
        data={"name": "KV con copy"},
        files={"artwork": ("kv.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def price_layer(project: dict) -> dict:
    return next(layer for layer in project["layers"] if layer["category"] == "price")


def logo_layer(project: dict) -> dict:
    return next(layer for layer in project["layers"] if layer["category"] == "logo")


def test_texts_lists_the_copy_with_its_measured_style(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    response = client.get(f"/projects/{project['project_id']}/texts")
    assert response.status_code == 200, response.text
    items = {item["category"]: item for item in response.json()["layers"]}

    assert "price" in items and "logo" in items
    # El producto y el fondo tienen su propio flujo: no se editan como copy.
    assert "product" not in items and "background" not in items

    price = items["price"]
    assert price["editable"] is True
    assert price["style"]["lines"] == 1
    assert price["style"]["ink_height"] > 8
    # El color medido es el de la tinta, no el del fondo transparente.
    red, green, blue = (int(price["style"]["color"][i : i + 2], 16) for i in (1, 3, 5))
    assert red > 150 and green < 110 and blue < 110

    # El PSD trae el logo como capa: sus píxeles no están dentro de la plancha.
    assert items["logo"]["in_plate"] is False


def test_rewriting_keeps_the_place_the_size_and_the_colour(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    original = price_layer(project)

    response = client.post(
        f"/projects/{project['project_id']}/layers/{original['id']}/text",
        json={"content": "$1.499,00"},
    )
    assert response.status_code == 200, response.text
    layer = response.json()["layer"]

    assert layer["type"] == "text"
    assert layer["content"] == "$1.499,00"
    assert layer["color"] == original["color"] or layer["color"].startswith("#")
    # El copy reescrito conserva el color medido de la tinta original.
    red = int(layer["color"][1:3], 16)
    assert red > 150

    # Sigue en su sitio: el centro vertical de la tinta no se mueve del original.
    before = original["y"] + original["height"] / 2
    after = layer["y"] + layer["height"] / 2
    assert abs(after - before) <= max(6, original["height"] * 0.35)
    # Y con el mismo peso visual: un texto más largo no encoge la tipografía.
    assert layer["font_size"] >= int(original["height"] * 0.5)
    # El color no se recalcula por contraste: es el del diseño.
    assert layer["auto_contrast"] is False


def test_rewriting_is_reversible(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    original = price_layer(project)
    project_id = project["project_id"]

    client.post(
        f"/projects/{project_id}/layers/{original['id']}/text",
        json={"content": "$99,00"},
    )
    response = client.post(
        f"/projects/{project_id}/layers/{original['id']}/text", json={"restore": True}
    )
    assert response.status_code == 200, response.text
    layer = response.json()["layer"]

    assert layer["type"] == "image"
    assert layer["src"] == original["src"]
    assert [layer["x"], layer["y"], layer["width"], layer["height"]] == [
        original["x"], original["y"], original["width"], original["height"]
    ]


def test_a_second_edit_starts_from_the_original_not_from_the_previous_text(
    client: TestClient, tmp_path
):
    """Editar dos veces no debe encadenar medidas: el original manda siempre."""
    project = psd_project(client, tmp_path)
    original = price_layer(project)
    project_id = project["project_id"]

    first = client.post(
        f"/projects/{project_id}/layers/{original['id']}/text",
        json={"content": "$1.999.999,00"},
    ).json()["layer"]
    second = client.post(
        f"/projects/{project_id}/layers/{original['id']}/text",
        json={"content": "$235.00"},
    ).json()["layer"]

    # El texto original vuelve a medir lo que medía, no lo que dejó el largo. La
    # tolerancia no es cero a propósito: el cuerpo se calcula contra el alto de
    # tinta del texto **nuevo**, y una coma que baja de la línea base ocupa unos
    # píxeles más que unos dígitos sueltos. La diferencia es de un 4 %, invisible.
    assert abs(second["height"] - first["height"]) <= max(3, first["height"] * 0.06)
    assert abs(second["y"] - first["y"]) <= max(3, first["height"] * 0.06)
    assert second["width"] < first["width"]


def test_the_variant_shows_the_new_copy(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    original = price_layer(project)

    client.post(
        f"/projects/{project_id}/layers/{original['id']}/text",
        json={"content": "$777,00"},
    )
    result = await_task(
        client,
        project_id,
        client.post(f"/projects/{project_id}/auto", json={"template_mode": True}),
    )
    assert result["variants"], result

    variant = result["variants"][0]
    placement = next(
        item for item in variant["placements"] if item["layer_id"] == original["id"]
    )
    assert placement["type"] == "text"
    assert placement["content"] == "$777,00"

    # Y en la imagen hay tinta roja donde va el precio: no quedó el hueco vacío.
    image = Image.open(storage.abs_path(project_id, variant["image"])).convert("RGB")
    patch = np.asarray(
        image.crop(
            (
                max(0, placement["x"] - 4),
                max(0, placement["y"] - 4),
                min(image.width, placement["x"] + placement["width"] + 4),
                min(image.height, placement["y"] + placement["height"] + 4),
            )
        ),
        dtype=np.int16,
    )
    ink = (patch[..., 0] > 140) & (patch[..., 1] < 120) & (patch[..., 2] < 120)
    assert ink.sum() > 40, "el precio reescrito no se pintó"


def test_removing_the_logo_takes_it_out_of_the_variant(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    logo = logo_layer(project)

    response = client.post(
        f"/projects/{project_id}/layers/{logo['id']}/text", json={"removed": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["layer"]["visible"] is False

    result = await_task(
        client,
        project_id,
        client.post(f"/projects/{project_id}/auto", json={"template_mode": True}),
    )
    variant = result["variants"][0]
    assert all(item["layer_id"] != logo["id"] for item in variant["placements"])
    # Quitarlo a propósito no es una falta de la pieza.
    assert "Falta el logo en la variante." not in variant["quality"]["warnings"]

    # Y se puede devolver.
    back = client.post(
        f"/projects/{project_id}/layers/{logo['id']}/text", json={"removed": False}
    )
    assert back.json()["layer"]["visible"] is True


def test_removing_a_flattened_logo_also_clears_the_plate(client: TestClient, project: dict):
    """En un arte aplanado, ocultar no basta: los píxeles siguen dentro del fondo."""
    project_id = project["project_id"]
    created = client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Logo",
            "category": "logo",
            "type": "image",
            "x": 30,
            "y": 24,
            "width": 102,
            "height": 48,
            "auto_segment": False,
        },
    )
    assert created.status_code == 201, created.text
    logo = created.json()

    listed = client.get(f"/projects/{project_id}/texts").json()["layers"]
    assert next(item for item in listed if item["id"] == logo["id"])["in_plate"] is True

    response = client.post(
        f"/projects/{project_id}/layers/{logo['id']}/text", json={"removed": True}
    )
    assert response.status_code == 200, response.text
    assert any("aplanados en el fondo" in warning for warning in response.json()["warnings"])

    source = np.asarray(
        Image.open(storage.abs_path(project_id, project["source"]["path"])).convert("RGB"),
        dtype=np.int16,
    )
    plate = np.asarray(
        Image.open(storage.abs_path(project_id, "backgrounds/background.png")).convert("RGB"),
        dtype=np.int16,
    )
    region = (slice(24, 72), slice(30, 132))
    assert np.abs(source[region] - plate[region]).mean() > art_text.PLATE_SAME_PIXELS

    # Fuera del logo el arte no se toca.
    resto = (slice(300, 400), slice(300, 400))
    assert np.abs(source[resto] - plate[resto]).mean() < 1.0


def test_restoring_a_flattened_logo_brings_the_plate_back(client: TestClient, project: dict):
    project_id = project["project_id"]
    logo = client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Logo",
            "category": "logo",
            "type": "image",
            "x": 30,
            "y": 24,
            "width": 102,
            "height": 48,
            "auto_segment": False,
        },
    ).json()

    client.post(f"/projects/{project_id}/layers/{logo['id']}/text", json={"removed": True})
    client.post(f"/projects/{project_id}/layers/{logo['id']}/text", json={"removed": False})

    source = np.asarray(
        Image.open(storage.abs_path(project_id, project["source"]["path"])).convert("RGB"),
        dtype=np.int16,
    )
    plate = np.asarray(
        Image.open(storage.abs_path(project_id, "backgrounds/background.png")).convert("RGB"),
        dtype=np.int16,
    )
    region = (slice(24, 72), slice(30, 132))
    assert np.abs(source[region] - plate[region]).mean() < art_text.PLATE_SAME_PIXELS


def test_each_batch_writes_its_own_copy(client: TestClient, tmp_path):
    """El precio de un producto no puede quedarse pegado en el arte del siguiente."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)

    first = await_task(
        client,
        project_id,
        client.post(
            f"/projects/{project_id}/auto",
            json={
                "template_mode": True,
                "text_overrides": [{"layer_id": price["id"], "content": "$235.00"}],
            },
        ),
    )
    assert first["variants"][0]["placements"]
    escrito = next(
        item
        for item in first["variants"][0]["placements"]
        if item["layer_id"] == price["id"]
    )
    assert escrito["content"] == "$235.00"

    # Segunda tanda con otro precio.
    second = await_task(
        client,
        project_id,
        client.post(
            f"/projects/{project_id}/auto",
            json={
                "template_mode": True,
                "replace_existing": True,
                "text_overrides": [{"layer_id": price["id"], "content": "$539.00"}],
            },
        ),
    )
    assert next(
        item
        for item in second["variants"][0]["placements"]
        if item["layer_id"] == price["id"]
    )["content"] == "$539.00"

    # Tercera tanda sin copy propio: vuelve al arte original, no hereda el anterior.
    third = await_task(
        client,
        project_id,
        client.post(
            f"/projects/{project_id}/auto",
            json={"template_mode": True, "replace_existing": True, "text_overrides": []},
        ),
    )
    restored = client.get(f"/projects/{project_id}").json()
    layer = next(item for item in restored["layers"] if item["id"] == price["id"])
    assert layer["type"] == "image"
    assert third["variants"]


def test_the_product_cannot_be_edited_as_copy(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    product = next(layer for layer in project["layers"] if layer["category"] == "product")
    response = client.post(
        f"/projects/{project['project_id']}/layers/{product['id']}/text",
        json={"content": "no"},
    )
    assert response.status_code == 400
    assert "propio flujo" in response.json()["detail"]


def test_an_element_without_ink_says_so_instead_of_inventing_a_size(
    client: TestClient, tmp_path
):
    project = psd_project(client, tmp_path)
    logo = logo_layer(project)
    # El logo de la prueba es un bloque liso: no tiene texto que medir, pero sí
    # es un rectángulo opaco, así que la tinta se busca por color y no aparece.
    response = client.post(
        f"/projects/{project['project_id']}/layers/{logo['id']}/text",
        json={"content": "MARCA"},
    )
    assert response.status_code == 400
    assert "no se puede reescribir" in response.json()["detail"]


def test_a_longer_text_shrinks_instead_of_invading_its_neighbour(
    client: TestClient, tmp_path
):
    """Un precio más largo no puede meterse debajo del titular de al lado."""
    project = psd_project(client, tmp_path)
    price = price_layer(project)
    vecino = next(
        layer for layer in project["layers"] if layer["category"] == "headline"
    )

    response = client.post(
        f"/projects/{project['project_id']}/layers/{price['id']}/text",
        json={"content": "$1.999.999.999.999.999,00"},
    )
    assert response.status_code == 200, response.text
    layer = response.json()["layer"]

    # No pisa al vecino de su izquierda…
    assert layer["x"] >= vecino["x"] + vecino["width"]
    # …ni se sale del arte.
    assert layer["x"] + layer["width"] <= project["canvas"]["width"]
    assert any("no cabía" in warning for warning in response.json()["warnings"])


def test_the_rewritten_copy_keeps_the_weight_of_the_original(
    client: TestClient, tmp_path
):
    """La negrita del arte se conserva; adivinarla por densidad no bastaba."""
    project = psd_project(client, tmp_path)
    if _face("bold") == _face("normal"):
        import pytest

        pytest.skip("El equipo no tiene una negrita instalada: no hay dos caras que elegir.")

    price = price_layer(project)
    response = client.post(
        f"/projects/{project['project_id']}/layers/{price['id']}/text",
        json={"content": "$539.00"},
    )
    # El precio del KV de prueba está escrito con la cara negrita.
    assert response.json()["layer"]["font_weight"] == "bold"


def test_marking_a_layer_as_unused_also_clears_it_from_the_plate(
    client: TestClient, project: dict
):
    """“No usar” en la revisión de roles tiene que quitar el elemento de verdad."""
    project_id = project["project_id"]
    logo = client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Logo",
            "category": "logo",
            "type": "image",
            "x": 30,
            "y": 24,
            "width": 102,
            "height": 48,
            "auto_segment": False,
        },
    ).json()

    response = client.put(
        f"/projects/{project_id}/layers",
        json={"updates": [{"id": logo["id"], "visible": False}]},
    )
    assert response.status_code == 200, response.text

    source = np.asarray(
        Image.open(storage.abs_path(project_id, project["source"]["path"])).convert("RGB"),
        dtype=np.int16,
    )
    plate = np.asarray(
        Image.open(storage.abs_path(project_id, "backgrounds/background.png")).convert("RGB"),
        dtype=np.int16,
    )
    region = (slice(24, 72), slice(30, 132))
    assert np.abs(source[region] - plate[region]).mean() > art_text.PLATE_SAME_PIXELS


def test_a_psd_kv_never_duplicates_its_plate(client: TestClient, tmp_path):
    """Sin nada que borrar no se guarda una copia del fondo: son megas por proyecto."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)

    client.post(
        f"/projects/{project_id}/layers/{price['id']}/text", json={"content": "$99,00"}
    )
    copia = storage.abs_path(project_id, art_text.PLATE_BASELINE_REL)
    assert not copia.exists()


def test_marking_a_layer_as_part_of_the_background_never_empties_the_plate(
    client: TestClient, project: dict
):
    """“Parte del fondo” también oculta la capa, pero ahí no hay nada que borrar."""
    project_id = project["project_id"]
    banda = client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Banda inferior",
            "category": "decoration",
            "type": "image",
            "x": 0,
            "y": 372,
            "width": 600,
            "height": 220,
            "auto_segment": False,
        },
    ).json()

    response = client.put(
        f"/projects/{project_id}/layers",
        json={
            "updates": [
                {"id": banda["id"], "category": "background", "visible": False}
            ]
        },
    )
    assert response.status_code == 200, response.text

    plate = storage.abs_path(project_id, "backgrounds/background.png")
    if not plate.exists():
        return  # sin plancha propia no hay nada que comprobar: nada se tocó

    source = np.asarray(
        Image.open(storage.abs_path(project_id, project["source"]["path"])).convert("RGB"),
        dtype=np.int16,
    )
    fondo = np.asarray(Image.open(plate).convert("RGB"), dtype=np.int16)
    region = (slice(400, 560), slice(20, 560))
    assert np.abs(source[region] - fondo[region]).mean() < art_text.PLATE_SAME_PIXELS


def brand_font_bytes(weight: str = "normal") -> bytes:
    """Una tipografía real del equipo, para hacer de «fuente de marca»."""
    path = _face(weight)
    assert path, "el equipo no tiene ninguna tipografía TrueType"
    return pathlib.Path(path).read_bytes()


def test_texts_says_when_there_is_no_brand_font(client: TestClient, tmp_path):
    """Sin tipografía de marca, el copy reescrito sale con la del sistema."""
    project = psd_project(client, tmp_path)
    body = client.get(f"/projects/{project['project_id']}/texts").json()
    assert body["brand_font"] is False
    assert body["brand_font_bold"] is False


def test_the_brand_font_can_be_added_after_importing_the_kv(client: TestClient, tmp_path):
    """Se sube tres pasos después de crear el proyecto, que es cuando hace falta."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]

    response = client.post(
        f"/projects/{project_id}/references/font",
        files={
            "font": ("marca.ttf", brand_font_bytes("normal"), "font/ttf"),
            "font_bold": ("marca-bold.ttf", brand_font_bytes("bold"), "font/ttf"),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["font"] and body["font_bold"]

    listed = client.get(f"/projects/{project_id}/texts").json()
    assert listed["brand_font"] is True and listed["brand_font_bold"] is True

    # Y el render la usa para las dos caras.
    from app.services import storage as storage_service
    from app.services.renderer import resolve_font_path

    saved = storage_service.load_project(project_id)
    assert resolve_font_path(saved, "normal") == str(
        storage_service.abs_path(project_id, body["font"])
    )
    assert resolve_font_path(saved, "bold") == str(
        storage_service.abs_path(project_id, body["font_bold"])
    )


def test_only_the_regular_face_still_serves_both_weights(client: TestClient, tmp_path):
    """Una sola cara de marca se parece al arte más que una negrita ajena."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    client.post(
        f"/projects/{project_id}/references/font",
        files={"font": ("marca.ttf", brand_font_bytes("normal"), "font/ttf")},
    )

    from app.services import storage as storage_service
    from app.services.renderer import resolve_font_path

    saved = storage_service.load_project(project_id)
    assert resolve_font_path(saved, "bold") == resolve_font_path(saved, "normal")


def test_adding_the_brand_font_remeasures_the_copy_already_rewritten(
    client: TestClient, tmp_path
):
    """El cuerpo y la caja venían medidos contra la cara anterior: se rehacen."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)

    before = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text",
        json={"content": "$539.00"},
    ).json()["layer"]

    response = client.post(
        f"/projects/{project_id}/references/font",
        files={"font": ("marca.ttf", brand_font_bytes("normal"), "font/ttf")},
    )
    assert response.json()["rewritten"] == 1

    after = next(
        layer
        for layer in client.get(f"/projects/{project_id}").json()["layers"]
        if layer["id"] == price["id"]
    )
    # Sigue diciendo lo mismo y en su banda, pero con las métricas de la cara nueva.
    assert after["content"] == "$539.00"
    assert after["type"] == "text"
    assert abs(after["y"] - before["y"]) <= max(4, before["height"] * 0.3)


def test_uploading_no_face_at_all_is_rejected(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    response = client.post(f"/projects/{project['project_id']}/references/font", files={})
    assert response.status_code == 400
    assert "al menos una" in response.json()["detail"]


def tight_two_line_kv(path, width: int = 900, height: int = 660):
    """KV cuyo copy de dos líneas va con la interlínea pegada de un arte real."""
    from app.services.renderer import load_font

    probe = Image.new("RGBA", (1200, 400), (0, 0, 0, 0))
    ImageDraw.Draw(probe).multiline_text(
        (20, 20),
        "LAVADORA AUTOMATICA\nWT19WVTM 19 KG",
        font=load_font(_face("bold"), 30),
        fill=(25, 25, 30, 255),
        spacing=0,
    )
    return write_psd(
        path,
        (width, height),
        [
            {
                "name": "Relleno de color 1",
                "image": Image.new("RGBA", (width, height), (245, 245, 248, 255)),
                "position": (0, 0),
            },
            {
                "name": "titular nombre producto",
                "image": probe.crop(probe.getbbox()),
                "position": (60, 400),
            },
        ],
    )


def test_tight_leading_is_read_as_two_lines_not_one(client: TestClient, tmp_path):
    """Dos líneas pegadas se medían como una sola con el doble de alto.

    El texto nuevo se escribía entonces a más del doble de cuerpo que el
    original: el defecto más visible que puede tener esta función.
    """
    source = tmp_path / "apretado.psd"
    tight_two_line_kv(source)
    created = client.post(
        "/projects",
        data={"name": "Interlínea apretada"},
        files={"artwork": ("kv.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    )
    project = created.json()
    titular = next(
        layer for layer in project["layers"] if layer["category"] == "headline"
    )

    listed = client.get(f"/projects/{project['project_id']}/texts").json()["layers"]
    medido = next(item for item in listed if item["id"] == titular["id"])
    assert medido["style"]["lines"] == 2, "las dos líneas se leyeron como una"
    # El alto de tinta es el de UNA línea, no el del bloque entero.
    assert medido["style"]["ink_height"] < titular["height"] * 0.7
    assert medido["style"]["line_pitch"] > 0

    response = client.post(
        f"/projects/{project['project_id']}/layers/{titular['id']}/text",
        json={"content": "COCINA A GAS\n40 20P CROMA"},
    )
    assert response.status_code == 200, response.text
    escrito = response.json()["layer"]
    # Y el bloque nuevo ocupa lo que ocupaba el viejo, no el doble.
    assert escrito["height"] <= titular["height"] * 1.45, (
        f"el texto se escribió a {escrito['height']}px donde el original medía "
        f"{titular['height']}px"
    )


def test_the_plate_comparison_notices_that_the_plate_changed(
    client: TestClient, project: dict
):
    """La comparación se cachea por fecha de archivo: borrar debe invalidarla."""
    project_id = project["project_id"]
    logo = client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Logo",
            "category": "logo",
            "type": "image",
            "x": 30,
            "y": 24,
            "width": 102,
            "height": 48,
            "auto_segment": False,
        },
    ).json()

    saved = storage.load_project(project_id)
    capa = saved.layer_by_id(logo["id"])
    assert art_text.pixels_in_plate(saved, capa) is True

    client.post(f"/projects/{project_id}/layers/{logo['id']}/text", json={"removed": True})

    # La plancha ya no lo contiene, pero la copia limpia sí: la respuesta se
    # mide contra ella, así que sigue diciendo que estaba aplanado. Lo que no
    # puede pasar es que la caché devuelva un array de otro archivo.
    saved = storage.load_project(project_id)
    capa = saved.layer_by_id(logo["id"])
    assert art_text.pixels_in_plate(saved, capa) is True
    assert capa.meta.get("erased_from_plate") is True


def test_removing_wins_over_rewriting(client: TestClient, tmp_path):
    """Un texto por producto no puede devolver al arte un logo ya retirado."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)

    client.post(f"/projects/{project_id}/layers/{price['id']}/text", json={"removed": True})
    response = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text", json={"content": "$539.00"}
    )
    assert response.status_code == 200, response.text
    layer = response.json()["layer"]
    assert layer["content"] == "$539.00"
    assert layer["visible"] is False, "escribir resucitó un elemento retirado"

    # Y devolverlo al arte lo trae con el texto nuevo.
    back = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text", json={"removed": False}
    ).json()["layer"]
    assert back["visible"] is True and back["content"] == "$539.00"


def test_moving_a_rewritten_text_by_hand_keeps_the_new_place(
    client: TestClient, tmp_path
):
    """Mover el texto en Revisar capas y luego reescribirlo no debe devolverlo de un salto."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)

    client.post(
        f"/projects/{project_id}/layers/{price['id']}/text", json={"content": "$449.00"}
    )
    movido = client.put(
        f"/projects/{project_id}/layers",
        json={"updates": [{"id": price["id"], "x": 120, "y": 240}]},
    )
    assert movido.status_code == 200, movido.text

    otra = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text", json={"content": "$479.00"}
    ).json()["layer"]
    # Exacto, no aproximado: el ancla se guarda en coordenadas de tinta, así que
    # el bloque vuelve al mismo píxel al que lo movió el usuario.
    assert otra["y"] == 240, "el texto se movió respecto de donde lo dejó el usuario"
    assert otra["x"] == 120


def test_the_svg_names_the_font_it_was_actually_drawn_with(client: TestClient, tmp_path):
    """El SVG de Illustrator pedía DejaVu aunque el arte usara la fuente de marca."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)

    sin_marca = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text",
        json={"content": "$539.00"},
    ).json()["layer"]

    client.post(
        f"/projects/{project_id}/references/font",
        files={"font": ("marca.ttf", brand_font_bytes("normal"), "font/ttf")},
    )
    con_marca = next(
        layer
        for layer in client.get(f"/projects/{project_id}").json()["layers"]
        if layer["id"] == price["id"]
    )

    from app.services.renderer import font_family_name

    assert con_marca["font_family"] == font_family_name(_face("normal"))
    assert con_marca["font_family"], "no se leyó la familia del archivo"
    # Y aparece en el SVG que se descarga.
    resultado = await_task(
        client,
        project_id,
        client.post(f"/projects/{project_id}/auto", json={"template_mode": True}),
    )
    svg = storage.abs_path(project_id, resultado["variants"][0]["meta"]["svg"]).read_text()
    assert con_marca["font_family"] in svg
    # Volver al original deja la familia como estaba.
    vuelto = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text", json={"restore": True}
    ).json()["layer"]
    assert vuelto["font_family"] == price["font_family"]
    assert sin_marca["font_family"]


def test_the_svg_carries_the_rewritten_copy_as_text_and_the_rest_as_pixels(
    client: TestClient, tmp_path
):
    """Illustrator debe poder retocar lo que reescribimos, no lo que no sabemos."""
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    price = price_layer(project)
    headline = next(
        layer for layer in project["layers"] if layer["category"] == "headline"
    )

    client.post(
        f"/projects/{project_id}/layers/{price['id']}/text",
        json={"content": "$809.00"},
    )
    resultado = await_task(
        client,
        project_id,
        client.post(f"/projects/{project_id}/auto", json={"template_mode": True}),
    )
    variant = resultado["variants"][0]
    svg = storage.abs_path(project_id, variant["meta"]["svg"]).read_text()

    # El precio reescrito: texto real y editable.
    assert "$809.00" in svg
    assert 'data-editable="true"' in svg
    # El titular no se tocó: sigue siendo píxeles, porque de un objeto
    # inteligente de Photoshop no sabemos con qué estaba escrito. El manifiesto
    # del ZIP es donde el diseñador lee qué puede retocar.
    import io
    import json
    import zipfile

    descarga = client.get(f"/projects/{project_id}/export")
    assert descarga.status_code == 200, descarga.text
    with zipfile.ZipFile(io.BytesIO(descarga.content)) as archivo:
        manifiesto = json.loads(archivo.read("manifest.json"))
    editables = manifiesto["variants"][0]["editable_text_layers"]
    assert price["name"] in editables
    assert headline["name"] not in editables


# ------------------------------------------------------------------- piezas
# Un bloque de precio de agencia viene entero en una capa: el rótulo "PRECIO
# OFERTA", el precio, el precio anterior y el sello. Reescribirlo como un solo
# texto no sirve para cambiar solo el precio, que es lo que se hace siempre.


def _ink(text: str, size: int, fill: tuple[int, int, int, int]):
    """El texto recortado a su tinta, sin el aire que deja la tipografía."""
    from app.services.renderer import load_font

    probe = Image.new("RGBA", (1400, 500), (0, 0, 0, 0))
    ImageDraw.Draw(probe).text((20, 20), text, font=load_font(_face("bold"), size), fill=fill)
    return probe.crop(probe.getbbox())


def _price_block_image():
    """Una capa con cuatro piezas distintas, como las que trae el PSD real.

    Las piezas se pegan con huecos exactos (14, 4 y 11 px) porque así están en
    el arte de verdad: más cerca de lo que separa la tilde de una Á de su letra.
    Con huecos holgados esta prueba pasaba con una regla que fallaba en el arte
    real, y midiéndolos a partir de la tipografía cambiaba entre macOS y Docker,
    que no tienen las mismas caras instaladas.
    """
    azul = (23, 62, 110, 255)
    sello = Image.new("RGBA", (242, 40), (0, 0, 0, 0))
    trazo = ImageDraw.Draw(sello)
    trazo.rounded_rectangle([0, 0, 241, 39], radius=19, fill=(146, 214, 46, 255))
    etiqueta = _ink("EXCLUSIVO ONLINE", 22, (12, 40, 70, 255))
    sello.paste(etiqueta, (16, (40 - etiqueta.height) // 2), etiqueta)

    piezas = [
        (_ink("PRECIO OFERTA", 34, azul), 0),
        (_ink("$235.00", 150, azul), 14),
        (_ink("P. ANTES $253.66", 40, azul), 4),
        (sello, 11),
    ]
    ancho = max(pieza.width for pieza, _ in piezas)
    alto = sum(pieza.height for pieza, _ in piezas) + sum(hueco for _, hueco in piezas)
    image = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    y = 0
    for pieza, hueco in piezas:
        y += hueco
        image.paste(pieza, (0, y), pieza)
        y += pieza.height
    return image


def block_psd(path, width: int = 900, height: int = 900):
    return write_psd(
        path,
        (width, height),
        [
            {
                "name": "Relleno de color 1",
                "image": Image.new("RGBA", (width, height), (245, 245, 248, 255)),
                "position": (0, 0),
            },
            {"name": "bloque precio", "image": _price_block_image(), "position": (120, 300)},
        ],
    )


@pytest.fixture()
def block_project(client: TestClient, tmp_path) -> dict:
    source = tmp_path / "bloques.psd"
    block_psd(source)
    response = client.post(
        "/projects",
        data={"name": "KV con bloque de precio"},
        files={"artwork": ("bloques.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _block_layer(project: dict) -> dict:
    return next(layer for layer in project["layers"] if "bloque" in layer["name"])


def test_the_inventory_says_how_many_pieces_a_layer_holds(
    client: TestClient, block_project
):
    listed = client.get(f"/projects/{block_project['project_id']}/texts").json()
    block = next(
        item for item in listed["layers"] if item["id"] == _block_layer(block_project)["id"]
    )
    # Rótulo, precio, precio anterior y sello.
    assert block["pieces"] == 4


def test_a_paragraph_is_not_cut_into_pieces(client: TestClient, tmp_path):
    """Dos líneas del mismo cuerpo y color son un texto, no dos piezas."""
    from app.services.renderer import load_font

    image = Image.new("RGBA", (700, 160), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(["Aplican condiciones. Válido", "para productos elegidos."]):
        draw.text((10, 10 + index * 48), line, font=load_font(_face("bold"), 34), fill=(23, 62, 110, 255))
    source = tmp_path / "parrafo.psd"
    write_psd(
        source,
        (900, 400),
        [
            {
                "name": "Relleno de color 1",
                "image": Image.new("RGBA", (900, 400), (245, 245, 248, 255)),
                "position": (0, 0),
            },
            {"name": "legal", "image": image, "position": (60, 120)},
        ],
    )
    created = client.post(
        "/projects",
        data={"name": "Legal"},
        files={"artwork": ("parrafo.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    ).json()
    listed = client.get(f"/projects/{created['project_id']}/texts").json()
    legal = next(item for item in listed["layers"] if item["name"] == "legal")
    assert legal["pieces"] == 1


def test_splitting_turns_each_piece_into_its_own_element(
    client: TestClient, block_project
):
    """Separado, el precio es un elemento: se reescribe sin tocar a los demás."""
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    response = client.post(f"/projects/{project_id}/layers/{block['id']}/split")
    assert response.status_code == 200, response.text
    parts = response.json()["layers"]
    assert len(parts) == 4
    # Cada parte queda donde estaba su tinta, dentro de la caja de la madre.
    for part in parts:
        assert block["x"] <= part["x"] <= block["x"] + block["width"]
        assert block["y"] <= part["y"] <= block["y"] + block["height"]
    # Y de arriba abajo, en el orden en que se leen.
    assert [part["y"] for part in parts] == sorted(part["y"] for part in parts)

    # La madre ya no está en el arte, pero cada parte sí se puede reescribir.
    project = client.get(f"/projects/{project_id}").json()
    assert block["id"] not in [layer["id"] for layer in project["layers"]]
    precio = max(parts, key=lambda item: item["height"])
    rewrite = client.post(
        f"/projects/{project_id}/layers/{precio['id']}/text",
        json={"content": "$199.00"},
    )
    assert rewrite.status_code == 200, rewrite.text
    assert rewrite.json()["layer"]["content"] == "$199.00"

    # Los otros tres siguen intactos: eso es lo que no se podía hacer antes.
    after = client.get(f"/projects/{project_id}").json()
    for part in parts:
        if part["id"] == precio["id"]:
            continue
        current = next(item for item in after["layers"] if item["id"] == part["id"])
        assert current["type"] == "image"
        assert current["src"] == part["src"]


def test_a_piece_can_be_removed_without_touching_the_rest(
    client: TestClient, block_project
):
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    sello = parts[-1]
    response = client.post(
        f"/projects/{project_id}/layers/{sello['id']}/text", json={"removed": True}
    )
    assert response.status_code == 200, response.text
    after = client.get(f"/projects/{project_id}").json()
    quitado = next(item for item in after["layers"] if item["id"] == sello["id"])
    assert quitado["visible"] is False
    for part in parts[:-1]:
        assert next(item for item in after["layers"] if item["id"] == part["id"])["visible"]


def test_the_pieces_can_be_put_back_together(client: TestClient, block_project):
    """Separar no es una puerta de un solo sentido."""
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    response = client.post(f"/projects/{project_id}/layers/{parts[0]['id']}/unsplit")
    assert response.status_code == 200, response.text
    after = client.get(f"/projects/{project_id}").json()
    ids = [layer["id"] for layer in after["layers"]]
    assert block["id"] in ids
    for part in parts:
        assert part["id"] not in ids


def test_putting_the_pieces_back_keeps_what_was_written_in_them(
    client: TestClient, block_project
):
    """Juntar no es deshacer: el precio nuevo no puede volver al viejo."""
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    precio = parts[1]
    escrito = client.post(
        f"/projects/{project_id}/layers/{precio['id']}/text",
        json={"content": "$599.00"},
    )
    assert escrito.status_code == 200, escrito.text

    response = client.post(f"/projects/{project_id}/layers/{parts[0]['id']}/unsplit")
    assert response.status_code == 200, response.text

    after = client.get(f"/projects/{project_id}").json()
    merged = next(item for item in after["layers"] if item["id"] == block["id"])
    for part in parts:
        assert part["id"] not in [layer["id"] for layer in after["layers"]]

    # Los píxeles del conjunto son nuevos, no el recorte con el que llegó.
    assert merged["src"] != block["src"]
    listed = client.get(f"/projects/{project_id}/texts").json()
    fila = next(item for item in listed["layers"] if item["id"] == block["id"])
    assert "$599.00" in fila["text"]
    # Y la miniatura enseña el resultado, no el original que acaba de cambiar.
    assert fila["src"] == merged["src"]

    # Lo que importa: la tinta del precio nuevo está dentro del PNG unido.
    precio_ahora = next(
        item for item in client.get(f"/projects/{project_id}/texts").json()["layers"]
        if item["id"] == block["id"]
    )
    assert precio_ahora["text"].strip()
    with Image.open(storage.abs_path(project_id, merged["src"])) as opened:
        pixels = np.asarray(opened.convert("RGBA"))
    assert pixels.shape[:2] == (merged["height"], merged["width"])
    assert pixels[..., 3].max() > 200, "el conjunto salió transparente"


def test_a_piece_removed_before_merging_stays_out_of_the_art(
    client: TestClient, block_project
):
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    sello = parts[-1]
    client.post(f"/projects/{project_id}/layers/{sello['id']}/text", json={"removed": True})

    response = client.post(f"/projects/{project_id}/layers/{parts[0]['id']}/unsplit")
    assert response.status_code == 200, response.text

    merged = next(
        item for item in client.get(f"/projects/{project_id}").json()["layers"]
        if item["id"] == block["id"]
    )
    unido = storage.abs_path(project_id, merged["src"])
    with Image.open(unido) as opened:
        pixels = np.asarray(opened.convert("RGBA"))
    # El hueco que ocupaba el sello quedó transparente: no volvió al juntarlas.
    hueco = pixels[
        sello["y"] - merged["y"] : sello["y"] - merged["y"] + sello["height"],
        sello["x"] - merged["x"] : sello["x"] - merged["x"] + sello["width"],
        3,
    ]
    assert hueco.size, "el sello cayó fuera de la caja del conjunto"
    assert hueco.max() < 96
    # Y las demás piezas sí están.
    assert pixels[..., 3].max() > 200


def test_the_original_comes_back_after_merging_edited_pieces(
    client: TestClient, block_project
):
    """«Volver al original» sigue siendo la salida, incluso tras juntar."""
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    client.post(
        f"/projects/{project_id}/layers/{parts[1]['id']}/text", json={"content": "$599.00"}
    )
    client.post(f"/projects/{project_id}/layers/{parts[0]['id']}/unsplit")

    response = client.post(
        f"/projects/{project_id}/layers/{block['id']}/text", json={"restore": True}
    )
    assert response.status_code == 200, response.text
    restored = next(
        item for item in client.get(f"/projects/{project_id}").json()["layers"]
        if item["id"] == block["id"]
    )
    assert restored["src"] == block["src"]
    assert [restored["x"], restored["y"], restored["width"], restored["height"]] == [
        block["x"], block["y"], block["width"], block["height"]
    ]


def test_rewriting_the_pieces_does_not_make_them_invade_each_other(
    client: TestClient, block_project
):
    """La caja de un texto reescrito mide su tinta, no el bloque tipográfico.

    Un bloque mide ascendente más descendente: bastante más alto que los trazos
    que se ven. Usarlo como caja hacía que el precio reescrito se solapara con
    el rótulo de arriba y con el sello de abajo sin dibujar nada encima, y unas
    cajas solapadas descuadran el reparto del diseño anclado y el control de
    calidad. Aquí se comprueba lo que el arte necesita: que sigan sin pisarse.
    """
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    antes = {p["id"]: dict(y=p["y"], h=p["height"]) for p in parts}

    for parte, texto in zip(parts, ["Precio de muerte", "$900", "P. ANTES 1200"]):
        respuesta = client.post(
            f"/projects/{project_id}/layers/{parte['id']}/text", json={"content": texto}
        )
        assert respuesta.status_code == 200, respuesta.text

    despues = [
        capa
        for capa in client.get(f"/projects/{project_id}").json()["layers"]
        if capa["id"] in antes
    ]
    for a, b in itertools.combinations(despues, 2):
        alto = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
        ancho = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
        assert not (alto > 0 and ancho > 0), (
            f"'{a['name']}' y '{b['name']}' se solapan {ancho}x{alto} px tras reescribir"
        )

    # Y cada una sigue ocupando aproximadamente el alto que ocupaba: el cuerpo
    # se elige para que la tinta nueva mida como la vieja.
    for capa in despues:
        original = antes[capa["id"]]
        assert abs(capa["height"] - original["h"]) <= max(4, original["h"] * 0.25), (
            f"'{capa['name']}' pasó de {original['h']}px de alto a {capa['height']}px"
        )


def test_a_rewritten_text_keeps_its_ink_where_the_original_had_it(
    client: TestClient, block_project
):
    """Medir la caja por la tinta no puede mover el texto de sitio."""
    project_id = block_project["project_id"]
    block = _block_layer(block_project)
    parts = client.post(f"/projects/{project_id}/layers/{block['id']}/split").json()["layers"]
    precio = parts[1]

    client.post(
        f"/projects/{project_id}/layers/{precio['id']}/text", json={"content": "$900"}
    )
    despues = next(
        capa
        for capa in client.get(f"/projects/{project_id}").json()["layers"]
        if capa["id"] == precio["id"]
    )
    # La tinta arranca en la misma fila que la del recorte original.
    assert despues["y"] == precio["y"]
    origen = despues["meta"]["art_text"]
    assert origen["box"][1] == precio["y"]
    # Y el bloque que se dibuja de verdad es más alto que la caja: por eso hacía
    # falta separar las dos medidas.
    assert origen["block_height"] >= despues["height"]


def test_a_single_piece_layer_refuses_to_split(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    price = price_layer(project)
    response = client.post(
        f"/projects/{project['project_id']}/layers/{price['id']}/split"
    )
    assert response.status_code == 400
    assert "una sola pieza" in response.json()["detail"]


def test_rewriting_without_the_brand_font_says_so(client: TestClient, tmp_path):
    """El aviso llega al guardar, no solo en el recuadro del principio.

    Es el fallo que se vio en producción: se reescribe, sale con la letra del
    sistema y nadie se enteró hasta ver el arte terminado.
    """
    project = psd_project(client, tmp_path)
    price = price_layer(project)
    response = client.post(
        f"/projects/{project['project_id']}/layers/{price['id']}/text",
        json={"content": "$99.00"},
    )
    assert response.status_code == 200, response.text
    assert any("tipografía del sistema" in w for w in response.json()["warnings"])


def test_with_the_brand_font_there_is_no_such_warning(client: TestClient, tmp_path):
    project = psd_project(client, tmp_path)
    project_id = project["project_id"]
    face = _face("bold")
    assert face, "el entorno de pruebas no tiene ninguna cara instalada"
    with open(face, "rb") as handle:
        payload = handle.read()
    saved = client.post(
        f"/projects/{project_id}/references/font",
        files={"font": ("marca.ttf", payload, "font/ttf")},
    )
    assert saved.status_code == 200, saved.text

    price = price_layer(project)
    response = client.post(
        f"/projects/{project_id}/layers/{price['id']}/text",
        json={"content": "$99.00"},
    )
    assert response.status_code == 200, response.text
    assert not any("tipografía del sistema" in w for w in response.json()["warnings"])
    # Y la capa declara la familia del archivo subido, que es lo que el SVG
    # le pide a Illustrator al abrirlo.
    assert response.json()["layer"]["font_family"]


# ------------------------------------------- centavos en volado dentro de una línea
"""El separador cortaba solo por bandas horizontales.

Un precio de retail lleva los centavos en volado —más pequeños y más arriba— en
la **misma** fila que los enteros, así que nunca se separaban: reescribir el
precio los rebajaba al cuerpo de los enteros y el arte perdía su forma. Ahora se
corta también por cuerpo dentro de la línea.

La firma que se busca es estrecha a propósito (remate más bajo y que acaba más
arriba), porque un separador que se pasa de listo parte palabras normales y eso
es peor que no cortar: son doce casos, y diez son de los que NO deben cortarse.
"""


def _piezas_de(dibujar, size=(700, 260)) -> int:
    lienzo = Image.new("RGBA", size, (0, 0, 0, 0))
    dibujar(ImageDraw.Draw(lienzo))
    rgb = np.asarray(lienzo.convert("RGB"), dtype=np.uint8)
    alpha = np.asarray(lienzo.getchannel("A"), dtype=np.uint8)
    if not art_text._ink_mask(rgb, alpha).any():
        return 0
    return len(art_text._block_boxes(rgb, alpha))


def _linea(texto: str, cuerpo: int):
    from app.services.renderer import load_font

    return lambda draw: draw.text(
        (20, 40), texto, font=load_font(_face("bold"), cuerpo), fill=(23, 62, 110, 255)
    )


def _precio(entero: str, centavos: str, cuerpo: int, cuerpo_centavos: int):
    from app.services.renderer import load_font

    def dibujar(draw):
        cara = load_font(_face("bold"), cuerpo)
        draw.text((20, 40), entero, font=cara, fill=(23, 62, 110, 255))
        ancho = draw.textlength(entero, font=cara)
        draw.text(
            (20 + ancho + 6, 48),
            centavos,
            font=load_font(_face("bold"), cuerpo_centavos),
            fill=(23, 62, 110, 255),
        )

    return dibujar


@pytest.mark.parametrize(
    "nombre, dibujar",
    [
        ("$43 con ,99 en volado", _precio("$43", ",99", 150, 80)),
        ("$1.459 con ,00 en volado", _precio("$1.459", ",00", 140, 72)),
    ],
)
def test_raised_cents_become_their_own_piece(nombre, dibujar):
    assert _piezas_de(dibujar) == 2, f"no se separó el volado de {nombre}"


@pytest.mark.parametrize(
    "texto, cuerpo",
    [
        ("MENSUALES", 40),
        ("12 CUOTAS", 44),
        ("P. ANTES $889.37", 40),
        # El asterisco es pequeño y va alto, como un volado: lo salva el mínimo
        # de ancho, porque un remate de tres columnas no es una pieza.
        ("SIN INTERESES*", 56),
        # Rasgos descendentes: la p y la q bajan del renglón. Crecen hacia
        # abajo, no hacia arriba, así que no disparan el corte.
        ("Balcones transparentes", 34),
        ("Compra y paga después", 34),
        ("EXCLUSIVO ONLINE", 30),
        ("SIDE BY SIDE 476L", 46),
        ("$499", 150),
        ("12 MESES", 80),
    ],
)
def test_ordinary_lines_are_not_cut_in_two(texto, cuerpo):
    assert _piezas_de(_linea(texto, cuerpo)) == 1, f"'{texto}' se partió sin motivo"


def test_a_two_line_paragraph_is_never_cut_by_body_size():
    """La trampa en la que cayó la primera versión del corte.

    En un párrafo de dos renglones la primera línea suele ser más larga. Sus
    columnas de la derecha llevan tinta solo arriba, así que miden poco y acaban
    alto: exactamente la firma de un volado. El corte partía en dos cualquier
    legal de dos líneas. Por eso solo se mira dentro de bandas de una línea.
    """
    from app.services.renderer import load_font

    def dibujar(draw):
        cara = load_font(_face("bold"), 34)
        for indice, linea in enumerate(
            ["Aplican condiciones. Válido para productos", "elegidos."]
        ):
            draw.text((10, 10 + indice * 48), linea, font=cara, fill=(23, 62, 110, 255))

    assert _piezas_de(dibujar, size=(900, 200)) == 1


def test_the_cut_lands_in_the_gap_and_not_inside_the_last_digit(
    client: TestClient, tmp_path
):
    """El corte va al blanco entre los dos trazos, no donde cambia el perfil.

    El último dígito remata en una cola corta —la curva de un 3— que ya parece
    un volado. Cortar ahí le arrancaba una esquina, que se quedaba pegada a los
    centavos y salía en el arte como un trazo suelto al lado del precio nuevo.
    Se comprueba componiendo: los huecos de tinta del arte con el entero
    reescrito tienen que coincidir con los del original de los centavos en
    adelante.
    """
    from app.services.renderer import load_font

    def bloque() -> Image.Image:
        lienzo = Image.new("RGBA", (520, 220), (0, 0, 0, 0))
        draw = ImageDraw.Draw(lienzo)
        cara = load_font(_face("bold"), 150)
        draw.text((20, 20), "$43", font=cara, fill=(23, 62, 110, 255))
        draw.text(
            (20 + draw.textlength("$43", font=cara) + 6, 28),
            ",99",
            font=load_font(_face("bold"), 80),
            fill=(23, 62, 110, 255),
        )
        return lienzo

    source = tmp_path / "precio.psd"
    write_psd(
        source,
        (1080, 1080),
        [
            {
                "name": "Relleno de color 1",
                "image": Image.new("RGBA", (1080, 1080), (245, 245, 248, 255)),
                "position": (0, 0),
            },
            {"name": "precio", "image": bloque(), "position": (200, 400)},
        ],
    )
    project = client.post(
        "/projects",
        data={"name": "precio"},
        files={"artwork": ("precio.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    ).json()
    project_id = project["project_id"]
    capa = next(item for item in project["layers"] if "precio" in item["name"])

    def huecos() -> list[tuple[int, int]]:
        respuesta = client.get(f"/projects/{project_id}/preview/template")
        assert respuesta.status_code == 200, respuesta.text
        arte = np.asarray(
            Image.open(io.BytesIO(respuesta.content)).convert("RGB"), dtype=int
        )
        tinta = np.abs(arte - np.array([245, 245, 248])).sum(axis=2) > 60
        columnas = np.nonzero(tinta.any(axis=0))[0]
        return [
            (int(columnas[i - 1]), int(columnas[i]))
            for i in range(1, columnas.size)
            if columnas[i] - columnas[i - 1] > 3
        ]

    def parecidos(a, b, holgura: int = 1) -> bool:
        """Mismos huecos, con un píxel de holgura.

        La caja de una pieza se ajusta a su máscara de tinta, que descarta el
        borde más suavizado del trazo. Al separar se pierde ese píxel: no se ve,
        pero mueve el hueco uno. Lo que no puede cambiar es cuántos huecos hay.
        """
        return len(a) == len(b) and all(
            abs(x1 - x2) <= holgura and abs(y1 - y2) <= holgura
            for (x1, y1), (x2, y2) in zip(a, b)
        )

    antes = huecos()
    separado = client.post(f"/projects/{project_id}/layers/{capa['id']}/split")
    assert separado.status_code == 200, separado.text
    partes = separado.json()["layers"]
    assert len(partes) == 2, "no se separó el volado"
    assert parecidos(huecos(), antes), "separar cambió el arte"

    entero = max(partes, key=lambda item: item["width"] * item["height"])
    centavos = min(partes, key=lambda item: item["width"] * item["height"])
    # El invariante, y no vale de otra forma: entre las dos piezas queda blanco.
    # Si el corte se hubiera ido dentro del último dígito, las cajas saldrían
    # pegadas —una acaba justo donde empieza la otra— y la esquina del dígito
    # viajaría con los centavos.
    blanco = centavos["x"] - (entero["x"] + entero["width"])
    assert blanco >= 2, (
        f"el corte no cayó en el hueco: quedan {blanco}px entre las piezas, "
        "así que una se llevó parte de la otra"
    )

    client.post(
        f"/projects/{project_id}/layers/{entero['id']}/text", json={"content": "$39"}
    )
    despues = huecos()
    # Desde donde arrancan los centavos, el arte tiene que ser el de siempre: si
    # el corte se hubiera comido parte del dígito, aparecería un hueco de más.
    frontera = centavos["x"]
    assert parecidos(
        [h for h in despues if h[0] >= frontera],
        [h for h in antes if h[0] >= frontera],
    ), "quedó un resto del dígito viejo pegado a los centavos"


# ------------------------------------------------- fichas, filetes y planchas
# El arte real de Marcimex trae dos formas que el separador no sabía leer, y en
# las dos el resultado era el mismo: una capa de varios renglones contaba como
# una sola pieza, y reescribirla los reemplazaba por un renglón único.


def _ficha_con_filete():
    """Ficha de producto con la barra de color de la marca a la izquierda.

    La barra toca las filas de los tres renglones a la vez, así que la
    proyección de tinta los ve como uno solo de 112 px de alto.
    """
    azul = (12, 50, 75, 255)
    lineas = [
        (_ink("SIDE BY SIDE 476L", 40, azul), 0),
        (_ink("SBE-422 | 31541", 26, azul), 14),
        (_ink("Balcones transparentes", 26, azul), 26),
    ]
    ancho = max(pieza.width for pieza, _ in lineas) + 40
    alto = sum(pieza.height for pieza, _ in lineas) + sum(h for _, h in lineas)
    image = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    y = 0
    for pieza, hueco in lineas:
        y += hueco
        image.paste(pieza, (40, y), pieza)
        y += pieza.height
    # La barra, de lado a lado de las tres líneas.
    ImageDraw.Draw(image).rectangle([0, 0, 13, alto - 1], fill=(146, 214, 46, 255))
    return image


def _ficha_project(client: TestClient, tmp_path, image) -> dict:
    source = tmp_path / "ficha.psd"
    write_psd(
        source,
        (900, 600),
        [
            {
                "name": "Relleno de color 1",
                "image": Image.new("RGBA", (900, 600), (245, 245, 248, 255)),
                "position": (0, 0),
            },
            {"name": "ficha", "image": image, "position": (60, 120)},
        ],
    )
    response = client.post(
        "/projects",
        data={"name": "Ficha"},
        files={"artwork": ("ficha.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_colour_bar_does_not_weld_three_lines_into_one(client: TestClient, tmp_path):
    """La barra de marca cruza los tres renglones: no puede unirlos en uno.

    Soldados, el bloque se medía como una línea del alto de los tres y
    reescribirlo los reemplazaba por un renglón de 112 px.
    """
    created = _ficha_project(client, tmp_path, _ficha_con_filete())
    capa = next(layer for layer in created["layers"] if layer["name"] == "ficha")
    listed = client.get(f"/projects/{created['project_id']}/texts").json()
    ficha = next(item for item in listed["layers"] if item["id"] == capa["id"])
    # Tres renglones más la barra.
    assert ficha["pieces"] == 4, "la barra sigue soldando los renglones"


def test_the_last_letter_of_a_line_stays_with_its_line(client: TestClient, tmp_path):
    """La «L» de «476L» no es un volado: no se corta del renglón.

    Con los renglones soldados, la cola de la línea larga —columnas con tinta
    solo arriba— tenía la firma exacta de unos centavos en volado, y el corte
    se llevaba la última letra a una pieza suya.
    """
    created = _ficha_project(client, tmp_path, _ficha_con_filete())
    project_id = created["project_id"]
    capa = next(layer for layer in created["layers"] if layer["name"] == "ficha")
    separado = client.post(f"/projects/{project_id}/layers/{capa['id']}/split")
    assert separado.status_code == 200, separado.text
    partes = separado.json()["layers"]

    renglones = [parte for parte in partes if parte["width"] > 30]
    arriba = min(renglones, key=lambda parte: parte["y"])
    resto = [parte for parte in renglones if parte is not arriba]
    # Ninguna otra pieza empieza a la derecha de donde acaba el primer renglón:
    # eso era la «L» suelta.
    for parte in resto:
        assert parte["x"] < arriba["x"] + arriba["width"], (
            "una pieza quedó colgando al final del primer renglón: es su última letra"
        )


def test_a_list_of_data_is_split_line_by_line(client: TestClient, tmp_path):
    """Nombre, código y viñetas son datos distintos: uno por renglón.

    Van al mismo color y casi al mismo cuerpo, así que por cercanía salían en
    una pieza sola y cambiar una viñeta obligaba a reescribir las tres.
    """
    azul = (12, 50, 75, 255)
    lineas = [
        _ink("Balcones transparentes", 26, azul),
        _ink("Twist ice maker", 26, azul),
        _ink("Manija incorporada", 26, azul),
    ]
    # Interlínea apretada, como en el arte: el hueco es menor que el alto de
    # línea, así que por cercanía las tres caen en una pieza sola. Lo que las
    # separa es el largo, que va y viene en vez de llegar siempre al borde.
    hueco = 8
    ancho = max(pieza.width for pieza in lineas)
    alto = sum(pieza.height for pieza in lineas) + hueco * (len(lineas) - 1)
    image = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    y = 0
    for pieza in lineas:
        image.paste(pieza, (0, y), pieza)
        y += pieza.height + hueco

    created = _ficha_project(client, tmp_path, image)
    capa = next(layer for layer in created["layers"] if layer["name"] == "ficha")
    listed = client.get(f"/projects/{created['project_id']}/texts").json()
    ficha = next(item for item in listed["layers"] if item["id"] == capa["id"])
    assert ficha["pieces"] == 3, "las viñetas siguen saliendo en una sola pieza"


def _bloque_con_plancha():
    """Bloque de precio de retail: caja, sello y marco, con el texto encima.

    Llega del PSD como una mancha maciza. Sin leer sus planchas, la capa entera
    contaba como una pieza: reescribirla borraba la caja y escribía el precio
    nuevo del color de la caja, invisible sobre el arte.
    """
    crema = (245, 238, 218, 255)
    cian = (0, 178, 225, 255)
    morado = (107, 61, 190, 255)
    tinta = (15, 52, 75, 255)

    # Todo se coloca a partir de lo que mide cada texto: Docker no tiene las
    # mismas caras que macOS, y con medidas fijas el pie acababa pisando al
    # precio por un píxel. Pegados, son un solo tramo de tinta y la prueba
    # medía otra cosa en cada máquina.
    rotulo = _ink("12 CUOTAS", 30, (255, 255, 255, 255))
    precio = _ink("$43", 110, tinta)
    centavos = _ink(",99", 60, tinta)
    pie = _ink("MENSUALES", 32, tinta)

    margen, aire = 34, 18
    ancho = max(precio.width + 6 + centavos.width, pie.width, rotulo.width) + margen * 2
    sello_alto = rotulo.height + 40
    caja_arriba = sello_alto - 15
    alto = caja_arriba + aire + precio.height + aire + pie.height + aire + 20

    image = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    trazo = ImageDraw.Draw(image)
    trazo.rounded_rectangle(
        [margen * 2, 0, ancho - margen * 2, sello_alto], radius=10, fill=morado
    )
    trazo.rounded_rectangle([0, caja_arriba, ancho - 1, alto - 1], radius=18, fill=cian)
    trazo.rounded_rectangle(
        [14, caja_arriba + 17, ancho - 15, alto - 18], radius=10, fill=crema
    )

    image.paste(rotulo, ((ancho - rotulo.width) // 2, 20), rotulo)
    y = caja_arriba + aire
    image.paste(precio, (margen, y), precio)
    image.paste(centavos, (margen + precio.width + 6, y), centavos)
    y += precio.height + aire
    image.paste(pie, ((ancho - pie.width) // 2, y), pie)
    return image


@pytest.fixture()
def plate_project(client: TestClient, tmp_path) -> dict:
    source = tmp_path / "plancha.psd"
    write_psd(
        source,
        (900, 900),
        [
            {
                "name": "Relleno de color 1",
                "image": Image.new("RGBA", (900, 900), (255, 255, 255, 255)),
                "position": (0, 0),
            },
            {"name": "bloque", "image": _bloque_con_plancha(), "position": (200, 300)},
        ],
    )
    response = client.post(
        "/projects",
        data={"name": "Bloque con plancha"},
        files={"artwork": ("plancha.psd", source.read_bytes(), "image/vnd.adobe.photoshop")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_price_plate_is_read_as_background_plus_its_texts(
    client: TestClient, plate_project
):
    """La caja no es texto: los renglones que lleva encima, sí."""
    project_id = plate_project["project_id"]
    capa = next(layer for layer in plate_project["layers"] if layer["name"] == "bloque")
    separado = client.post(f"/projects/{project_id}/layers/{capa['id']}/split")
    assert separado.status_code == 200, separado.text
    partes = separado.json()["layers"]

    fondos = [parte for parte in partes if parte["meta"].get("art_piece") == "fondo"]
    assert len(fondos) == 1, "la caja, el sello y el marco son un fondo solo"
    # Rótulo, enteros, centavos y pie.
    assert len(partes) - len(fondos) == 4, [parte["name"] for parte in partes]

    listado = client.get(f"/projects/{project_id}/texts").json()["layers"]
    fondo = next(item for item in listado if item["id"] == fondos[0]["id"])
    assert not fondo["editable"], "el fondo no es un texto y no se reescribe"


def test_rewriting_the_price_keeps_its_plate_and_its_cents(
    client: TestClient, plate_project
):
    """Cambiar los enteros no toca la caja, el sello, el pie ni los centavos.

    Sin separar las planchas, reescribir este bloque lo dejaba en un renglón
    suelto del color de la caja y el diseño se perdía entero.
    """
    project_id = plate_project["project_id"]
    capa = next(layer for layer in plate_project["layers"] if layer["name"] == "bloque")
    separado = client.post(f"/projects/{project_id}/layers/{capa['id']}/split")
    assert separado.status_code == 200, separado.text
    partes = separado.json()["layers"]

    listado = client.get(f"/projects/{project_id}/texts").json()["layers"]
    editables = [item for item in listado if item["editable"]]
    enteros = max(editables, key=lambda item: item["style"]["ink_height"])
    # El color medido es el de la tinta, no el de la caja que hay detrás.
    assert enteros["style"]["color"].lower() != "#f5eeda"

    antes = Image.open(io.BytesIO(client.get(f"/projects/{project_id}/preview/template").content))
    response = client.post(
        f"/projects/{project_id}/layers/{enteros['id']}/text", json={"content": "$39"}
    )
    assert response.status_code == 200, response.text
    despues = Image.open(
        io.BytesIO(client.get(f"/projects/{project_id}/preview/template").content)
    )

    caja = next(parte for parte in partes if parte["id"] == enteros["id"])
    cambio = ImageChops.difference(antes.convert("RGB"), despues.convert("RGB")).getbbox()
    assert cambio is not None, "no cambió nada"
    izquierda, arriba, derecha, abajo = cambio
    # Lo que cambia vive dentro de la caja de los enteros, con margen para el
    # ancho del texto nuevo. Si la plancha se hubiera borrado, el cambio
    # abarcaría el bloque entero.
    assert arriba >= caja["y"] - 4 and abajo <= caja["y"] + caja["height"] + 4, (
        f"el cambio se salió de la línea del precio: {cambio}"
    )
    assert izquierda >= caja["x"] - 4, f"el cambio se fue a la izquierda: {cambio}"
