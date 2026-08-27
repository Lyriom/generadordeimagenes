"""Generación de variantes, formatos, determinismo, render y ZIP."""
from __future__ import annotations

import zipfile

from fastapi.testclient import TestClient
from PIL import Image

from app.services import storage
from tests.conftest import create_manual_layers

FORMATS = ["1080x1080", "1080x1350", "1080x1920"]


def _prepare(client: TestClient, project: dict) -> str:
    project_id = project["project_id"]
    create_manual_layers(client, project_id)
    response = client.post(f"/projects/{project_id}/reconstruct-background", json={})
    assert response.status_code == 200, response.text
    return project_id


def _generate(client: TestClient, project_id: str, **overrides) -> dict:
    payload = {
        "count": 12,
        "seed": 2024,
        "formats": FORMATS,
        "intensity": "creative",
    }
    payload.update(overrides)
    response = client.post(f"/projects/{project_id}/generate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_generate_twelve_variants_in_three_formats(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    body = _generate(client, project_id)
    variants = body["variants"]
    assert len(variants) == 12
    assert {variant["format"] for variant in variants} == set(FORMATS)
    # Al menos 6 familias de layout distintas entre 12 variantes.
    assert len({variant["layout"] for variant in variants}) >= 6

    for variant in variants:
        path = storage.abs_path(project_id, variant["image"])
        assert path.exists()
        with Image.open(path) as image:
            assert image.size == (variant["width"], variant["height"])
        assert 0 <= variant["quality"]["score"] <= 100
        assert isinstance(variant["quality"]["warnings"], list)
        assert variant["layout_label"]


def test_variants_change_product_but_keep_a_consistent_layer_order(
    client: TestClient, project: dict
):
    project_id = _prepare(client, project)
    variants = _generate(client, project_id)["variants"]

    square = [v for v in variants if v["format"] == "1080x1080"]
    assert len(square) >= 4

    def product_of(variant: dict) -> dict:
        return next(p for p in variant["placements"] if p["category"] == "product")

    positions = {(product_of(v)["x"], product_of(v)["y"]) for v in square}
    assert len(positions) >= 3, "El producto debe cambiar de sitio entre variantes"

    sizes = {(product_of(v)["width"], product_of(v)["height"]) for v in square}
    assert len(sizes) >= 2, "El producto debe cambiar de tamaño entre variantes"

    orders = {
        tuple(p["category"] for p in sorted(v["placements"], key=lambda item: item["z_index"]))
        for v in variants
    }
    # La variedad viene de posición y escala; variar arbitrariamente el z-index puede
    # poner el producto encima del logo, descuento o CTA.
    assert len(orders) == 1
    assert next(iter(orders))[-1] == "logo"


def test_locked_layers_keep_pixels_and_aspect_ratio(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    stored = client.get(f"/projects/{project_id}").json()
    locked = {
        layer["id"]: layer
        for layer in stored["layers"]
        if layer["locked"] and layer["type"] == "image"
    }
    assert locked, "El fixture debe crear capas bloqueadas"

    checksums_before = {
        layer_id: storage.abs_path(project_id, layer["src"]).read_bytes()
        for layer_id, layer in locked.items()
    }

    variants = _generate(client, project_id)["variants"]
    for variant in variants:
        for placement in variant["placements"]:
            layer = locked.get(placement["layer_id"])
            if layer is None:
                continue
            original = layer["width"] / layer["height"]
            rendered = placement["width"] / placement["height"]
            assert abs(original - rendered) / original < 0.03, placement

    # Los PNG de las capas bloqueadas no se regeneran ni se tocan.
    for layer_id, payload in checksums_before.items():
        current = storage.abs_path(project_id, locked[layer_id]["src"]).read_bytes()
        assert current == payload


def test_generation_is_deterministic_with_the_same_seed(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    first = _generate(client, project_id, seed=777)["variants"]
    second = _generate(client, project_id, seed=777)["variants"]

    def signature(variants: list[dict]) -> list:
        return [
            (
                variant["layout"],
                variant["format"],
                variant["background_style"],
                [
                    (p["layer_id"], p["x"], p["y"], p["width"], p["height"], p["z_index"])
                    for p in variant["placements"]
                ],
            )
            for variant in variants
        ]

    assert signature(first) == signature(second)
    assert [v["quality"]["score"] for v in first] == [v["quality"]["score"] for v in second]

    third = _generate(client, project_id, seed=778)["variants"]
    assert signature(third) != signature(first)


def test_intensity_conservative_moves_less_than_creative(client: TestClient, project: dict):
    project_id = _prepare(client, project)

    def spread(intensity: str) -> int:
        variants = _generate(client, project_id, intensity=intensity, count=6, seed=5)["variants"]
        squares = [v for v in variants if v["format"] == "1080x1080"]
        boxes = {
            tuple(
                (p["x"], p["y"])
                for p in sorted(v["placements"], key=lambda item: item["layer_id"])
            )
            for v in squares
        }
        return len(boxes)

    assert spread("creative") >= spread("conservative")


def test_variant_detail_and_download(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    variant = _generate(client, project_id, count=4)["variants"][0]

    detail = client.get(f"/projects/{project_id}/variants/{variant['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == variant["id"]

    download = client.get(
        f"/projects/{project_id}/variants/{variant['id']}", params={"download": True}
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"
    assert len(download.content) > 1000

    missing = client.get(f"/projects/{project_id}/variants/inexistente")
    assert missing.status_code == 404


def test_list_variants(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    _generate(client, project_id, count=4)
    response = client.get(f"/projects/{project_id}/variants")
    assert response.status_code == 200
    assert len(response.json()["variants"]) == 4


def test_batch_generation_appends_and_labels_each_product(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    first = _generate(
        client, project_id, count=4, product_label="Zapato azul", replace_existing=True
    )["variants"]
    second = _generate(
        client, project_id, count=4, product_label="Zapato rojo", replace_existing=False
    )["variants"]

    stored = client.get(f"/projects/{project_id}/variants").json()["variants"]
    assert len(first) == len(second) == 4
    assert len(stored) == 8
    assert {item["meta"]["product_label"] for item in stored} == {
        "Zapato azul",
        "Zapato rojo",
    }


def test_export_zip_contains_selected_variants(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    variants = _generate(client, project_id, count=4)["variants"]

    full = client.get(f"/projects/{project_id}/export")
    assert full.status_code == 200
    assert full.headers["content-type"] == "application/zip"

    archive_path = storage.abs_path(project_id, "exports")
    zips = list(archive_path.glob("*.zip"))
    assert zips
    with zipfile.ZipFile(zips[0]) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        assert sum(1 for name in names if name.endswith(".png")) == 4
        assert sum(1 for name in names if name.endswith(".psd")) == 4
        from psd_tools import PSDImage

        psd_name = next(name for name in names if name.endswith(".psd"))
        with archive.open(psd_name) as stream:
            layered = PSDImage.open(stream)
            assert layered.size == (1080, 1080)
            assert len(layered) >= 2

    selected = [variants[0]["id"], variants[2]["id"]]
    partial = client.get(
        f"/projects/{project_id}/export",
        params=[("variant_ids", value) for value in selected] + [("include_layers", "true")],
    )
    assert partial.status_code == 200
    with zipfile.ZipFile(zips[0]) as archive:
        names = archive.namelist()
        assert sum(1 for name in names if name.endswith(".png") and not name.startswith("capas/")) == 2
        assert any(name.startswith("capas/") for name in names)


def test_generate_without_layers_returns_422(client: TestClient, project: dict):
    project_id = project["project_id"]
    response = client.post(f"/projects/{project_id}/generate", json={"count": 4})
    assert response.status_code == 422
    assert "warnings" in response.json()["detail"]


def test_generate_rejects_invalid_format_and_count(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    assert (
        client.post(
            f"/projects/{project_id}/generate", json={"formats": ["800x600"], "count": 4}
        ).status_code
        == 422
    )
    assert (
        client.post(f"/projects/{project_id}/generate", json={"count": 0}).status_code == 422
    )
    assert (
        client.post(f"/projects/{project_id}/generate", json={"count": 40}).status_code == 422
    )


def test_hidden_and_locked_overrides_are_respected(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    stored = client.get(f"/projects/{project_id}").json()
    legal = next(layer for layer in stored["layers"] if layer["category"] == "legal")

    body = _generate(client, project_id, count=4, hidden_layers=[legal["id"]])
    for variant in body["variants"]:
        assert all(p["layer_id"] != legal["id"] for p in variant["placements"])


def test_instruction_influences_layout_choice(client: TestClient, project: dict):
    project_id = _prepare(client, project)
    body = _generate(
        client, project_id, count=6, instruction="producto grande y centrado", seed=31
    )
    layouts = {variant["layout"] for variant in body["variants"]}
    assert layouts & {"hero_product_overlay", "product_center_headline_top", "vertical_stack"}
