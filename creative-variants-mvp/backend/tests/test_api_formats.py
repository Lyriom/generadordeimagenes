"""El endpoint que contesta qué entra en cada formato, antes de generar."""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import create_manual_layers


def test_el_endpoint_dice_que_entra_en_cada_formato(client: TestClient, project: dict):
    create_manual_layers(client, project["project_id"])
    respuesta = client.get(f"/projects/{project['project_id']}/formats")
    assert respuesta.status_code == 200, respuesta.text
    payload = respuesta.json()
    assert payload["project_id"] == project["project_id"]
    formatos = {item["id"]: item for item in payload["formats"]}
    assert "google_display_320x50" in formatos
    for item in payload["formats"]:
        assert item["width"] > 0 and item["height"] > 0
        assert 0 <= item["fits"] <= item["total"]
        assert len(item["dropped"]) == item["total"] - item["fits"]


def test_un_proyecto_que_no_existe_da_404(client: TestClient):
    respuesta = client.get("/projects/00000000-0000-4000-8000-000000000999/formats")
    assert respuesta.status_code == 404
