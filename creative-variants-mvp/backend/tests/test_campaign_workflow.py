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
    for candidate in body["template_candidates"]:
        assert candidate["status"] == "proposed"
        assert set(candidate["source_ids"]) <= source_ids
        assert candidate["supported_aspects"]
        slots = candidate["slots"]
        products = [slot for slot in slots if slot["category"] == "product"]
        assert products, candidate
        assert all(slot["required"] is True for slot in products)
        assert all(
            slot["required"] is False
            for slot in slots
            if slot["category"] != "product"
        )
        # Una candidata define huecos y reglas; no lleva el televisor de la
        # referencia ni copy rasterizado dentro.
        assert all("content" not in slot and "image" not in slot for slot in slots)

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
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["primary_message"] == "Todo para clases con crédito directo"

    regenerated = client.post(
        f"/clients/{profile['client_id']}/campaigns/{campaign['campaign_id']}/brief/generate",
        json={"use_ai": False},
    )
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()["brief"]["objective"] == "Priorizar combos de regreso a clases"
    assert regenerated.json()["brief"]["audience"] == "Familias con hijos en edad escolar"
    assert regenerated.json()["brief"]["primary_message"] == "Todo para clases con crédito directo"
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
