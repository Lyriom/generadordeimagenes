"""Modo automático: una sola llamada debe dejar variantes listas."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Canvas, Project, SourceImage
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
    # Un banner 1200x400 debe generar en su propia proporción antes que en cuadrado.
    formats = autopilot.auto_formats(_project_with_canvas(1200, 400))
    assert formats[0] == "1200x400"
    assert "1080x1080" in formats and "1080x1350" in formats

    vertical = autopilot.auto_formats(_project_with_canvas(1080, 1920))
    assert vertical[0] == "1080x1920"
    # Sin repetir el nativo entre los formatos de redes.
    assert len(vertical) == len(set(vertical))


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
