"""Pliegos: un PSD con varias piezas se detecta y se corta en varios proyectos.

Las agencias entregan un solo PSD con 2, 4 o más avisos sobre el mismo lienzo.
Photoshop los guarda como *artboards*; cuando no los usa, quedan separados por
espacio vacío. Aquí se cubren ambos caminos y el recorte de las capas.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import settings
from app.services import psd_import
from app.services.psd_import import _content_mask, _grid_boxes
from tests.conftest import make_artwork
from tests.psd_fixture import sample_kv, sample_sheet

pytest.importorskip("psd_tools", reason="psd-tools no instalado")

GAP = 40
#: Mismo reparto que los pliegos reales: dos cuadradas arriba, dos verticales abajo.
SHEET_PIECES = [
    (GAP, GAP, 540, 540),
    (GAP + 580, GAP, 540, 540),
    (GAP, GAP + 580, 540, 675),
    (GAP + 580, GAP + 580, 540, 675),
]
SHEET_CANVAS = (1160, 1295)


@pytest.fixture()
def sheet_psd(tmp_path):
    return sample_sheet(tmp_path / "pliego.psd", SHEET_PIECES, SHEET_CANVAS)


@pytest.fixture()
def sheet_in_ingest(sheet_psd):
    """El pliego, copiado a la carpeta de ingesta y retirado al terminar."""
    target = settings.ingest_dir / "pliego_pruebas.psd"
    target.write_bytes(sheet_psd.read_bytes())
    yield "pliego_pruebas.psd"
    target.unlink(missing_ok=True)


# ------------------------------------------------------------------- geometría
def test_gutters_split_a_sheet_into_its_pieces():
    """Un pliego con pasillos vacíos se separa en piezas exactas y en orden."""
    width = GAP * 3 + 540 * 2
    height = GAP * 3 + 540 + 675
    sheet = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    boxes = [
        (GAP, GAP, 540, 540),
        (GAP * 2 + 540, GAP, 540, 540),
        (GAP, GAP * 2 + 540, 540, 675),
        (GAP * 2 + 540, GAP * 2 + 540, 540, 675),
    ]
    for index, (x, y, w, h) in enumerate(boxes):
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=(180 + index, 160, 140))

    assert _grid_boxes(_content_mask(sheet)) == boxes


def test_a_single_piece_is_never_split():
    art = Image.new("RGB", (540, 675), (200, 180, 160))
    ImageDraw.Draw(art).rectangle([30, 30, 500, 150], fill=(240, 240, 240))
    assert len(_grid_boxes(_content_mask(art))) == 1


def test_transparent_canvas_uses_the_alpha_channel():
    sheet = Image.new("RGBA", (1160, 620), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for x in (GAP, GAP + 580):
        draw.rectangle([x, GAP, x + 539, GAP + 539], fill=(200, 180, 160, 255))
    assert len(_grid_boxes(_content_mask(sheet))) == 2


# ------------------------------------------------------------------- detección
def test_sheet_without_artboards_is_detected_by_geometry(sheet_psd):
    pieces, warnings = psd_import.detect_pieces(sheet_psd)

    assert [(p.x, p.y, p.width, p.height) for p in pieces] == SHEET_PIECES
    assert [p.origin for p in pieces] == ["grid"] * 4
    assert any("4 piezas" in warning for warning in warnings)


def test_a_normal_kv_reports_exactly_one_piece(tmp_path):
    pieces, _ = psd_import.detect_pieces(sample_kv(tmp_path / "kv.psd", 900, 660))

    assert len(pieces) == 1
    assert (pieces[0].width, pieces[0].height) == (900, 660)


def test_fast_detection_skips_the_pixel_analysis(sheet_psd):
    """El listado de la carpeta no puede aplanar cada PSD: solo mira artboards."""
    pieces, _ = psd_import.detect_pieces(sheet_psd, analyse_pixels=False)

    assert len(pieces) == 1
    assert pieces[0].origin == "canvas"


# ------------------------------------------------------------------ importación
def test_layer_coordinates_are_relative_to_the_piece(client: TestClient, sheet_in_ingest):
    """Cada pieza es un proyecto propio: sus capas empiezan en (0,0), no en el pliego."""
    response = client.post("/projects/from-ingest/split", json={"source": sheet_in_ingest})
    assert response.status_code == 201, response.text
    payload = response.json()

    assert payload["pieces_detected"] == 4
    assert payload["pieces_imported"] == 4

    for project, (_x, _y, width, height) in zip(payload["projects"], SHEET_PIECES):
        assert project["canvas"] == {"width": width, "height": height}
        content = [
            layer for layer in project["layers"] if layer["category"] != "background"
        ]
        assert content, f"{project['name']} se importó sin capas"
        for layer in content:
            assert 0 <= layer["x"] <= width
            assert 0 <= layer["y"] <= height
            assert layer["x"] + layer["width"] <= width
            assert layer["y"] + layer["height"] <= height


def test_each_piece_keeps_its_own_artwork(client: TestClient, sheet_in_ingest):
    """El recorte es real: cada proyecto muestra su pieza, no el pliego entero."""
    projects = client.post(
        "/projects/from-ingest/split", json={"source": sheet_in_ingest}
    ).json()["projects"]

    tones = []
    for project, (_x, _y, width, height) in zip(projects, SHEET_PIECES):
        raw = client.get(
            f"/projects/{project['project_id']}/files/{project['source']['path']}"
        )
        assert raw.status_code == 200
        with Image.open(io.BytesIO(raw.content)) as image:
            assert image.size == (width, height)
            tones.append(image.convert("RGB").getpixel((5, 5)))

    # El fixture da a cada pieza un fondo distinto: si el recorte fuese el mismo
    # trozo del pliego, los cuatro colores coincidirían.
    assert len(set(tones)) == 4


def test_piece_metadata_records_where_it_came_from(client: TestClient, sheet_in_ingest):
    projects = client.post(
        "/projects/from-ingest/split", json={"source": sheet_in_ingest}
    ).json()["projects"]

    piece = projects[2]["meta"]["psd_piece"]
    assert (piece["x"], piece["y"]) == SHEET_PIECES[2][:2]
    assert piece["origin"] == "grid"
    assert projects[2]["meta"]["ingest_source"] == sheet_in_ingest


def test_only_the_requested_pieces_are_imported(client: TestClient, sheet_in_ingest):
    payload = client.post(
        "/projects/from-ingest/split", json={"source": sheet_in_ingest, "pieces": [1, 3]}
    ).json()

    assert payload["pieces_detected"] == 4
    assert payload["pieces_imported"] == 2
    assert [project["canvas"]["height"] for project in payload["projects"]] == [540, 675]


def test_names_carry_the_piece_only_when_there_is_more_than_one(
    client: TestClient, sheet_in_ingest, tmp_path
):
    sheet = client.post(
        "/projects/from-ingest/split",
        json={"source": sheet_in_ingest, "name": "Salas"},
    ).json()["projects"]
    assert all(project["name"].startswith("Salas · ") for project in sheet)

    single = settings.ingest_dir / "kv_simple.psd"
    single.write_bytes(sample_kv(tmp_path / "kv.psd", 640, 640).read_bytes())
    try:
        projects = client.post(
            "/projects/from-ingest/split",
            json={"source": "kv_simple.psd", "name": "KV suelto"},
        ).json()["projects"]
        assert [project["name"] for project in projects] == ["KV suelto"]
    finally:
        single.unlink(missing_ok=True)


def test_unknown_piece_index_is_rejected(client: TestClient, sheet_in_ingest):
    response = client.post(
        "/projects/from-ingest/split", json={"source": sheet_in_ingest, "pieces": [9]}
    )
    assert response.status_code == 400
    assert "9" in response.json()["detail"]


def test_split_requires_a_psd(client: TestClient):
    path = settings.ingest_dir / "plano_split.png"
    path.write_bytes(make_artwork(600, 600))
    try:
        response = client.post(
            "/projects/from-ingest/split", json={"source": "plano_split.png"}
        )
        assert response.status_code == 400
        assert "PSD" in response.json()["detail"]
    finally:
        path.unlink(missing_ok=True)


def test_split_blocks_path_traversal(client: TestClient):
    for evil in ("../../etc/passwd", "/etc/passwd", "../project.json"):
        response = client.post("/projects/from-ingest/split", json={"source": evil})
        assert response.status_code == 400, evil


# ---------------------------------------------------------------------- subida
def test_uploaded_sheet_is_split_and_the_psd_is_not_kept(
    client: TestClient, sheet_psd
):
    response = client.post(
        "/projects/split",
        data={"name": "Pliego subido"},
        files={"artwork": ("pliego.psd", sheet_psd.read_bytes(), "image/vnd.adobe.photoshop")},
    )
    assert response.status_code == 201, response.text
    payload = response.json()

    assert payload["pieces_imported"] == 4
    assert [project["canvas"]["width"] for project in payload["projects"]] == [540] * 4
    # El PSD se usa y se descarta: no se copia 100 MB en cada uno de los proyectos.
    assert not list((settings.data_dir / "tmp").glob("*"))
    for project in payload["projects"]:
        assert "psd_source" not in project["meta"]
        assert project["meta"]["psd_original_name"] == "pliego.psd"


def test_uploaded_flat_art_is_rejected_by_the_split_endpoint(client: TestClient):
    response = client.post(
        "/projects/split",
        files={"artwork": ("plano.png", make_artwork(600, 600), "image/png")},
    )
    assert response.status_code == 400
    assert "PSD" in response.json()["detail"]


# ------------------------------------------------------------------- endpoints
def test_ingest_pieces_endpoint_lists_every_piece(client: TestClient, sheet_in_ingest):
    payload = client.get("/ingest/pieces", params={"source": sheet_in_ingest}).json()

    assert (payload["width"], payload["height"]) == SHEET_CANVAS
    assert [(p["x"], p["y"], p["width"], p["height"]) for p in payload["pieces"]] == SHEET_PIECES
    assert [p["index"] for p in payload["pieces"]] == [0, 1, 2, 3]


def test_ingest_piece_preview_returns_the_piece_alone(client: TestClient, sheet_in_ingest):
    response = client.get(
        "/ingest/pieces/preview",
        params={"source": sheet_in_ingest, "index": 2, "max_side": 200},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"

    with Image.open(io.BytesIO(response.content)) as preview:
        assert max(preview.size) <= 200
        # La pieza 2 es vertical (540x675): la miniatura conserva la proporción.
        assert preview.height > preview.width


def test_ingest_listing_counts_pieces_only_when_asked(client: TestClient, sheet_in_ingest):
    plain = client.get("/ingest").json()
    entry = next(item for item in plain["files"] if item["path"] == sheet_in_ingest)
    assert entry["pieces"] == 1  # sin analizar: el pliego no usa artboards

    detailed = client.get("/ingest", params={"with_pieces": True}).json()
    entry = next(item for item in detailed["files"] if item["path"] == sheet_in_ingest)
    assert entry["pieces"] == 1  # el conteo rápido solo cuenta artboards


def test_brand_assets_are_copied_to_every_piece(client: TestClient, sheet_psd):
    """El logo es de la campaña, no de un aviso: llega a las cuatro piezas."""
    response = client.post(
        "/projects/split",
        data={"name": "Con marca"},
        files={
            "artwork": ("pliego.psd", sheet_psd.read_bytes(), "image/vnd.adobe.photoshop"),
            "logo": ("logo.png", make_artwork(300, 300), "image/png"),
        },
    )
    assert response.status_code == 201, response.text
    projects = response.json()["projects"]

    assert len(projects) == 4
    for project in projects:
        assert project["references"]["logo"] is not None
