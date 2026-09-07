"""Modo automático: una sola llamada debe dejar variantes listas."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.models.schemas import SUPPORTED_FORMATS
from app.services import autopilot

from .conftest import await_task, create_manual_layers


def _project_with_canvas(width: int, height: int) -> Project:
    return Project(
        project_id="00000000-0000-4000-8000-000000000000",
        name="test",
        canvas=Canvas(width=width, height=height),
        source=SourceImage(
            path="original/a.png",
            width=width,
            height=height,
            format="PNG",
            original_filename="a.png",
            bytes=1,
        ),
    )


def test_auto_formats_uses_native_aspect_first():
    # Un banner 1200x400 debe generar en su propia proporción antes que nada.
    formats = autopilot.auto_formats(_project_with_canvas(1200, 400))
    assert formats[0] == "1200x400"

    vertical = autopilot.auto_formats(_project_with_canvas(1080, 1920))
    assert vertical[0] == "1080x1920"
    # Sin repetir el nativo entre los formatos de redes.
    assert len(vertical) == len(set(vertical))


def test_un_arte_plano_no_promete_formatos_que_no_puede_llenar():
    """Un banner sin capas metido en un cuadrado es el arte al 33% y el resto relleno.

    Es lo que producía doce piezas inservibles de un KV de 1920x325: se ofrecían
    los formatos de redes pasara lo que pasara.
    """
    banner = autopilot.auto_formats(_project_with_canvas(1920, 325))
    assert banner == ["970x250"] or all(
        SUPPORTED_FORMATS[fmt][0] / SUPPORTED_FORMATS[fmt][1] > 2.5 for fmt in banner
    )
    assert "1080x1350" not in banner and "1080x1080" not in banner


def test_un_arte_por_capas_si_puede_recomponerse_a_cualquier_proporcion():
    """Con capas el motor recoloca, así que no hay proporción prohibida."""
    project = _project_with_canvas(1920, 325)
    project.layers = [
        Layer(name=f"Capa {i}", type=LayerType.IMAGE, category=LayerCategory.PRODUCT,
              src=f"layers/{i}.png", width=200, height=200)
        for i in range(3)
    ]
    formats = autopilot.auto_formats(project)
    assert "1080x1080" in formats and "1080x1350" in formats


def test_auto_generates_from_scratch(client: TestClient, project: dict):
    """Sin análisis previo ni capas: el endpoint debe hacerlo todo."""
    response = client.post(
        f"/projects/{project['project_id']}/auto", json={"count": 4}
    )
    payload = await_task(client, project["project_id"], response)

    names = [step["name"] for step in payload["steps"]]
    assert names == [
        "Detectar elementos",
        "Recortar elementos",
        "Preparar el fondo",
        "Componer variantes",
    ]
    assert len(payload["variants"]) == 4
    # El fondo quedó reconstruido y las variantes tienen imagen en disco.
    stored = client.get(f"/projects/{project['project_id']}").json()
    assert stored["background"]["path"]
    for variant in payload["variants"]:
        assert client.get(
            f"/projects/{project['project_id']}/files/{variant['image']}"
        ).status_code == 200


def test_auto_respects_explicit_formats_and_reuses_layers(
    client: TestClient, project: dict
):
    """Con capas ya listas no se vuelve a detectar, y se honran los formatos pedidos."""
    create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"count": 6, "formats": ["1080x1350"], "intensity": "conservative"},
    )
    payload = await_task(client, project["project_id"], response)

    detect = next(step for step in payload["steps"] if step["name"] == "Detectar elementos")
    assert "ya estaban listos" in detect["detail"]
    assert {variant["format"] for variant in payload["variants"]} == {"1080x1350"}
    assert all(variant["width"] == 1080 for variant in payload["variants"])


def test_auto_returns_every_selected_format_even_when_count_is_lower(
    client: TestClient, project: dict
):
    """Cada medida elegida debe producir al menos una salida visible."""
    create_manual_layers(client, project["project_id"])
    requested = [
        "meta_feed_4_5",
        "google_search_landscape",
        "youtube_video_vertical",
    ]
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"count": 2, "formats": requested, "intensity": "conservative"},
    )
    payload = await_task(client, project["project_id"], response)

    returned = {variant["format"] for variant in payload["variants"]}
    assert returned == set(requested)
    assert len(payload["variants"]) >= len(requested)


def test_template_mode_only_replaces_at_native_size_without_new_layouts(
    client: TestClient, project: dict
):
    """El flujo de catálogo entrega un arte fiel, no propuestas recompuestas."""
    create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={
            "count": 6,
            "formats": ["1080x1350"],
            "intensity": "creative",
            "instruction": "mover todo y cambiar el fondo",
            "template_mode": True,
            "regenerate_background": True,
        },
    )
    payload = await_task(client, project["project_id"], response)

    assert len(payload["variants"]) == 1
    variant = payload["variants"][0]
    assert variant["layout"] == "faithful"
    assert variant["format"] == "1080x1080"
    assert variant["intensity"] == "conservative"


def test_auto_rejects_unknown_format(client: TestClient, project: dict):
    response = client.post(
        f"/projects/{project['project_id']}/auto", json={"formats": ["5000x5000"]}
    )
    assert response.status_code == 422


def test_auto_rejects_unknown_format_mixed_with_valid_one(
    client: TestClient, project: dict
):
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"formats": ["meta_feed_4_5", "formato_inventado"]},
    )
    assert response.status_code == 422
