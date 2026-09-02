"""Sesiones y retención: el trabajo no se queda en el servidor.

Un PSD de 100 MB deja cientos de MB entre capas, máscaras, fondos y variantes.
Sin barrido el disco se llena y el sitio se cae, así que aquí se fija el
contrato: el trabajo se etiqueta con la sesión del navegador, cada sesión solo
ve el suyo, y el barrido borra por antigüedad y nunca por sesión —para que
abrir la página no destruya la campaña que otra persona está produciendo—.
"""
from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

from app.services import storage


def _create(client: TestClient, artwork: bytes, name: str, session: str | None = None) -> dict:
    headers = {"X-Session-Id": session} if session else {}
    response = client.post(
        "/projects",
        data={"name": name},
        files={"artwork": ("arte.png", artwork, "image/png")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wipe(client: TestClient) -> None:
    client.delete("/projects")


def test_project_queda_etiquetado_con_la_sesion(client: TestClient, artwork_png: bytes):
    _wipe(client)
    created = _create(client, artwork_png, "Con sesión", session="sesion-uno")
    assert created["meta"]["session_id"] == "sesion-uno"

    # Releído de disco: la etiqueta se persistió, no vive solo en la respuesta.
    stored = storage.load_project(created["project_id"])
    assert stored.meta["session_id"] == "sesion-uno"


def test_sin_cabecera_no_rompe_y_no_etiqueta(client: TestClient, artwork_png: bytes):
    _wipe(client)
    created = _create(client, artwork_png, "Sin sesión")
    assert created["meta"].get("session_id") is None


def test_cada_sesion_solo_ve_lo_suyo(client: TestClient, artwork_png: bytes):
    _wipe(client)
    _create(client, artwork_png, "De Ana", session="ana")
    _create(client, artwork_png, "De Ana otro", session="ana")
    _create(client, artwork_png, "De Beto", session="beto")
    _create(client, artwork_png, "Huérfano")

    de_ana = client.get("/projects", params={"session": "ana"}).json()
    de_beto = client.get("/projects", params={"session": "beto"}).json()
    de_nadie = client.get("/projects", params={"session": "sesion-que-no-existe"}).json()
    todo = client.get("/projects").json()

    assert sorted(item["name"] for item in de_ana) == ["De Ana", "De Ana otro"]
    assert [item["name"] for item in de_beto] == ["De Beto"]
    assert de_nadie == []
    # Sin filtro se ve todo, incluido el que llegó sin cabecera.
    assert len(todo) == 4


def test_la_sesion_no_puede_pasar_de_64_caracteres(client: TestClient):
    response = client.get("/projects", headers={"X-Session-Id": "x" * 200})
    assert response.status_code == 422


def test_purge_no_borra_el_trabajo_de_otra_sesion(client: TestClient, artwork_png: bytes):
    """La propiedad de seguridad: barrer desde una sesión no toca a las demás."""
    _wipe(client)
    _create(client, artwork_png, "De Ana", session="ana")
    _create(client, artwork_png, "De Beto", session="beto")

    response = client.post("/projects/purge", headers={"X-Session-Id": "ana"})
    assert response.status_code == 200
    assert response.json()["removed_count"] == 0

    assert len(client.get("/projects", params={"session": "ana"}).json()) == 1
    assert len(client.get("/projects", params={"session": "beto"}).json()) == 1


def test_purge_borra_por_antiguedad(client: TestClient, artwork_png: bytes):
    _wipe(client)
    viejo = _create(client, artwork_png, "Abandonado", session="ayer")
    reciente = _create(client, artwork_png, "En curso", session="hoy")

    # Se envejece el manifiesto, que es lo que mira el barrido.
    manifest = storage.project_json_path(viejo["project_id"])
    hace_tres_horas = time.time() - 3 * 3600
    os.utime(manifest, (hace_tres_horas, hace_tres_horas))

    removed = storage.purge_expired_projects(retention_hours=1, max_kept=100)

    assert viejo["project_id"] in removed
    assert reciente["project_id"] not in removed
    assert storage.project_dir(viejo["project_id"]).exists() is False
    assert storage.project_dir(reciente["project_id"]).exists() is True


def test_purge_respeta_el_tope_por_cantidad(client: TestClient, artwork_png: bytes):
    """Red de seguridad para muchas campañas dentro de la ventana de retención."""
    _wipe(client)
    for index in range(4):
        _create(client, artwork_png, f"KV {index}", session="misma")

    # Retención desactivada: solo debe actuar el tope.
    removed = storage.purge_expired_projects(retention_hours=0, max_kept=2)

    assert len(removed) == 2
    assert len(client.get("/projects").json()) == 2


def test_retencion_cero_no_borra_nada_por_tiempo(client: TestClient, artwork_png: bytes):
    """`PROJECT_RETENTION_HOURS=0` desactiva el criterio de antigüedad."""
    _wipe(client)
    creado = _create(client, artwork_png, "Intacto", session="s")
    manifest = storage.project_json_path(creado["project_id"])
    muy_viejo = time.time() - 400 * 3600
    os.utime(manifest, (muy_viejo, muy_viejo))

    removed = storage.purge_expired_projects(retention_hours=0, max_kept=100)

    assert removed == []
    assert storage.project_dir(creado["project_id"]).exists() is True


def test_borrar_todo_vacia_el_disco(client: TestClient, artwork_png: bytes):
    _wipe(client)
    _create(client, artwork_png, "Uno", session="a")
    _create(client, artwork_png, "Dos", session="b")

    response = client.delete("/projects")
    assert response.status_code == 200
    body = response.json()

    assert body["removed_count"] == 2
    assert body["disk_usage_mb"] == 0.0
    assert client.get("/projects").json() == []


def test_purge_informa_del_disco_ocupado(client: TestClient, artwork_png: bytes):
    _wipe(client)
    _create(client, artwork_png, "Ocupa algo", session="s")

    body = client.post("/projects/purge").json()

    assert body["retention_hours"] >= 0
    assert body["remaining"] == 1
    # El proyecto ocupa algo en disco, así que la medida no puede ser negativa.
    assert body["disk_usage_mb"] >= 0.0
