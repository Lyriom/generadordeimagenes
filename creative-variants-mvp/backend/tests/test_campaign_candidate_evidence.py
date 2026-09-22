"""Las plantillas fallback deben seguir la evidencia, incluso sin OpenAI."""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.campaign import Campaign, CampaignBrief, CampaignSource, CampaignSourceRole
from app.models.template import Brand
from app.models.campaign import ClientKnowledge
from app.services import campaign_analysis
from app.services.campaign_analysis import deterministic_candidates
from app.services.production_matrix import MatrixRow, select_template


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
    assert master.supported_product_count.minimum == 1
    assert master.supported_product_count.maximum == 4
    assert next(slot for slot in master.slots if slot.key == "producto").repeatable is True
    assert all(
        slot.hide_when_empty
        for slot in master.slots
        if slot.key in {"precio", "precio_anterior", "cuota", "descuento", "cta", "vigencia", "legal"}
    )

    institutional = next(candidate for candidate in candidates if candidate.category == "institutional")
    assert institutional.supported_product_count.minimum == 0
    assert institutional.supported_product_count.maximum == 0


def test_master_de_producto_admite_un_arte_grupal_aun_si_el_brief_no_menciono_combo():
    campaign = Campaign(
        client_id="cliente",
        name="Lanzamiento",
        objective="Presentar productos de la colección",
        sources=[
            _source(
                text="Producto nuevo disponible. Comunicar beneficios.",
                roles=[CampaignSourceRole.STRATEGY, CampaignSourceRole.PRODUCT_REFERENCE],
            )
        ],
    )
    candidates = deterministic_candidates(campaign, CampaignBrief(objective=campaign.objective))
    master = next(candidate for candidate in candidates if candidate.name == "Producto protagonista")
    row = MatrixRow(
        row_number=2,
        producto="Silla | Mesa | Lámpara",
        imagen="silla.png | mesa.png | lampara.png",
    )

    assert select_template(row, [master]) is master


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
    # El aviso manda al camino que SI funciona. Comprobado el 2026-09-22: un
    # perfil no entrega su feed sin sesion, pero instagram.com/reel/... si
    # devuelve su imagen, con o sin token de compartir.
    aviso = " ".join(warnings)
    assert "/reel/" in aviso or "reel" in aviso


def test_un_perfil_legible_sin_posts_no_manda_a_pegar_enlaces_de_publicacion(
    monkeypatch,
):
    """El aviso no puede proponer un camino que no existe.

    Ni el perfil ni el enlace a una publicacion concreta entregan imagenes a
    quien no ha iniciado sesion: Instagram y Facebook devuelven la aplicacion en
    JavaScript. Pedir "enlaces directos a publicaciones" mandaba a un callejon
    sin salida; la unica via real es subir capturas o los artes.
    """
    campaign = Campaign(
        client_id="cliente",
        name="Campaña",
        social_urls=["https://facebook.com/marca"],
    )
    monkeypatch.setattr(
        campaign_analysis,
        "inspect_public_url",
        lambda _url, timeout: {
            "url": "https://facebook.com/marca",
            "title": "Marca",
            "description": "422.458 followers · Conectamos la tecnologia a tus manos",
            "posts": [],
            "accessible": True,
            "blocked_reason": "",
        },
    )

    warnings = campaign_analysis._collect_social_evidence(campaign)

    [evidence] = campaign.meta["social_evidence"]
    # El perfil si aporta contexto: nombre, biografia y comunidad.
    assert evidence["accessible"] is True
    assert evidence["posts"] == []
    aviso = " ".join(warnings)
    assert "Material de campaña" in aviso
    assert "enlaces directos" not in aviso
    assert "enlaces publicos" not in aviso


def test_el_analisis_reintenta_una_vez_antes_de_rendirse_al_brief_local(monkeypatch):
    """Un corte no da un brief peor: da uno que no miro ninguna vista.

    La llamada de vision dura minutos y manda varias imagenes; un timeout suelto
    es el fallo normal, no una señal de que OpenAI no sirva. Se reintenta una vez
    antes de caer al analisis local.
    """
    import httpx

    intentos: list[int] = []

    class RespuestaFalsa:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {"message": {"content": '{"brief": {}, "template_candidates": []}'}}
                ]
            }

    class ClienteFalso:
        def __init__(self, *_args, **kwargs):
            self.timeout = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, *_args, **_kwargs):
            intentos.append(1)
            if len(intentos) == 1:
                raise httpx.ReadTimeout("tardo demasiado")
            # El ajuste dedicado manda: sin el, el tope quedaba en 60 s.
            assert self.timeout == settings.campaign_analysis_timeout
            return RespuestaFalsa()

    monkeypatch.setattr(campaign_analysis.httpx, "Client", ClienteFalso)
    monkeypatch.setattr(settings, "openai_api_key", "clave-de-prueba")
    campaign = Campaign(client_id="cliente", name="Campaña")
    brand = Brand(name="Marca")
    fallback = campaign_analysis.deterministic_brief(
        campaign, brand, ClientKnowledge(client_id="cliente")
    )

    brief, candidatas = campaign_analysis._openai_analysis(
        campaign,
        brand,
        ClientKnowledge(client_id="cliente"),
        fallback,
        deterministic_candidates(campaign, fallback),
    )

    assert len(intentos) == 2, "un timeout debe reintentarse una vez"
    # Y el reintento sirve de algo: devuelve el analisis, no el de respaldo.
    assert brief is not None
    assert len(candidatas) >= 3


def test_un_http_de_error_no_se_reintenta(monkeypatch):
    """Repetir un 4xx/5xx no cambia la respuesta y alarga la espera."""
    import httpx

    intentos: list[int] = []

    class ClienteFalso:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, *_args, **_kwargs):
            intentos.append(1)
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", "https://api.openai.com"),
                response=httpx.Response(429),
            )

    monkeypatch.setattr(campaign_analysis.httpx, "Client", ClienteFalso)
    monkeypatch.setattr(settings, "openai_api_key", "clave-de-prueba")
    campaign = Campaign(client_id="cliente", name="Campaña")
    brand = Brand(name="Marca")
    fallback = campaign_analysis.deterministic_brief(
        campaign, brand, ClientKnowledge(client_id="cliente")
    )

    with pytest.raises(httpx.HTTPStatusError):
        campaign_analysis._openai_analysis(
            campaign,
            brand,
            ClientKnowledge(client_id="cliente"),
            fallback,
            deterministic_candidates(campaign, fallback),
        )

    assert len(intentos) == 1, "un HTTP de error no debe reintentarse"
