"""Cambiar el producto de un KV conservando el resto del diseño."""
from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from app.services import storage

from .conftest import create_manual_layers


def cutout(width: int, height: int, *, alpha: bool = True, margin: int = 0) -> bytes:
    """Recorte sintético: forma opaca centrada sobre transparencia."""
    mode = "RGBA" if alpha else "RGB"
    base = (0, 0, 0, 0) if alpha else (255, 255, 255)
    image = Image.new(mode, (width, height), base)
    fill = (10, 120, 200, 255) if alpha else (10, 120, 200)
    image.paste(
        Image.new(mode, (width - 2 * margin, height - 2 * margin), fill), (margin, margin)
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_replaceable_layers_lists_product_first(client: TestClient, project: dict):
    create_manual_layers(client, project["project_id"])
    response = client.get(f"/projects/{project['project_id']}/layers/replaceable")
    assert response.status_code == 200, response.text
    layers = response.json()["layers"]

    assert [layer["category"] for layer in layers] == ["product", "logo"]
    assert 0 < layers[0]["area_ratio"] <= 1
    # Las capas de texto no se pueden reemplazar por una imagen.
    assert all(layer["category"] not in {"headline", "cta", "legal"} for layer in layers)


def test_replace_product_fits_box_without_deforming(client: TestClient, project: dict):
    layers = create_manual_layers(client, project["project_id"])
    original = layers["product"]

    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        data={"layer_id": original["id"]},
        files={"image": ("producto.png", cutout(800, 400), "image/png")},
    )
    assert response.status_code == 200, response.text
    layer = response.json()["layer"]

    # Cabe dentro de la caja original y conserva la proporción 2:1 del recorte.
    assert layer["width"] <= original["width"] and layer["height"] <= original["height"]
    assert abs(layer["width"] / layer["height"] - 2.0) < 0.05
    # Queda centrado en la caja que ocupaba el producto viejo.
    old_cx = original["x"] + original["width"] / 2
    new_cx = layer["x"] + layer["width"] / 2
    assert abs(old_cx - new_cx) <= 1
    assert layer["src"] and layer["src"] != original["src"]
    assert layer["meta"]["original_src"] == original["src"]
    # La máscara no se reescribe: sigue describiendo el hueco del producto viejo.
    assert layer["meta"]["mask_edited"] is True


def test_replace_crops_empty_margin_so_product_is_not_tiny(
    client: TestClient, project: dict
):
    """Un PNG con mucho aire alrededor no debe quedar diminuto en el KV."""
    layers = create_manual_layers(client, project["project_id"])
    box = layers["product"]
    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        data={"layer_id": box["id"]},
        files={"image": ("aire.png", cutout(1000, 1000, margin=350), "image/png")},
    )
    assert response.status_code == 200, response.text
    layer = response.json()["layer"]
    # El contenido real es 300x300: debe escalarse hasta llenar la caja, no quedarse en 300.
    assert layer["width"] >= box["width"] * 0.9


def test_repeated_replacements_keep_the_original_kv_slot(client: TestClient, project: dict):
    layers = create_manual_layers(client, project["project_id"])
    original = layers["product"]
    endpoint = f"/projects/{project['project_id']}/layers/replace"

    first = client.post(
        endpoint,
        data={"layer_id": original["id"]},
        files={"image": ("vertical.png", cutout(100, 800), "image/png")},
    ).json()["layer"]
    second = client.post(
        endpoint,
        data={"layer_id": original["id"]},
        files={"image": ("horizontal.png", cutout(800, 100), "image/png")},
    ).json()["layer"]

    assert first["meta"]["replacement_box"] == [
        original["x"], original["y"], original["width"], original["height"]
    ]
    assert second["width"] == original["width"]


def test_replace_without_layer_id_uses_largest_product(client: TestClient, project: dict):
    layers = create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        files={"image": ("p.png", cutout(500, 500), "image/png")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["layer"]["id"] == layers["product"]["id"]


def test_replace_hide_others_and_warns_without_alpha(client: TestClient, project: dict):
    create_manual_layers(client, project["project_id"])
    # Segundo producto para comprobar el ocultado.
    second = client.post(
        f"/projects/{project['project_id']}/layers",
        json={
            "name": "Producto 2",
            "category": "product",
            "type": "image",
            "x": 60,
            "y": 400,
            "width": 120,
            "height": 120,
            "auto_segment": False,
        },
    ).json()

    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        data={"hide_others": "true"},
        files={"image": ("plano.png", cutout(400, 400, alpha=False), "image/png")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert any("no tiene transparencia" in w for w in payload["warnings"])
    assert any("ocultaron" in w for w in payload["warnings"])

    stored = client.get(f"/projects/{project['project_id']}").json()
    other = next(layer for layer in stored["layers"] if layer["id"] == second["id"])
    assert other["visible"] is False


def test_replace_hides_adjacent_generic_psd_product_layers(
    client: TestClient, project: dict
):
    """Un balón llamado ``Capa 15`` no debe sobrevivir como decoración del KV."""
    layers = create_manual_layers(client, project["project_id"])
    generic = client.post(
        f"/projects/{project['project_id']}/layers",
        json={
            "name": "Capa 15",
            "category": "decoration",
            "type": "image",
            "x": 425,
            "y": 280,
            "width": 120,
            "height": 120,
            "auto_segment": False,
        },
    ).json()
    stored = storage.load_project(project["project_id"])
    stored.layer_by_id(generic["id"]).meta["psd_name"] = "Capa 15"
    storage.save_project(stored)

    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        data={"layer_id": layers["product"]["id"], "hide_others": "true"},
        files={"image": ("nuevo.png", cutout(300, 500), "image/png")},
    )
    assert response.status_code == 200, response.text
    replaced = response.json()["layer"]
    refreshed = client.get(f"/projects/{project['project_id']}").json()
    old_subject = next(item for item in refreshed["layers"] if item["id"] == generic["id"])

    assert old_subject["visible"] is False
    # El hueco se amplía para ocupar el conjunto original, no solo la primera capa.
    assert replaced["meta"]["replacement_box"][2] > layers["product"]["width"]


def test_append_keeps_multiple_uploaded_products_as_separate_layers(
    client: TestClient, project: dict
):
    layers = create_manual_layers(client, project["project_id"])
    endpoint = f"/projects/{project['project_id']}/layers/replace"
    first = client.post(
        endpoint,
        data={"layer_id": layers["product"]["id"], "hide_others": "true"},
        files={"image": ("uno.png", cutout(300, 500), "image/png")},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        endpoint,
        data={"layer_id": layers["product"]["id"], "append": "true"},
        files={"image": ("dos.png", cutout(500, 300), "image/png")},
    )
    assert second.status_code == 200, second.text
    stored = client.get(f"/projects/{project['project_id']}").json()
    products = [item for item in stored["layers"] if item["category"] == "product"]
    assert len(products) == 2
    assert sum(bool(item["meta"].get("external")) for item in products) == 1
    assert products[0]["src"] != products[1]["src"]


def test_product_combination_metadata_is_kept_on_every_member(
    client: TestClient, project: dict
):
    layers = create_manual_layers(client, project["project_id"])
    endpoint = f"/projects/{project['project_id']}/layers/replace"
    common = {
        "layer_id": layers["product"]["id"],
        "group_id": "looks-verano",
        "group_name": "Looks verano",
        "arrangement": "overlap",
    }
    first = client.post(
        endpoint,
        data={**common, "hide_others": "true"},
        files={"image": ("uno.png", cutout(300, 500), "image/png")},
    )
    second = client.post(
        endpoint,
        data={**common, "append": "true"},
        files={"image": ("dos.png", cutout(500, 300), "image/png")},
    )
    assert first.status_code == second.status_code == 200
    stored = client.get(f"/projects/{project['project_id']}").json()
    products = [item for item in stored["layers"] if item["category"] == "product"]
    assert len(products) == 2
    assert {item["meta"]["product_group_id"] for item in products} == {"looks-verano"}
    assert {item["meta"]["product_arrangement"] for item in products} == {"overlap"}


def test_replace_rejects_non_image(client: TestClient, project: dict):
    create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        files={"image": ("x.png", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/png")},
    )
    assert response.status_code == 400
    assert "SVG" in response.json()["detail"]


def test_replace_rejects_unknown_layer(client: TestClient, project: dict):
    response = client.post(
        f"/projects/{project['project_id']}/layers/replace",
        data={"layer_id": "no-existe"},
        files={"image": ("p.png", cutout(300, 300), "image/png")},
    )
    assert response.status_code == 400
