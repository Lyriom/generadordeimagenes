from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.models.campaign import NormalizedPlacement, TemplateBlueprint, TemplateCandidate
from app.models.formats import DEFAULT_SAFE_AREA
from app.services.campaign_creative import CampaignProductionError, _layout, resolve_format


def _ready_campaign(client: TestClient, artwork_png: bytes) -> tuple[str, str, list[dict]]:
    profile = client.post("/clients", json={"name": "Cliente producción"}).json()
    client_id = profile["client_id"]
    campaign = client.post(
        f"/clients/{client_id}/campaigns",
        json={"name": "Promo adaptable", "objective": "Vender productos con claridad"},
    ).json()
    campaign_id = campaign["campaign_id"]
    uploaded = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/sources",
        files=[("files", ("referencia.png", artwork_png, "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    analysis = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/brief/generate",
        json={"use_ai": False},
    )
    assert analysis.status_code == 200, analysis.text
    reviewed = client.put(
        f"/clients/{client_id}/campaigns/{campaign_id}/brief",
        json={"objective": analysis.json()["brief"]["objective"]},
    )
    assert reviewed.status_code == 200, reviewed.text
    regenerated = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/brief/generate",
        json={"use_ai": False, "preserve_review": True},
    )
    assert regenerated.status_code == 200, regenerated.text
    candidates = regenerated.json()["template_candidates"]
    for candidate in candidates:
        approved = client.post(
            f"/clients/{client_id}/campaigns/{campaign_id}/template-candidates/"
            f"{candidate['candidate_id']}/approve",
            json={"notes": "Sistema aprobado para producción"},
        )
        assert approved.status_code == 200, approved.text
    return client_id, campaign_id, candidates


def _matrix_xlsx() -> bytes:
    values = [
        "producto", "imagen", "precio", "formatos", "propuestas",
        "Televisor", "tv.png", "499", "300x300|320x400", "1",
    ]
    shared = "".join(f"<si><t>{value}</t></si>" for value in values)
    cells = []
    for row_number, offset in ((1, 0), (2, 5)):
        row = "".join(
            f'<c r="{letter}{row_number}" t="s"><v>{offset + index}</v></c>'
            for index, letter in enumerate("ABCDE")
        )
        cells.append(f'<row r="{row_number}">{row}</row>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(cells)}</sheetData></worksheet>',
        )
    return buffer.getvalue()


def test_vista_previa_xlsx_usa_el_contrato_real_de_produccion(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/preview",
        files=[(
            "matrix",
            (
                "pedido.xlsx",
                _matrix_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        )],
    )

    assert response.status_code == 200, response.text
    assert response.json()["requested_pieces"] == 2
    assert response.json()["rows"][0]["producto"] == "Televisor"


def test_candidatas_tienen_preview_real_sin_crear_proyectos(
    client: TestClient, artwork_png: bytes
):
    before = len(client.get("/projects").json())
    client_id, campaign_id, candidates = _ready_campaign(client, artwork_png)

    preview = client.get(
        f"/clients/{client_id}/campaigns/{campaign_id}/template-candidates/"
        f"{candidates[0]['candidate_id']}/preview"
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("image/png")
    with Image.open(io.BytesIO(preview.content)) as image:
        assert image.size == (720, 900)
    expected = {
        "portrait": (720, 900),
        "square": (720, 720),
        "story": (540, 960),
        "landscape": (960, 503),
    }
    for aspect, size in expected.items():
        adapted = client.get(
            f"/clients/{client_id}/campaigns/{campaign_id}/template-candidates/"
            f"{candidates[0]['candidate_id']}/preview?aspect={aspect}"
        )
        assert adapted.status_code == 200, adapted.text
        with Image.open(io.BytesIO(adapted.content)) as image:
            assert image.size == size
    assert len(client.get("/projects").json()) == before


def test_matriz_produce_formatos_cantidad_zip_csv_y_psd(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    matrix = (
        "producto,imagen,titular,precio,cta,formatos,cantidad_propuestas\n"
        "Televisor,tv.png,Una pantalla para todo,499,Conoce más,320x400|300x300,2\n"
    ).encode()

    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[
            ("matrix", ("matriz.csv", matrix, "text/csv")),
            ("product_files", ("tv.png", artwork_png, "image/png")),
        ],
        data={"default_formats": "[]", "use_ai_copy": "false"},
    )

    assert response.status_code == 201, response.text
    batch = response.json()
    assert batch["total_rows"] == 1
    assert batch["total_pieces"] == 4
    assert {(piece["width"], piece["height"]) for piece in batch["pieces"]} == {
        (320, 400),
        (300, 300),
    }
    assert {piece["proposal"] for piece in batch["pieces"]} == {1, 2}

    first = batch["pieces"][0]
    png = client.get("/clients" + first["preview_url"].split("/clients", 1)[1])
    assert png.status_code == 200
    with Image.open(io.BytesIO(png.content)) as image:
        assert image.size == (first["width"], first["height"])

    psd_url = (
        f"/clients/{client_id}/campaigns/{campaign_id}/production/{batch['batch_id']}"
        f"/files/{first['psd']}"
    )
    psd = client.get(psd_url)
    assert psd.status_code == 200
    assert psd.content.startswith(b"8BPS")

    archive = client.get("/clients" + batch["zip_url"].split("/clients", 1)[1])
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        names = bundle.namelist()
        assert "estado.csv" in names
        assert "manifest.json" in names
        assert sum(name.endswith(".png") for name in names) == 4
        assert sum(name.endswith(".jpg") for name in names) == 4
        assert sum(name.endswith(".psd") for name in names) == 4


def test_produccion_explica_imagen_faltante(client: TestClient, artwork_png: bytes):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[
            (
                "matrix",
                ("matriz.csv", b"producto,imagen\nLaptop,laptop.png\n", "text/csv"),
            )
        ],
        data={"default_formats": '["meta_feed_square"]', "use_ai_copy": "false"},
    )
    assert response.status_code == 422
    assert "fila 2" in response.json()["detail"]
    assert "laptop.png" in response.json()["detail"]


def test_combo_exige_una_imagen_por_cada_producto(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[
            (
                "matrix",
                (
                    "matriz.csv",
                    b"producto,imagen,formatos\nTV|Soundbar,tv.png,300x300\n",
                    "text/csv",
                ),
            ),
            ("product_files", ("tv.png", artwork_png, "image/png")),
        ],
        data={"default_formats": "[]", "use_ai_copy": "false"},
    )

    assert response.status_code == 422, response.text
    assert "1/2 imagenes" in response.json()["detail"]


def test_aliases_del_mismo_formato_no_sobrescriben_ni_duplican_la_salida(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[
            (
                "matrix",
                (
                    "matriz.csv",
                    b"producto,imagen,formatos\nTV,tv.png,feed|meta_feed_4_5\n",
                    "text/csv",
                ),
            ),
            ("product_files", ("tv.png", artwork_png, "image/png")),
        ],
        data={"default_formats": "[]", "use_ai_copy": "false"},
    )

    assert response.status_code == 201, response.text
    batch = response.json()
    assert batch["total_pieces"] == 1
    assert batch["pieces"][0]["format"] == "meta_feed_4_5"


def test_rechaza_un_lienzo_que_podria_agotar_la_memoria_del_servidor():
    with pytest.raises(CampaignProductionError, match="limite de pixeles"):
        resolve_format("8000x8000")


def test_familias_de_plantilla_tienen_reticulas_distintas_y_dentro_del_lienzo():
    product_regions: set[tuple[int, int, int, int]] = set()
    categories = (
        "single_product",
        "price_promotion",
        "combo",
        "product_benefit",
        "institutional",
    )
    for width, height in ((1080, 1080), (1080, 1350), (1200, 628)):
        for proposal in (1, 2):
            for category in categories:
                candidate = TemplateCandidate(name=category, category=category)
                regions = _layout(
                    candidate,
                    width,
                    height,
                    dict(DEFAULT_SAFE_AREA),
                    proposal=proposal,
                    visible_fields={
                        "product",
                        "headline",
                        "price",
                        "cta",
                        "legal",
                        "logo",
                    },
                )
                if (width, height, proposal) == (1080, 1080, 1):
                    product_regions.add(regions["product"])
                for x, y, region_width, region_height in regions.values():
                    assert x >= 0 and y >= 0
                    assert region_width > 0 and region_height > 0
                    assert x + region_width <= width
                    assert y + region_height <= height
    assert len(product_regions) == 5


def test_layout_usa_placements_inferidos_en_el_blueprint():
    candidate = TemplateCandidate(
        name="Inferida desde la campaña",
        category="single_product",
        revision_hash="a" * 64,
        blueprint=TemplateBlueprint(
            placements={
                "square": {
                    "product": NormalizedPlacement(
                        x=.08, y=.12, width=.44, height=.66
                    ),
                    "headline": NormalizedPlacement(
                        x=.58, y=.18, width=.34, height=.18
                    ),
                }
            }
        ),
    )

    regions = _layout(
        candidate,
        1000,
        1000,
        dict(DEFAULT_SAFE_AREA),
        proposal=1,
        visible_fields={"product", "headline", "logo"},
    )

    product = regions["product"]
    headline = regions["headline"]
    assert product[0] < headline[0]
    assert product[2] > headline[2]
