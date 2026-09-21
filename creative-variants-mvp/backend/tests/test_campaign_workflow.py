"""Aceptación del flujo campaña → brief → propuestas aprobables.

Estos tests protegen la distinción central del producto: PDF, PPTX, PSD e
imágenes iniciales enseñan la campaña; no se convierten en artes/KV activos.
"""
from __future__ import annotations

import io
import zipfile

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app.models.campaign_production import ProductionJob
from app.services import campaign_store


def _client_and_campaign(client: TestClient) -> tuple[dict, dict]:
    created_client = client.post(
        "/clients",
        json={
            "name": "Marca de aceptación",
            "social_urls": [
                "https://instagram.com/marca",
                "https://facebook.com/marca",
                "https://instagram.com/marca",
            ],
        },
    )
    assert created_client.status_code == 201, created_client.text
    profile = created_client.json()
    created_campaign = client.post(
        f"/clients/{profile['client_id']}/campaigns",
        json={
            "name": "Temporada escolar",
            "objective": "Vender a crédito sin perder el lenguaje de la marca",
            "social_urls": ["https://tiktok.com/@marca"],
        },
    )
    assert created_campaign.status_code == 201, created_campaign.text
    return profile, created_campaign.json()


def test_cliente_se_puede_editar_y_borrar_su_biblioteca(client: TestClient):
    created = client.post(
        "/clients", json={"name": "Cliente original", "social_urls": ["https://marca.example"]}
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    changed = client.put(
        f"/clients/{profile['client_id']}",
        json={"name": "Cliente editado", "social_urls": ["https://instagram.com/cliente"]},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Cliente editado"
    assert changed.json()["social_urls"] == ["https://instagram.com/cliente"]

    deleted = client.delete(f"/clients/{profile['client_id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/clients/{profile['client_id']}").status_code == 404


def test_cliente_con_produccion_activa_no_se_puede_borrar(client: TestClient):
    profile, campaign = _client_and_campaign(client)
    campaign_store.save_production_job(
        ProductionJob(client_id=profile["client_id"], campaign_id=campaign["campaign_id"], state="PENDING")
    )
    response = client.delete(f"/clients/{profile['client_id']}")
    assert response.status_code == 409
    assert "producción en curso" in response.json()["detail"]


def _pptx_two_slides(image: bytes) -> bytes:
    """PPTX mínimo suficiente para el extractor ZIP tolerante de campaña."""

    slide = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t>'
        '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as deck:
        deck.writestr("ppt/slides/slide1.xml", slide.format(text="Concepto creativo"))
        deck.writestr("ppt/slides/slide2.xml", slide.format(text="Oferta y legales"))
        deck.writestr("ppt/media/image1.png", image)
    return output.getvalue()


def _pdf_two_pages() -> bytes:
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "Brief objetivo audiencia estrategia")
    second = document.new_page()
    second.insert_text((72, 72), "Key visual promocion precio legal")
    payload = document.tobytes()
    document.close()
    return payload


def _pdf_many_pages(count: int) -> bytes:
    document = fitz.open()
    for index in range(count):
        page = document.new_page()
        page.draw_rect(
            fitz.Rect(0, 0, page.rect.width, page.rect.height),
            color=None,
            fill=((index % 5) / 5, .15, .55),
        )
        page.insert_text((72, 72), f"Pagina visual {index + 1}", color=(1, 1, 1))
    payload = document.tobytes()
    document.close()
    return payload


def _review_and_regenerate(
    client: TestClient, profile: dict, campaign: dict, generated: dict
) -> dict:
    reviewed = client.put(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief",
        json={"objective": generated["brief"]["objective"]},
    )
    assert reviewed.status_code == 200, reviewed.text
    regenerated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False, "preserve_review": True},
    )
    assert regenerated.status_code == 200, regenerated.text
    return regenerated.json()


def test_documentos_son_fuentes_de_una_campana_y_no_multiples_kv(
    client: TestClient, artwork_png: bytes
):
    profile, campaign = _client_and_campaign(client)
    projects_before = {item["project_id"] for item in client.get("/projects").json()}

    response = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[
            (
                "files",
                ("concepto.pptx", _pptx_two_slides(artwork_png),
                 "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ),
            ("files", ("referencia.png", artwork_png, "image/png")),
        ],
    )

    assert response.status_code == 201, response.text
    sources = response.json()["sources"]
    # Dos archivos de entrada son dos fuentes. Las dos diapositivas y el PNG
    # incrustado son evidencia de la primera, nunca tres KV adicionales.
    assert len(sources) == 2
    presentation = next(item for item in sources if item["filename"] == "concepto.pptx")
    assert presentation["kind"] == "presentation"
    assert presentation["page_count"] == 2
    assert "Concepto creativo" in presentation["extracted_text"]
    assert len(presentation["asset_files"]) == 1
    assert [item.rsplit("/", 1)[-1] for item in presentation["preview_files"]] == [
        "slide-001.jpg",
        "slide-002.jpg",
    ]

    projects_after = {item["project_id"] for item in client.get("/projects").json()}
    assert projects_after == projects_before

    stored = client.get(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
    )
    assert stored.status_code == 200, stored.text
    assert {item["filename"] for item in stored.json()["sources"]} == {
        "concepto.pptx",
        "referencia.png",
    }


def test_quitar_fuente_invalida_el_brief_y_elimina_su_evidencia(
    client: TestClient, artwork_png: bytes
):
    profile, campaign = _client_and_campaign(client)
    uploaded = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("referencia.png", artwork_png, "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    source_id = uploaded.json()["sources"][0]["source_id"]
    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert generated.status_code == 200, generated.text

    removed = client.delete(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources/{source_id}"
    )
    assert removed.status_code == 204, removed.text
    campaign_after = client.get(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
    )
    assert campaign_after.status_code == 200, campaign_after.text
    assert campaign_after.json()["sources"] == []
    assert campaign_after.json()["brief"] is None
    assert campaign_after.json()["template_candidates"] == []

    missing = client.delete(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources/{source_id}"
    )
    assert missing.status_code == 404


def test_no_se_puede_borrar_contexto_mientras_una_produccion_esta_en_cola(
    client: TestClient, artwork_png: bytes
):
    profile, campaign = _client_and_campaign(client)
    uploaded = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("referencia.png", artwork_png, "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    source_id = uploaded.json()["sources"][0]["source_id"]
    campaign_store.save_production_job(
        ProductionJob(
            client_id=profile["client_id"],
            campaign_id=campaign["campaign_id"],
            state="PENDING",
        )
    )

    removed = client.delete(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources/{source_id}"
    )

    assert removed.status_code == 409, removed.text
    assert "producción en curso" in removed.json()["detail"]


def test_pdf_de_varias_paginas_sigue_siendo_una_sola_fuente(client: TestClient):
    profile, campaign = _client_and_campaign(client)
    before = len(client.get("/projects").json())

    response = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("brief.pdf", _pdf_two_pages(), "application/pdf"))],
    )

    assert response.status_code == 201, response.text
    assert len(response.json()["sources"]) == 1
    source = response.json()["sources"][0]
    assert source["kind"] == "pdf"
    assert source["page_count"] == 2
    assert len(source["preview_files"]) == 2
    assert len(client.get("/projects").json()) == before


def test_pdf_largo_muestrea_inicio_medio_y_cierre(client: TestClient):
    profile, campaign = _client_and_campaign(client)
    response = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("toolkit.pdf", _pdf_many_pages(25), "application/pdf"))],
    )

    assert response.status_code == 201, response.text
    source = response.json()["sources"][0]
    assert len(source["preview_files"]) == 16
    pages = source["meta"]["previewed_page_numbers"]
    assert pages[0] == 1
    assert pages[-1] == 25
    assert any(page > 16 for page in pages)
    assert source["preview_files"][-1].endswith("page-025.jpg")


def test_brief_offline_propone_tres_a_cinco_plantillas_sin_productos_reales(
    client: TestClient,
):
    profile, campaign = _client_and_campaign(client)
    uploaded = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[
            (
                "files",
                (
                    "brief.txt",
                    b"Objetivo: vender televisores. Concepto: tecnologia cercana. "
                    b"Usar precio, cuota, CTA y legal. Combos permitidos.",
                    "text/plain",
                ),
            )
        ],
    )
    assert uploaded.status_code == 201, uploaded.text
    source_ids = {item["source_id"] for item in uploaded.json()["sources"]}

    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )

    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert body["engine"] == "deterministic"
    assert 3 <= len(body["template_candidates"]) <= 5
    assert body["brief"]["objective"]
    product_candidates = 0
    for candidate in body["template_candidates"]:
        assert candidate["status"] == "proposed"
        assert set(candidate["source_ids"]) <= source_ids
        assert candidate["supported_aspects"]
        slots = candidate["slots"]
        products = [slot for slot in slots if slot["category"] == "product"]
        # La biblioteca mezcla masters de producto con una pieza institucional
        # válida sin foto. Lo importante para una campaña comercial es que al
        # menos una candidata soporte producto, no forzar una foto ficticia en
        # cada composición de marca.
        if products:
            product_candidates += 1
            assert all(slot["required"] is True for slot in products)
        else:
            assert candidate["supported_product_count"]["minimum"] == 0
        assert all(
            slot["required"] is False
            for slot in slots
            if slot["category"] != "product"
        )
        # Una candidata define huecos y reglas; no lleva el televisor de la
        # referencia ni copy rasterizado dentro.
        assert all("content" not in slot and "image" not in slot for slot in slots)
    assert product_candidates >= 1

    persisted = client.get(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
    ).json()
    assert persisted["brief"] == body["brief"]
    assert [item["candidate_id"] for item in persisted["template_candidates"]] == [
        item["candidate_id"] for item in body["template_candidates"]
    ]


def test_aprobacion_se_persiste_en_la_campana_y_en_la_memoria_del_cliente(
    client: TestClient,
):
    profile, campaign = _client_and_campaign(client)
    source = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("direccion.md", b"# Campana\nProducto y precio", "text/markdown"))],
    )
    assert source.status_code == 201, source.text
    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    ).json()
    generated = _review_and_regenerate(client, profile, campaign, generated)
    candidate = generated["template_candidates"][0]

    approved = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
        f"/template-candidates/{candidate['candidate_id']}/approve",
        json={"notes": "Aprobada después de revisar espacios y formatos"},
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["candidate"]["status"] == "approved"
    assert approved.json()["candidate"]["approved"] is True
    assert approved.json()["candidate"]["approved_at"]

    campaign_again = client.get(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
    ).json()
    saved = next(
        item
        for item in campaign_again["template_candidates"]
        if item["candidate_id"] == candidate["candidate_id"]
    )
    assert saved["status"] == "approved"
    client_again = client.get(f"/clients/{profile['client_id']}")
    assert client_again.status_code == 200, client_again.text
    assert client_again.json()["approved_candidates"] >= 1


def test_no_se_puede_aprobar_una_plantilla_sin_revisar_el_brief(
    client: TestClient,
):
    profile, campaign = _client_and_campaign(client)
    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert generated.status_code == 200, generated.text
    candidate = generated.json()["template_candidates"][0]

    approval = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
        f"/template-candidates/{candidate['candidate_id']}/approve",
        json={"notes": "Todavia no revise el brief"},
    )

    assert approval.status_code == 409
    assert "brief" in approval.json()["detail"].lower()


def test_corregir_plantilla_redibuja_blueprint_y_exige_aprobacion_nueva(
    client: TestClient,
):
    profile, campaign = _client_and_campaign(client)
    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    ).json()
    generated = _review_and_regenerate(client, profile, campaign, generated)
    candidate = generated["template_candidates"][0]
    approved = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
        f"/template-candidates/{candidate['candidate_id']}/approve",
        json={"notes": "Base aprobada"},
    )
    assert approved.status_code == 200, approved.text

    corrected = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
        f"/template-candidates/{candidate['candidate_id']}/revise",
        json={"notes": "Quiero un diseño más limpio, minimal y con más aire"},
    )

    assert corrected.status_code == 200, corrected.text
    candidates = corrected.json()["template_candidates"]
    changed = next(item for item in candidates if item["category"] == candidate["category"])
    assert changed["status"] == "proposed"
    assert changed["approved"] is False
    assert changed["revision_hash"] != candidate["revision_hash"]
    assert changed["blueprint"]["accent_style"] == "minimal"
    assert changed["blueprint"]["density"] == "airy"


def test_material_nuevo_exige_reanalisis_y_una_aprobacion_nueva(
    client: TestClient,
):
    profile, campaign = _client_and_campaign(client)
    base = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("brief.txt", b"Objetivo: vender tecnologia", "text/plain"))],
    )
    assert base.status_code == 201, base.text
    analysis = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    ).json()
    analysis = _review_and_regenerate(client, profile, campaign, analysis)
    candidate = analysis["template_candidates"][0]
    approved = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
        f"/template-candidates/{candidate['candidate_id']}/approve",
        json={"notes": "Aprobada con el material disponible"},
    )
    assert approved.status_code == 200, approved.text

    added = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[
            (
                "files",
                (
                    "manual.txt",
                    b"Manual de marca: usar siempre el sello de garantia y fondo oscuro",
                    "text/plain",
                ),
            )
        ],
    )
    assert added.status_code == 201, added.text
    assert any("aprueba" in warning for warning in added.json()["warnings"])

    refreshed = client.get(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
    ).json()
    assert refreshed["status"] == "ready_for_brief"
    assert refreshed["brief"] is None
    assert refreshed["template_candidates"] == []

    regenerated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert regenerated.status_code == 200, regenerated.text
    assert all(
        item["status"] == "proposed" and item["approved"] is False
        for item in regenerated.json()["template_candidates"]
    )


def test_cliente_y_campana_no_desaparecen_al_barrer_proyectos(
    client: TestClient, artwork_png: bytes
):
    profile, campaign = _client_and_campaign(client)
    uploaded = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/sources",
        files=[("files", ("referencia.png", artwork_png, "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text

    purged = client.post("/projects/purge")
    assert purged.status_code == 200, purged.text
    assert client.get(f"/clients/{profile['client_id']}").status_code == 200
    recovered = client.get(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}"
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["sources"][0]["filename"] == "referencia.png"


def test_correcciones_del_brief_son_permanentes_y_sobreviven_reanalisis(
    client: TestClient,
):
    profile, campaign = _client_and_campaign(client)
    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert generated.status_code == 200, generated.text

    revised = client.put(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief",
        json={
            "objective": "Priorizar combos de regreso a clases",
            "audience": "Familias con hijos en edad escolar",
            "primary_message": "Todo para clases con crédito directo",
            "headline_style": "Titular corto y de alto contraste",
            "cta_style": "Solo cuando exista una acción verificable",
            "legal_requirements": ["Válido del 1 al 30 de septiembre."],
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["primary_message"] == "Todo para clases con crédito directo"
    assert revised.json()["legal_requirements"] == ["Válido del 1 al 30 de septiembre."]

    regenerated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()["brief"]["objective"] == "Priorizar combos de regreso a clases"
    assert regenerated.json()["brief"]["audience"] == "Familias con hijos en edad escolar"
    assert regenerated.json()["brief"]["primary_message"] == "Todo para clases con crédito directo"
    assert regenerated.json()["brief"]["headline_style"] == "Titular corto y de alto contraste"
    assert regenerated.json()["brief"]["cta_style"] == "Solo cuando exista una acción verificable"
    assert regenerated.json()["brief"]["legal_requirements"] == ["Válido del 1 al 30 de septiembre."]
    knowledge = client.get(f"/clients/{profile['client_id']}").json()
    assert any("Correccion manual" in item for item in knowledge["learned_rules"])


def test_se_pueden_corregir_varias_redes_sin_recrear_la_campana(client: TestClient):
    profile, campaign = _client_and_campaign(client)
    generated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert generated.status_code == 200, generated.text

    urls = [
        "https://instagram.com/marca",
        "https://facebook.com/marca",
        "https://www.marca.example/campana",
    ]
    updated = client.put(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}",
        json={"social_urls": urls},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["social_urls"] == urls
    assert updated.json()["brief"] is None
    assert updated.json()["template_candidates"] == []
    assert updated.json()["status"] == "ready_for_brief"
