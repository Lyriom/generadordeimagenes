from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from psd_tools import PSDImage

from app.models.campaign import (
    Campaign,
    CampaignBrief,
    NormalizedPlacement,
    ProductCountRange,
    TemplateBlueprint,
    TemplateCandidate,
    TemplateSlotProposal,
)
from app.models.project import utcnow
from app.models.formats import DEFAULT_SAFE_AREA, FORMAT_PRESETS
from app.services import campaign_creative
from app.services.campaign_analysis import _canonical_candidate_payload, deterministic_candidates
from app.services.campaign_creative import (
    CampaignProductionError,
    _layout,
    _open_product_for_render,
    _render,
    _validate_product_render_budget,
    _values,
    complete_copy_once,
    resolve_format,
)
from app.services.production_matrix import ai_fillable_fields, parse_csv, score_template
from app.services import campaign_store


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
    assert response.json()["plans"][0]["status"] == "ready"
    assert response.json()["plans"][0]["template"]


def test_preview_conserva_borrador_y_explicita_plantilla_incompatible(
    client: TestClient,
):
    profile = client.post("/clients", json={"name": "Marca con plantilla limitada"}).json()
    client_id = profile["client_id"]
    campaign_payload = client.post(
        f"/clients/{client_id}/campaigns",
        json={"name": "Campaña", "objective": "Vender"},
    ).json()
    campaign_id = campaign_payload["campaign_id"]
    campaign = campaign_store.load_campaign(client_id, campaign_id)
    campaign.template_candidates = [
        TemplateCandidate(
            name="Producto editorial",
            category="single_product",
            slots=[
                TemplateSlotProposal(
                    key="producto", label="Producto", category="product", kind="image", required=True,
                ),
                TemplateSlotProposal(key="titular", label="Titular", category="headline"),
            ],
            status="approved",
            approved=True,
        )
    ]
    campaign_store.save_campaign(campaign)

    preview = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/preview",
        files=[(
            "matrix",
            ("pedido.csv", b"producto,imagen,precio\nTV,tv.png,499\n", "text/csv"),
        )],
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["matrix_draft_id"]
    [plan] = body["plans"]
    assert plan["status"] == "incompatible"
    assert plan["template"] is None
    assert "precio" in plan["required_fields"]
    assert "Ninguna plantilla aprobada" in plan["message"]


def test_matriz_guardada_y_pieza_institucional_no_exigen_producto(
    client: TestClient,
):
    """Una fila de mensaje puede sobrevivir un F5 y producir sin foto.

    Es el contrato central para plantillas de campaña: los productos llegan
    después en la matriz, pero una pieza de fecha, marca o titular no debe
    tomar por accidente la única foto que haya quedado cargada.
    """

    profile = client.post("/clients", json={"name": "Marca institucional"}).json()
    client_id = profile["client_id"]
    campaign_payload = client.post(
        f"/clients/{client_id}/campaigns",
        json={"name": "Lanzamiento", "objective": "Presentar la campaña"},
    ).json()
    campaign_id = campaign_payload["campaign_id"]
    campaign = campaign_store.load_campaign(client_id, campaign_id)
    campaign.brief = CampaignBrief(primary_message="Una nueva etapa")
    campaign.brief_reviewed_at = utcnow()
    campaign.template_candidates = [
        TemplateCandidate(
            name="Mensaje de campaña",
            category="institutional",
            supported_product_count=ProductCountRange(minimum=0, maximum=0),
            slots=[
                TemplateSlotProposal(
                    key="titular", label="Titular", category="headline",
                    generate_if_missing=True,
                ),
                TemplateSlotProposal(
                    key="logo", label="Logo", category="logo", kind="image",
                ),
            ],
            status="approved",
            approved=True,
        )
    ]
    campaign_store.save_campaign(campaign)

    preview = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/preview",
        files=[(
            "matrix",
            (
                "mensaje.csv",
                b"titular,formatos\nNueva etapa,300x300\n",
                "text/csv",
            ),
        )],
        data={"default_formats": "[]"},
    )
    assert preview.status_code == 200, preview.text
    draft_id = preview.json()["matrix_draft_id"]
    assert isinstance(draft_id, str) and draft_id

    # No reenviamos ni la matriz ni una foto: el servidor vuelve a leer el
    # draft validado y la plantilla institucional conserva el producto vacío.
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        data={
            "matrix_draft_id": draft_id,
            "default_formats": "[]",
            "use_ai_copy": "false",
        },
    )
    assert response.status_code == 201, response.text
    batch = response.json()
    assert batch["total_pieces"] == 1
    assert batch["pieces"][0]["product"] == ""


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


def test_preview_del_master_no_finge_precio_descuento_o_cta_sin_evidencia():
    campaign = Campaign(
        client_id="cliente",
        name="Lanzamiento de producto",
        objective="Presentar una nueva línea",
        brief=CampaignBrief(primary_message="Diseño pensado para tu día"),
    )
    master = next(
        candidate
        for candidate in deterministic_candidates(campaign, campaign.brief)
        if candidate.name == "Producto protagonista"
    )

    image, layers = _render(
        campaign,
        "Marca",
        master,
        width=720,
        height=900,
        safe=dict(DEFAULT_SAFE_AREA),
        proposal=1,
    )
    try:
        names = {name for name, _layer in layers}
        assert "Precio" not in names
        assert "Precio anterior" not in names
        assert "Cuota" not in names
        assert "Descuento" not in names
        assert "CTA" not in names
        assert "Titular" in names
        assert any(name.startswith("Producto") for name in names)
    finally:
        image.close()
        for _name, layer in layers:
            layer.close()


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
    # Los tres entregables son el mismo arte: el PSD editable y el JPG tienen
    # que medir exactamente lo que pidio la matriz, no solo abrir sin error.
    documento = PSDImage.open(io.BytesIO(psd.content))
    assert (documento.width, documento.height) == (first["width"], first["height"])
    jpg_url = (
        f"/clients/{client_id}/campaigns/{campaign_id}/production/{batch['batch_id']}"
        f"/files/{first['jpg']}"
    )
    jpg = client.get(jpg_url)
    assert jpg.status_code == 200
    with Image.open(io.BytesIO(jpg.content)) as imagen:
        assert imagen.size == (first["width"], first["height"])

    # Y todas las piezas de la tanda, no solo la primera.
    for piece in batch["pieces"]:
        descarga = client.get(
            f"/clients/{client_id}/campaigns/{campaign_id}/production/"
            f"{batch['batch_id']}/files/{piece['png']}"
        )
        assert descarga.status_code == 200
        with Image.open(io.BytesIO(descarga.content)) as imagen:
            assert imagen.size == (piece["width"], piece["height"]), piece["png"]

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


def test_previsualizacion_resuelve_aliases_y_rechaza_formatos_antes_de_subir_fotos(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    preview = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/preview",
        files=[(
            "matrix",
            ("matriz.csv", b"producto,imagen,formatos\nTV,tv.png,feed|meta_feed_4_5\n", "text/csv"),
        )],
        data={"default_formats": "[]"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["requested_pieces"] == 1

    invalid = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/preview",
        files=[(
            "matrix",
            ("matriz.csv", b"producto,formatos\nTV,formato-imaginario\n", "text/csv"),
        )],
        data={"default_formats": "[]"},
    )
    assert invalid.status_code == 422
    assert "Formato 'formato-imaginario' desconocido" in invalid.json()["detail"]


def test_rechaza_un_lienzo_que_podria_agotar_la_memoria_del_servidor():
    with pytest.raises(CampaignProductionError, match="limite de pixeles"):
        resolve_format("8000x8000")


def test_producto_bmp_se_acepta_como_imagen_de_produccion(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    with Image.open(io.BytesIO(artwork_png)) as source:
        bmp = io.BytesIO()
        source.convert("RGB").save(bmp, format="BMP")
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[
            ("matrix", ("matriz.csv", b"producto,imagen,formatos\nTV,tv.bmp,feed\n", "text/csv")),
            ("product_files", ("tv.bmp", bmp.getvalue(), "image/bmp")),
        ],
        data={"default_formats": "[]", "use_ai_copy": "false"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["total_pieces"] == 1


def test_imagen_de_producto_persistida_sobrevive_y_se_usa_sin_reenviarla(
    client: TestClient, artwork_png: bytes
):
    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    uploaded = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/assets",
        files=[("files", ("televisor.png", artwork_png, "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    [asset] = uploaded.json()["assets"]

    restored = client.get(f"/clients/{client_id}/campaigns/{campaign_id}")
    assert restored.status_code == 200
    assert restored.json()["production_assets"][0]["asset_id"] == asset["asset_id"]
    file_response = client.get(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/assets/"
        f"{asset['asset_id']}/file"
    )
    assert file_response.status_code == 200
    assert file_response.content == artwork_png
    assert file_response.headers["content-type"].startswith("image/png")
    assert file_response.headers["x-content-type-options"] == "nosniff"

    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[("matrix", ("matriz.csv", b"producto,imagen,formatos\nTV,televisor.png,feed\n", "text/csv"))],
        data={
            "product_asset_ids": f'["{asset["asset_id"]}"]',
            "default_formats": "[]",
            "use_ai_copy": "false",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["total_pieces"] == 1

    deleted = client.delete(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/assets/{asset['asset_id']}"
    )
    assert deleted.status_code == 204
    assert client.get(f"/clients/{client_id}/campaigns/{campaign_id}").json()["production_assets"] == []


def test_imagen_valida_no_conserva_el_mime_declarado_por_el_navegador(
    client: TestClient, artwork_png: bytes
):
    """Un upload puede afirmar ``text/html`` aunque sus bytes sean PNG.

    La vista previa siempre debe usar el MIME que Pillow decodificó, nunca el
    encabezado enviado por quien sube el archivo.
    """

    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    uploaded = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/assets",
        files=[("files", ("producto.png", artwork_png, "text/html"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    [asset] = uploaded.json()["assets"]
    assert asset["media_type"] == "image/png"

    preview = client.get(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/assets/"
        f"{asset['asset_id']}/file"
    )
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.headers["x-content-type-options"] == "nosniff"


def test_un_fondo_marcado_por_el_equipo_se_compone_nitido_en_la_plantilla(
    client: TestClient, artwork_png: bytes
):
    profile = client.post("/clients", json={"name": "Marca con branding"}).json()
    client_id = profile["client_id"]
    campaign_payload = client.post(
        f"/clients/{client_id}/campaigns",
        json={"name": "Campaña de marca", "objective": "Recordación"},
    ).json()
    campaign_id = campaign_payload["campaign_id"]
    uploaded = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/sources",
        files=[("files", ("fondo-limpio.png", artwork_png, "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    source_id = uploaded.json()["sources"][0]["source_id"]

    marked = client.put(
        f"/clients/{client_id}/campaigns/{campaign_id}/sources/{source_id}/role",
        json={"role": "background"},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["roles"] == ["background"]

    campaign = campaign_store.load_campaign(client_id, campaign_id)
    fixed = campaign_creative._fixed_brand_background(campaign, (180, 120))
    assert fixed is not None
    name, layer = fixed
    try:
        assert "fondo-limpio.png" in name
        assert layer.size == (180, 120)
        # El layer llega opaco y directo; no es la textura de referencia que
        # el motor reduce y desenfoca para no reciclar un KV completo.
        assert layer.getchannel("A").getextrema()[0] == 255
    finally:
        layer.close()


def test_produccion_encolada_guarda_matriz_y_fotos_antes_del_worker(
    client: TestClient, artwork_png: bytes, monkeypatch: pytest.MonkeyPatch
):
    """El camino 202 no depende de File temporales ni de Redis para consultarse."""

    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    from app.worker import produce_campaign_batch_task

    enqueued: dict[str, object] = {}

    def leave_pending(*, args: tuple[str, str, str], task_id: str):
        enqueued["args"] = args
        enqueued["task_id"] = task_id
        return object()

    monkeypatch.setattr(produce_campaign_batch_task, "apply_async", leave_pending)
    response = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production",
        files=[
            ("matrix", ("pedido.csv", b"producto,imagen,formatos\nTV,tv.png,feed\n", "text/csv")),
            ("product_files", ("tv.png", artwork_png, "image/png")),
        ],
        data={"default_formats": "[]", "use_ai_copy": "false"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "PENDING"
    assert enqueued["args"] == (client_id, campaign_id, body["task_id"])
    assert enqueued["task_id"] == body["task_id"]

    status_response = client.get(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/tasks/{body['task_id']}"
    )
    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["state"] == "PENDING"
    assert status_response.json()["meta"]["planned_pieces"] == 1

    pending_response = client.get(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/tasks"
    )
    assert pending_response.status_code == 200, pending_response.text
    assert [task["task_id"] for task in pending_response.json()["tasks"]] == [body["task_id"]]
    # La respuesta pública no filtra el snapshot del brief ni rutas internas
    # de los productos que sí conserva job.json para el worker.
    assert "campaign_snapshot" not in body
    assert "product_files" not in body

    job = campaign_store.load_production_job(client_id, campaign_id, body["task_id"])
    assert job.matrix_filename == "pedido.csv"
    assert set(job.product_files) == {"tv.png"}
    assert campaign_store.campaign_path(client_id, campaign_id, job.matrix_path).read_bytes().startswith(
        b"producto,imagen"
    )
    assert campaign_store.campaign_path(
        client_id, campaign_id, job.product_files["tv.png"]
    ).read_bytes() == artwork_png
    campaign = client.get(f"/clients/{client_id}/campaigns/{campaign_id}").json()
    assert [asset["filename"] for asset in campaign["production_assets"]] == ["tv.png"]


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


def test_aliases_en_ingles_de_openai_se_vuelven_slots_ejecutables():
    payload = _canonical_candidate_payload(
        {
            "name": "Price hero",
            "category": "promotion",
            "slots": [
                {"key": "product", "label": "Product", "kind": "image"},
                {"key": "headline", "label": "Headline"},
                {"key": "price", "label": "Price", "kind": "money"},
                {"key": "call_to_action", "label": "CTA"},
            ],
            "blueprint": {
                "archetype": "price",
                "placements": {
                    "1:1": {
                        "product": {"x": .08, "y": .1, "width": .42, "height": .62},
                        "headline": {"x": .56, "y": .16, "width": .34, "height": .2},
                    }
                },
            },
        }
    )
    candidate = TemplateCandidate.model_validate(payload)
    [row] = parse_csv("producto,imagen,titular,precio\nTV,tv.png,Una pantalla,499\n")
    campaign = Campaign(client_id="client", name="Prueba", objective="Vender")

    assert candidate.category == "price_promotion"
    assert {slot.key for slot in candidate.slots} >= {"producto", "titular", "precio", "cta"}
    assert candidate.blueprint.archetype == "price_focus"
    # Los slots se conservan en español para la matriz, pero los placements se
    # guardan con las regiones que sí consume el renderer.
    assert {"product", "headline"} <= set(candidate.blueprint.placements["square"])
    regions = _layout(
        candidate,
        1000,
        1000,
        dict(DEFAULT_SAFE_AREA),
        proposal=1,
        visible_fields={"product", "headline", "price"},
    )
    assert regions["product"][0] < 150
    assert regions["headline"][0] > 500
    values = _values(row, campaign, candidate)
    assert values["headline"] == "Una pantalla"
    assert values["price"] == "499"
    assert score_template(row, candidate) > float("-inf")


def test_cta_vacio_no_recibe_un_llamado_generico():
    [row] = parse_csv("producto,imagen\nTV,tv.png\n")
    campaign = Campaign(client_id="client", name="Prueba", objective="Conocer la marca")
    candidate = TemplateCandidate(
        name="Institucional sin CTA forzado",
        category="institutional",
        slots=[
            TemplateSlotProposal(
                key="producto", label="Producto", category="product", kind="image", required=True
            ),
            TemplateSlotProposal(
                key="cta", label="CTA", category="cta", generate_if_missing=True
            ),
        ],
    )

    assert _values(row, campaign, candidate)["cta"] == ""


def test_copy_ia_solo_escribe_los_slots_que_la_plantilla_aprobo(monkeypatch):
    rows = parse_csv("producto,imagen\nTV,tv.png\nLaptop,laptop.png\n")
    only_headline = TemplateCandidate(
        name="Solo titular",
        category="single_product",
        slots=[
            TemplateSlotProposal(key="producto", label="Producto", category="product", kind="image", required=True),
            TemplateSlotProposal(
                key="titular", label="Titular", category="headline", generate_if_missing=True,
            ),
        ],
    )
    headline_and_cta = TemplateCandidate(
        name="Titular y CTA",
        category="single_product",
        slots=[
            TemplateSlotProposal(key="producto", label="Producto", category="product", kind="image", required=True),
            TemplateSlotProposal(
                key="titular", label="Titular", category="headline", generate_if_missing=True,
            ),
            TemplateSlotProposal(key="cta", label="CTA", category="cta", generate_if_missing=True),
        ],
    )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps({"rows": [
                    {"row_number": 2, "headline": "TV inteligente", "cta": "Comprar TV"},
                    {"row_number": 3, "headline": "Laptop lista", "cta": "Comprar laptop"},
                ]})}}]
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(campaign_creative.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(campaign_creative.httpx, "Client", FakeClient)
    warnings = complete_copy_once(
        Campaign(client_id="cliente", name="Campaña", objective="Vender"),
        rows,
        {
            rows[0].row_number: ai_fillable_fields(only_headline),
            rows[1].row_number: ai_fillable_fields(headline_and_cta),
        },
    )

    assert warnings == []
    assert rows[0].titular == "TV inteligente"
    assert rows[0].cta is None
    assert rows[1].titular == "Laptop lista"
    assert rows[1].cta == "Comprar laptop"


def test_legal_aprobado_en_el_brief_se_usa_solo_si_es_copy_real():
    [row] = parse_csv("producto,imagen\nTV,tv.png\n")
    candidate = TemplateCandidate(
        name="Legal adaptable",
        category="single_product",
        slots=[
            TemplateSlotProposal(
                key="producto", label="Producto", category="product", kind="image", required=True
            ),
            TemplateSlotProposal(key="legal", label="Legal", category="legal"),
        ],
    )
    campaign = Campaign(
        client_id="client",
        name="Prueba",
        brief=CampaignBrief(
            legal_requirements=["Válido del 1 al 30 de septiembre de 2026."],
        ),
    )
    assert _values(row, campaign, candidate)["legal"] == "Válido del 1 al 30 de septiembre de 2026."

    campaign.brief = CampaignBrief(
        legal_requirements=["Conservar los legales presentes en el material fuente."],
    )
    assert _values(row, campaign, candidate)["legal"] == ""


def test_combo_se_detiene_antes_de_decodificar_demasiados_pixeles(tmp_path: Path):
    [row] = parse_csv("producto,imagen\nTV|Barra,tv.png|barra.png\n")
    products: list[Path] = []
    for name in ("tv.png", "barra.png"):
        path = tmp_path / name
        Image.new("RGB", (4, 4), "white").save(path)
        products.append(path)

    with pytest.raises(CampaignProductionError, match="suman 0.0 Mpx"):
        _validate_product_render_budget(row, products, max_decode_pixels=24)


def test_producto_se_reduce_a_una_copia_de_trabajo_segura(tmp_path: Path):
    path = tmp_path / "producto.jpg"
    Image.new("RGB", (100, 100), "white").save(path, quality=90)

    product = _open_product_for_render(path, max_working_pixels=400)
    try:
        assert product.mode == "RGBA"
        assert product.width * product.height <= 400
    finally:
        product.close()


def test_combo_de_openai_sin_rango_explicito_acepta_varios_productos():
    payload = _canonical_candidate_payload(
        {
            "name": "Bundle grid",
            "type": "bundle",
            "slots": [
                {"id": "products", "label": "Products", "repeatable": True},
                {"id": "headline", "label": "Headline"},
            ],
            "blueprint": {"archetype": "grid"},
        }
    )
    candidate = TemplateCandidate.model_validate(payload)
    [row] = parse_csv(
        "producto,imagen\nTelevisor|Soundbar,tv.png|soundbar.png\n"
    )

    assert candidate.category == "combo"
    assert candidate.supported_product_count.minimum == 2
    assert candidate.supported_product_count.maximum >= 2
    assert score_template(row, candidate) > float("-inf")


def test_previsualizaciones_conservan_la_proporcion_y_el_area_segura_reales(
    client: TestClient, artwork_png: bytes, monkeypatch: pytest.MonkeyPatch
):
    """Una previsualizacion es una ubicacion real reducida, no un rectangulo.

    La proporcion debe ser la del preset del catalogo, y el area segura la suya:
    en Stories la interfaz de Instagram cubre 14 % arriba y 20 % abajo, asi que
    aprobar una plantilla con el margen generico del 3,5 % dejaba el titular o
    el legal debajo del nombre de la cuenta.
    """
    client_id, campaign_id, candidates = _ready_campaign(client, artwork_png)
    esperado = {
        "portrait": "meta_feed_4_5",
        "square": "meta_feed_square",
        "story": "meta_stories",
        "landscape": "meta_feed_landscape",
    }
    for aspect, preset_id in esperado.items():
        preset = FORMAT_PRESETS[preset_id]
        respuesta = client.get(
            f"/clients/{client_id}/campaigns/{campaign_id}/template-candidates/"
            f"{candidates[0]['candidate_id']}/preview?aspect={aspect}"
        )
        assert respuesta.status_code == 200, respuesta.text
        with Image.open(io.BytesIO(respuesta.content)) as imagen:
            ancho, alto = imagen.size
        real = preset["width"] / preset["height"]
        # Una diferencia de medio pixel al reducir es redondeo; una proporcion
        # distinta significa que la previsualizacion enseña otro encuadre.
        assert abs(ancho / alto - real) < 0.002, f"{aspect}: {ancho}x{alto} vs {real}"

    # Y el area segura que se usa al dibujarlas es la del preset, no el margen
    # generico: es lo que decide donde puede caer el copy en cada ubicacion.
    usados: dict[tuple[int, int], dict[str, float]] = {}
    original = campaign_creative._render

    def espia(*args, **kwargs):
        usados[(kwargs["width"], kwargs["height"])] = dict(kwargs["safe"])
        return original(*args, **kwargs)

    monkeypatch.setattr(campaign_creative, "_render", espia)
    campaign = campaign_store.load_campaign(client_id, campaign_id)
    campaign_creative.render_candidate_previews(
        campaign, "Marca", campaign.template_candidates[:1]
    )
    assert usados[(540, 960)] == FORMAT_PRESETS["meta_stories"]["safe_area"]
    assert usados[(720, 900)] == FORMAT_PRESETS["meta_feed_4_5"]["safe_area"]
    assert usados[(720, 720)] == FORMAT_PRESETS["meta_feed_square"]["safe_area"]
    assert usados[(960, 503)] == FORMAT_PRESETS["meta_feed_landscape"]["safe_area"]
    # Stories reserva de verdad, no es el margen por defecto disfrazado.
    assert usados[(540, 960)]["top"] > DEFAULT_SAFE_AREA["top"]
    assert usados[(540, 960)]["bottom"] > DEFAULT_SAFE_AREA["bottom"]


def _matrix_csv(**campos: str) -> tuple[str, bytes, str]:
    """Misma matriz que arma el editor manual del navegador."""

    cabeceras = [
        "producto", "imagen", "titular", "subtitulo", "precio_actual", "precio_anterior",
        "cuota", "descuento", "cta", "legal", "vigencia", "formatos",
        "cantidad_propuestas", "notas", "plantilla",
    ]
    fila = [campos.get(nombre, "") for nombre in cabeceras]
    contenido = ",".join(cabeceras) + "\n" + ",".join(f'"{valor}"' for valor in fila)
    return ("pedido.csv", contenido.encode("utf-8"), "text/csv")


def test_la_vista_previa_de_una_fila_compone_el_arte_antes_de_producir(
    client: TestClient, artwork_png: bytes
):
    """El ask de fondo: ver la composición sin pagar la tanda entera.

    Antes solo se podía ver un arte produciendo la tanda y abriendo el ZIP.
    """

    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)
    client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/assets",
        files=[("files", ("televisor.png", artwork_png, "image/png"))],
    )

    respuesta = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/row-preview",
        files=[("matrix", _matrix_csv(
            producto="Televisor", imagen="televisor.png", precio_actual="499",
            cantidad_propuestas="1",
        ))],
        data={"row_number": "2", "piece_format": "meta_feed_4_5"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["row_number"] == 2
    assert cuerpo["template"]["name"]
    # Las medidas anunciadas son las del entregable, no las del recorte que se
    # mira en pantalla: prometer 720 px seria mentir sobre lo que se descarga.
    assert (cuerpo["width"], cuerpo["height"]) == (1080, 1350)

    imagen = client.get(cuerpo["preview_url"].split("?")[0])
    assert imagen.status_code == 200
    assert imagen.headers["content-type"].startswith("image/jpeg")
    with Image.open(io.BytesIO(imagen.content)) as vista:
        # Proporcion exacta del formato, reducida para la pantalla.
        assert max(vista.size) <= campaign_creative.ROW_PREVIEW_MAX_SIDE
        assert abs(vista.width / vista.height - 1080 / 1350) < 0.01
        # Una composicion, no un lienzo en blanco.
        assert len(vista.convert("RGB").getcolors(maxcolors=100000) or []) > 12


def test_la_vista_previa_dice_lo_que_no_puede_ensenar(
    client: TestClient, artwork_png: bytes
):
    """Un hueco vacío en la vista no es un arte que saldrá vacío."""

    client_id, campaign_id, _ = _ready_campaign(client, artwork_png)

    respuesta = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/row-preview",
        files=[("matrix", _matrix_csv(
            producto="Televisor", imagen="televisor.png", precio_actual="499",
        ))],
        data={"row_number": "2"},
    )

    assert respuesta.status_code == 200, respuesta.text
    avisos = respuesta.json()["warnings"]
    assert any("fotos de esta fila" in aviso for aviso in avisos), avisos


def test_una_plantilla_forzada_incompatible_no_deja_sin_vista_previa(
    client: TestClient, artwork_png: bytes
):
    """El callejón sin salida de la captura: se ve la que sí sirve, y por qué."""

    client_id, campaign_id, candidatos = _ready_campaign(client, artwork_png)
    institucional = next(
        (item for item in candidatos if item["category"] == "institutional"), None
    )
    if institucional is None:
        pytest.skip("el analisis local no propuso una plantilla institucional")

    respuesta = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/row-preview",
        files=[("matrix", _matrix_csv(
            producto="Televisor", imagen="televisor.png", precio_actual="499",
            plantilla=institucional["name"],
        ))],
        data={"row_number": "2"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["template"]["name"] != institucional["name"]
    assert any("automatica" in aviso for aviso in cuerpo["warnings"]), cuerpo["warnings"]


def test_el_plan_nombra_la_plantilla_que_si_sirve_en_vez_de_mandar_a_probar(
    client: TestClient, artwork_png: bytes
):
    """«Elige otra plantilla aprobada» no es una instruccion si solo hay una."""

    client_id, campaign_id, candidatos = _ready_campaign(client, artwork_png)
    institucional = next(
        (item for item in candidatos if item["category"] == "institutional"), None
    )
    if institucional is None:
        pytest.skip("el analisis local no propuso una plantilla institucional")

    respuesta = client.post(
        f"/clients/{client_id}/campaigns/{campaign_id}/production/preview",
        files=[("matrix", _matrix_csv(
            producto="Televisor", imagen="televisor.png", precio_actual="499",
            plantilla=institucional["name"],
        ))],
    )

    assert respuesta.status_code == 200, respuesta.text
    [plan] = respuesta.json()["plans"]
    assert plan["status"] == "incompatible"
    assert plan["suggested_template"], plan
    assert plan["suggested_template"]["name"] in plan["message"]
    assert "Elige otra plantilla aprobada" not in plan["message"]
