"""Las plantillas fallback deben seguir la evidencia, incluso sin OpenAI."""
from __future__ import annotations

from app.models.campaign import Campaign, CampaignBrief, CampaignSource, CampaignSourceRole
from app.services import campaign_analysis
from app.services.campaign_analysis import deterministic_candidates


def _source(*, text: str, roles: list[CampaignSourceRole]) -> CampaignSource:
    return CampaignSource(
        filename="brief.txt",
        kind="document",
        extension=".txt",
        size_bytes=len(text.encode()),
        sha256="a" * 64,
        stored_path="sources/brief/original.txt",
        extracted_text=text,
        roles=roles,
    )


def test_fallback_no_inventa_precio_ni_combo_si_el_brief_no_los_sustenta():
    campaign = Campaign(
        client_id="cliente",
        name="Posicionamiento",
        objective="Fortalecer la recordación de marca",
        sources=[
            _source(
                text="Objetivo: comunicar confianza y cercanía de la marca.",
                roles=[CampaignSourceRole.STRATEGY, CampaignSourceRole.BRAND_MANUAL],
            )
        ],
    )
    brief = CampaignBrief(
        objective=campaign.objective,
        primary_message="Una marca más cerca de las familias.",
    )

    candidates = deterministic_candidates(campaign, brief)

    assert 3 <= len(candidates) <= 5
    assert "price_promotion" not in {candidate.category for candidate in candidates}
    assert "combo" not in {candidate.category for candidate in candidates}
    institutional = next(candidate for candidate in candidates if candidate.category == "institutional")
    assert not [slot for slot in institutional.slots if slot.category == "product"]
    assert institutional.supported_product_count.minimum == 0
    assert institutional.supported_product_count.maximum == 0


def test_fallback_solo_ofrece_precio_y_combo_cuando_los_menciona_la_campana():
    campaign = Campaign(
        client_id="cliente",
        name="Regreso a clases",
        objective="Vender combos con precio especial y cuotas sin intereses",
        sources=[
            _source(
                text="Oferta: combo de laptop y mochila. Precio $499. Cuotas disponibles.",
                roles=[CampaignSourceRole.STRATEGY, CampaignSourceRole.PRODUCT_REFERENCE],
            )
        ],
    )
    brief = CampaignBrief(objective=campaign.objective)

    categories = {candidate.category for candidate in deterministic_candidates(campaign, brief)}

    assert {"single_product", "price_promotion", "combo"} <= categories


def test_master_de_producto_y_pieza_institucional_no_descartan_campos_de_matriz():
    campaign = Campaign(
        client_id="cliente",
        name="Lanzamiento",
        objective="Vender una nueva línea de productos",
        sources=[
            _source(
                text="Producto nuevo disponible. Comunicar sus beneficios principales.",
                roles=[CampaignSourceRole.STRATEGY, CampaignSourceRole.PRODUCT_REFERENCE],
            )
        ],
    )
    candidates = deterministic_candidates(campaign, CampaignBrief(objective=campaign.objective))

    master = next(candidate for candidate in candidates if candidate.name == "Producto protagonista")
    assert {
        "producto", "precio", "precio_anterior", "cuota", "descuento", "cta", "vigencia", "legal",
    } <= {slot.key for slot in master.slots}
    assert all(
        slot.hide_when_empty
        for slot in master.slots
        if slot.key in {"precio", "precio_anterior", "cuota", "descuento", "cta", "vigencia", "legal"}
    )

    institutional = next(candidate for candidate in candidates if candidate.category == "institutional")
    assert institutional.supported_product_count.minimum == 0
    assert institutional.supported_product_count.maximum == 0


def test_referencia_social_bloqueada_queda_visible_pero_no_se_usa_como_post(
    monkeypatch,
):
    campaign = Campaign(
        client_id="cliente",
        name="Campaña",
        social_urls=["https://instagram.com/marca"],
    )
    monkeypatch.setattr(
        campaign_analysis,
        "inspect_public_url",
        lambda _url, timeout: {
            "url": "https://instagram.com/marca",
            "title": "Instagram · Log in",
            "description": "Create an account or log in",
            "posts": [],
            "accessible": False,
            "blocked_reason": "login_wall",
        },
    )

    warnings = campaign_analysis._collect_social_evidence(campaign)

    [evidence] = campaign.meta["social_evidence"]
    assert evidence["accessible"] is False
    assert evidence["posts"] == []
    assert evidence["blocked_reason"] == "login_wall"
    assert any("capturas" in warning for warning in warnings)
