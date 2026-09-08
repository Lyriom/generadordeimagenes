"""Capas: creación manual, máscaras, extracción PNG y edición de comportamiento."""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app.services import storage
from tests.conftest import create_manual_layers


def test_analyze_returns_layers_and_warnings(client: TestClient, project: dict):
    project_id = project["project_id"]
    response = client.post(f"/projects/{project_id}/analyze", json={"run_ocr": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(layer["category"] == "background" for layer in body["layers"])
    # Sin PaddleOCR instalado debe advertir con claridad, no fallar.
    assert body["warnings"]
    assert any("aproximada" in warning for warning in body["warnings"])


def test_create_manual_layer_and_extract_transparent_png(client: TestClient, project: dict):
    project_id = project["project_id"]
    response = client.post(
        f"/projects/{project_id}/layers",
        json={
            "name": "Producto",
            "category": "product",
            "type": "image",
            "x": 180,
            "y": 180,
            "width": 240,
            "height": 250,
            "locked": True,
            "auto_segment": True,
        },
    )
    assert response.status_code == 201, response.text
    layer = response.json()
    assert layer["extracted"] is True
    assert layer["src"].startswith("layers/")
    assert layer["mask"].startswith("masks/")

    png_path = storage.abs_path(project_id, layer["src"])
    with Image.open(png_path) as image:
        assert image.mode == "RGBA"
        alpha = np.asarray(image.split()[-1])
    # Transparencia real: hay píxeles opacos y transparentes.
    assert alpha.max() > 200
    assert alpha.min() < 60

    # La máscara se guarda a tamaño del lienzo original.
    with Image.open(storage.abs_path(project_id, layer["mask"])) as mask:
        assert mask.size == (600, 600)


def test_extracted_layer_keeps_aspect_ratio(client: TestClient, project: dict):
    project_id = project["project_id"]
    layers = create_manual_layers(client, project_id)
    product = layers["product"]

    reloaded = client.get(f"/projects/{project_id}").json()
    stored = next(item for item in reloaded["layers"] if item["id"] == product["id"])
    with Image.open(storage.abs_path(project_id, stored["src"])) as image:
        png_w, png_h = image.size

    # El PNG conserva exactamente el bounding box declarado (sin reescalar).
    assert (png_w, png_h) == (stored["width"], stored["height"])
    assert abs((png_w / png_h) - (stored["width"] / stored["height"])) < 1e-6


def test_extract_endpoint_reports_skipped(client: TestClient, project: dict):
    project_id = project["project_id"]
    create_manual_layers(client, project_id)
    response = client.post(f"/projects/{project_id}/extract", json={"force": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extracted"]
    # Las capas de texto se rasterizan como referencia; ninguna capa rompe el flujo.
    assert isinstance(body["skipped"], list)


def test_mask_edit_add_and_subtract(client: TestClient, project: dict):
    project_id = project["project_id"]
    layers = create_manual_layers(client, project_id)
    product = layers["product"]

    def mask_area(layer: dict) -> int:
        with Image.open(storage.abs_path(project_id, layer["mask"])) as mask:
            return int((np.asarray(mask.convert("L")) > 127).sum())

    before = mask_area(product)
    response = client.post(
        f"/projects/{project_id}/layers/mask",
        json={
            "layer_id": product["id"],
            "reset_from_box": True,
            "operations": [
                {"op": "add", "shape": "rect", "x": 100, "y": 100, "width": 80, "height": 80}
            ],
            "re_extract": True,
        },
    )
    assert response.status_code == 200, response.text
    after_add = mask_area(response.json())
    assert after_add > before

    response = client.post(
        f"/projects/{project_id}/layers/mask",
        json={
            "layer_id": product["id"],
            "operations": [
                {"op": "subtract", "shape": "ellipse", "x": 200, "y": 200, "width": 120, "height": 120}
            ],
            "re_extract": True,
        },
    )
    assert response.status_code == 200
    assert mask_area(response.json()) < after_add


def test_update_layers_category_order_and_flags(client: TestClient, project: dict):
    project_id = project["project_id"]
    layers = create_manual_layers(client, project_id)
    headline = layers["headline"]
    cta = layers["cta"]

    response = client.put(
        f"/projects/{project_id}/layers",
        json={
            "updates": [
                {
                    "id": headline["id"],
                    "category": "subheadline",
                    "content": "Texto corregido",
                    "movable": False,
                    "locked": True,
                    "color": "#123456",
                },
                {"id": cta["id"], "resizable": False, "reorderable": False},
            ],
            "order": [cta["id"], headline["id"]],
        },
    )
    assert response.status_code == 200, response.text
    updated = {item["id"]: item for item in response.json()["layers"]}
    assert updated[headline["id"]]["category"] == "subheadline"
    assert updated[headline["id"]]["content"] == "Texto corregido"
    assert updated[headline["id"]]["movable"] is False
    assert updated[headline["id"]]["locked"] is True
    assert updated[headline["id"]]["color"] == "#123456"
    assert updated[cta["id"]]["resizable"] is False
    # El orden solicitado se refleja en z_index.
    assert updated[cta["id"]]["z_index"] == 0
    assert updated[headline["id"]]["z_index"] == 1


def test_delete_layer(client: TestClient, project: dict):
    project_id = project["project_id"]
    layers = create_manual_layers(client, project_id)
    target = layers["legal"]["id"]
    response = client.put(
        f"/projects/{project_id}/layers", json={"delete": [target]}
    )
    assert response.status_code == 200
    assert all(layer["id"] != target for layer in response.json()["layers"])


def test_background_reconstruction(client: TestClient, project: dict):
    project_id = project["project_id"]
    create_manual_layers(client, project_id)
    response = client.post(
        f"/projects/{project_id}/reconstruct-background", json={"dilate": 4}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "opencv"
    background = storage.abs_path(project_id, body["background"])
    assert background.exists()

    with Image.open(background) as image:
        assert image.size == (600, 600)
        arr = np.asarray(image.convert("RGB"), dtype=np.int16)

    with Image.open(storage.abs_path(project_id, project["source"]["path"])) as original:
        original_arr = np.asarray(original.convert("RGB"), dtype=np.int16)

    # El fondo reconstruido difiere del original en la zona del producto.
    region = (slice(200, 400), slice(200, 400))
    assert np.abs(arr[region] - original_arr[region]).mean() > 5


def test_preview_endpoints(client: TestClient, project: dict):
    project_id = project["project_id"]
    layers = create_manual_layers(client, project_id)
    detections = client.get(f"/projects/{project_id}/preview/detections")
    assert detections.status_code == 200
    assert detections.headers["content-type"] == "image/png"

    mask_preview = client.get(
        f"/projects/{project_id}/preview/mask/{layers['product']['id']}"
    )
    assert mask_preview.status_code == 200

    missing = client.get(f"/projects/{project_id}/preview/mask/no-existe")
    assert missing.status_code == 404


def test_file_serving_blocks_path_traversal(client: TestClient, project: dict):
    project_id = project["project_id"]
    ok = client.get(f"/projects/{project_id}/files/{project['source']['path']}")
    assert ok.status_code == 200

    for evil in ("../../../etc/passwd", "..%2f..%2fproject.json", "/etc/passwd"):
        response = client.get(f"/projects/{project_id}/files/{evil}")
        assert response.status_code in (400, 404), evil


def test_no_se_queda_dos_veces_con_el_mismo_objeto(monkeypatch, client, project):
    """El detector por contraste propone recuadros anidados sobre una misma cosa.

    Sin suprimirlos, al recomponer el arte salía el mismo bloque repetido en la
    pieza: es lo que pasaba al separar un banner aplanado.
    """
    from app.models import Project as ProjectModel
    from app.providers.base import Detection
    from app.services import analysis, storage
    from app.services import segmentation as seg_service

    propuestas = [
        Detection(x=100, y=100, width=200, height=160, score=0.8),
        Detection(x=110, y=108, width=180, height=140, score=0.7),  # la misma, anidada
        Detection(x=380, y=120, width=140, height=150, score=0.6),  # otra cosa
    ]
    monkeypatch.setattr(seg_service, "detect_regions", lambda *a, **k: (propuestas, []))
    monkeypatch.setattr(seg_service, "refine_box", lambda *a, **k: None)
    monkeypatch.setattr(analysis, "detect_faces", lambda *a, **k: [])

    cargado: ProjectModel = storage.load_project(project["project_id"])
    layers, *_ = analysis.analyze_project(
        cargado, run_segmentation=True, run_ocr=False, max_regions=12, extract=False
    )
    cajas = [(l.x, l.y, l.width, l.height) for l in layers if l.type.value == "image"]
    assert (110, 108, 180, 140) not in cajas, "se quedó con la caja anidada"
    assert (100, 100, 200, 160) in cajas
    assert (380, 120, 140, 150) in cajas


def _separar(monkeypatch, client, project, region, textos):
    """Corre la separación con una región y unos textos dados. Devuelve (capas, avisos)."""
    from app.models import Project as ProjectModel
    from app.providers.base import Detection, OcrResult, TextRegion
    from app.services import analysis, ocr as ocr_service, storage
    from app.services import segmentation as seg_service

    monkeypatch.setattr(
        seg_service, "detect_regions",
        lambda *a, **k: ([Detection(x=region[0], y=region[1], width=region[2],
                                    height=region[3], score=0.9)], []),
    )
    monkeypatch.setattr(seg_service, "refine_box", lambda *a, **k: None)
    monkeypatch.setattr(analysis, "detect_faces", lambda *a, **k: [])
    monkeypatch.setattr(
        ocr_service, "run_ocr",
        lambda *a, **k: OcrResult(
            regions=[TextRegion(text=t, x=x, y=y, width=w, height=h)
                     for t, x, y, w, h in textos],
            provider="falso",
        ),
    )
    cargado: ProjectModel = storage.load_project(project["project_id"])
    layers, warnings, *_ = analysis.analyze_project(
        cargado, run_segmentation=True, run_ocr=True, max_regions=12, extract=False
    )
    return layers, warnings


def test_una_plancha_de_copy_se_queda_como_texto_editable(monkeypatch, client, project):
    """El bloque azul con el titular dentro: el contenido son las palabras."""
    layers, _ = _separar(
        monkeypatch, client, project,
        region=(40, 40, 300, 200),
        textos=[("ESPECIAL DE ELECTROMENORES", 60, 90, 260, 60)],
    )
    textos = [l.content for l in layers if l.type.value == "text"]
    imagenes = [(l.x, l.y, l.width, l.height) for l in layers if l.type.value == "image"]
    assert any("ELECTROMENORES" in (t or "") for t in textos)
    # Y el rectángulo de color no se queda además como imagen: sería el mismo
    # mensaje dos veces en la pieza.
    assert (40, 40, 300, 200) not in imagenes


def test_una_marca_pequena_sobre_una_foto_deja_la_foto(monkeypatch, client, project):
    """Al revés: unas letras pequeñas sobre un objeto grande no lo convierten en copy."""
    layers, warnings = _separar(
        monkeypatch, client, project,
        region=(30, 30, 400, 400),
        textos=[("ref. 123", 60, 380, 90, 24)],
    )
    textos = [l.content for l in layers if l.type.value == "text"]
    imagenes = [(l.x, l.y, l.width, l.height) for l in layers if l.type.value == "image"]
    assert (30, 30, 400, 400) in imagenes
    assert not any("ref. 123" in (t or "") for t in textos)
    assert any("dentro de bloques del arte" in w for w in warnings)


def test_un_recorte_no_se_amplia_sin_limite():
    """Un bloque decorativo pequeño no puede acabar haciendo de fondo."""
    import random

    from app.models import Layer, LayerCategory, LayerType
    from app.services.layout_engine import MAX_UPSCALE, build_placements

    pequeno = Layer(
        id="d1", name="Bloque", type=LayerType.IMAGE, category=LayerCategory.DECORATION,
        src="layers/d.png", x=800, y=55, width=380, height=230, z_index=2,
    )
    placements, _ = build_placements(
        [pequeno], "product_center_headline_top", 1080, 1350, random.Random(3),
        intensity="moderate",
    )
    puesto = next(p for p in placements if p.layer.id == "d1")
    assert puesto.width <= pequeno.width * MAX_UPSCALE + 2
    assert puesto.height <= pequeno.height * MAX_UPSCALE + 2
