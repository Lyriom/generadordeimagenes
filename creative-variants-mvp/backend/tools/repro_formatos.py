"""Reproduce la queja: un KV llevado a otra proporción sale con bandas."""
import os, pathlib, shutil

SALIDA = pathlib.Path("/tmp/repro-formatos")
os.environ["DATA_DIR"] = str(SALIDA / "data")
os.environ["INGEST_DIR"] = str(SALIDA / "data/ingest")
os.environ["ENABLE_OCR"] = "false"
os.environ["SEGMENTATION_PROVIDER"] = "local"
os.environ["INPAINTING_PROVIDER"] = "opencv"
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from fastapi.testclient import TestClient
from app.main import app

PSD = pathlib.Path("../data/ingest/CYBER-AGOSTO-2026CYBER-MS-900X660-ACCESORIOS.psd")

with TestClient(app) as client:
    cabecera = {"X-Session-Id": "repro"}
    creado = client.post(
        "/projects",
        data={"name": "repro"},
        files={"artwork": (PSD.name, PSD.read_bytes(), "image/vnd.adobe.photoshop")},
        headers=cabecera,
    ).json()
    pid = creado["project_id"]
    formatos = ["900x660", "meta_feed_square", "meta_feed_4_5", "meta_stories"]
    tarea = client.post(
        f"/projects/{pid}/auto",
        json={"count": 1, "formats": formatos, "template_mode": True},
        headers=cabecera,
    ).json()
    resultado = client.get(
        f"/projects/{pid}/tasks/{tarea['task_id']}", headers=cabecera
    ).json()["result"]
    piezas = SALIDA / "piezas"
    piezas.mkdir(parents=True, exist_ok=True)
    for v in resultado["variants"]:
        origen = SALIDA / "data" / "projects" / pid / v["image"]
        shutil.copy(origen, piezas / f"{v['format']}_{v['layout']}_{v['quality']['score']}.png")
        print(f"{v['format']:>18} {v['width']}x{v['height']} {v['layout']:<12} {v['quality']['score']}")
        for w in v['quality']['warnings'][:3]:
            print(f"      ⚠ {w[:130]}")
