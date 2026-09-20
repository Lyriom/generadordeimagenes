"""Guardas mínimas de exposición HTTP para la API de campaña."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.worker import celery_app


def test_cors_no_acepta_origenes_arbitrarios(client: TestClient):
    response = client.options(
        "/health",
        headers={
            "Origin": "https://sitio-ajeno.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_cors_permite_el_frontend_local_de_desarrollo(client: TestClient):
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:8501",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") == "http://localhost:8501"


def test_respuestas_incluyen_nosniff(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"


def test_worker_reconoce_tarde_las_tandas_largas_y_rechaza_perdidas():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
