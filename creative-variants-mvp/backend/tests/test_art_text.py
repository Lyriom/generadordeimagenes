"""Reescribir el copy del arte y quitar elementos (logos, sellos) de la pieza.

Un KV llega con el precio y el logo como píxeles. Estas pruebas cubren lo que hace
falta para producir la fila de artes de una promoción: cambiar ese texto sin mover
el diseño, quitar una marca del arte sin dejarla fantasma en el fondo, y que cada
tanda de un catálogo escriba su propio copy sin heredar el del producto anterior.
"""
from __future__ import annotations

import pathlib

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

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
    """Mover el texto en Ajustes finos y luego reescribirlo no debe devolverlo de un salto."""
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
