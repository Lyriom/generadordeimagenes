"""Lectura del feed real de Instagram vía Social Tools.

Un perfil de Instagram no entrega las imagenes de su feed sin sesion iniciada.
Social Tools si, y es la unica via que teniamos para que el brief analice como
compone la marca sus propias piezas. Las respuestas se simulan con el transporte
de httpx: la API es de pago, esta limitada por IP y no debe llamarse en la suite.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.services import campaign_analysis, social_tools
from app.models.campaign import Campaign


@pytest.fixture(autouse=True)
def _credenciales(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "socialtools_login", "equipo@misiva.com.ec")
    monkeypatch.setattr(settings, "socialtools_password", "secreta")
    monkeypatch.setattr(settings, "data_dir", tmp_path)


def _transporte(manejador):
    """Sustituye httpx.Client por uno con transporte simulado."""

    original = httpx.Client

    class Cliente(original):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(manejador)
            kwargs.pop("trust_env", None)
            super().__init__(*args, **kwargs)

    return Cliente


# Campos reales de GetPages, verificados contra el mapeador del servicio
# socialtools-explorer y su catalogo en disco: el sobre es "export", el enlace
# "lien_page" y la audiencia "nbFollowers_page". La ficha de Marcimex es la que
# trae ese catalogo (id 38790, 150.202 seguidores).
CATALOGO = {
    "export": [
        {
            "id_page": 1, "nom_page": "Coca-Cola", "nom_pays": "INTERNATIONAL",
            "lien_page": "http://instagram.com/cocacola", "nbFollowers_page": 3342960,
        },
        {
            "id_page": 38790, "nom_page": "Marcimex", "nom_pays": "ECUADOR",
            "lien_page": "http://instagram.com/marcimexec", "nbFollowers_page": 150202,
        },
    ]
}

FEED = {
    "account": [
        {
            "Id": 38790,
            "Name": "Marcimex",
            "Dates": [
                {
                    "Date": "2026-09-18",
                    "Posts": [
                        {
                            "IdIg": "a1", "Date": "10:02", "Text": "Cyber days",
                            "Likes": 120, "Comments": 8,
                            "Link": "https://instagram.com/p/a1/",
                            "Url_media": "https://scontent.cdninstagram.com/a1.jpg",
                        }
                    ],
                },
                {
                    "Date": "2026-09-20",
                    "Posts": [
                        {
                            "IdIg": "b2", "Date": "09:15", "Text": "Sala nueva",
                            "Likes": 300, "Comments": 21,
                            "Link": "https://instagram.com/p/b2/",
                            "Url_media": "https://scontent.cdninstagram.com/b2.jpg",
                        },
                        {
                            # Sin imagen: no sirve como evidencia visual.
                            "IdIg": "c3", "Text": "solo texto", "Url_media": "",
                        },
                    ],
                },
            ],
        }
    ]
}


def _api(request: httpx.Request) -> httpx.Response:
    # Las credenciales viajan SIEMPRE en el cuerpo, nunca en la URL.
    cuerpo = request.content.decode()
    assert "login=" in cuerpo and "password=" in cuerpo
    assert "password" not in str(request.url)
    if request.url.path.endswith("/Instagram/GetPages.php"):
        return httpx.Response(200, json=CATALOGO)
    if request.url.path.endswith("/Instagram/Data.php"):
        assert request.url.params["idPage"] == "38790"
        assert request.url.params["Post"] == "1"
        return httpx.Response(200, json=FEED)
    return httpx.Response(404, json={})


def test_un_perfil_de_instagram_entrega_las_imagenes_de_su_feed(monkeypatch):
    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_api))

    lectura = social_tools.profile_posts("https://www.instagram.com/marcimexec/?hl=es")

    assert lectura is not None
    assert lectura["handle"] == "marcimexec"
    assert lectura["account"]["id"] == 38790
    assert lectura["account"]["name"] == "Marcimex"
    # Las mas nuevas primero, y la publicacion sin imagen no cuenta.
    assert [item["media"] for item in lectura["posts"]] == [
        "https://scontent.cdninstagram.com/b2.jpg",
        "https://scontent.cdninstagram.com/a1.jpg",
    ]


def test_un_enlace_a_publicacion_no_gasta_una_llamada_de_api(monkeypatch):
    """Ese camino ya funciona por og:image; la API es de pago."""

    def _prohibido(_request):
        raise AssertionError("no debia llamarse a Social Tools")

    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_prohibido))

    assert social_tools.profile_posts("https://www.instagram.com/reel/DcwyqeiONy4/") is None
    assert social_tools.profile_posts("https://www.instagram.com/p/abc/") is None
    assert social_tools.profile_posts("https://facebook.com/marcimex") is None


def test_una_cuenta_fuera_del_catalogo_lo_dice_en_vez_de_callar(monkeypatch):
    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_api))

    lectura = social_tools.profile_posts("https://instagram.com/marca_que_no_existe")

    assert lectura is not None
    assert lectura["posts"] == []
    assert lectura["reason"] == "sin_cobertura"
    assert "marca_que_no_existe" in lectura["detail"]


def test_el_catalogo_se_guarda_en_disco_y_no_se_vuelve_a_pedir(monkeypatch, tmp_path):
    llamadas: list[str] = []

    def _contando(request: httpx.Request) -> httpx.Response:
        llamadas.append(request.url.path)
        return _api(request)

    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_contando))

    social_tools.profile_posts("https://instagram.com/marcimexec")
    social_tools.profile_posts("https://instagram.com/marcimexec")

    assert llamadas.count("/Instagram/GetPages.php") == 1, "el catalogo debe cachearse"
    assert llamadas.count("/Instagram/Data.php") == 2
    guardado = json.loads((tmp_path / "social_tools" / "instagram-catalog.json").read_text())
    assert any(item["id"] == 38790 for item in guardado["entries"])


def test_sin_credenciales_no_se_llama_a_la_api_y_la_campana_sigue(monkeypatch):
    monkeypatch.setattr(settings, "socialtools_login", None)

    def _prohibido(_request):
        raise AssertionError("no debia llamarse sin credenciales")

    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_prohibido))

    assert social_tools.available() is False
    lectura = social_tools.profile_posts("https://instagram.com/marcimexec")
    assert lectura is not None and lectura["reason"] == "no_configurado"


def test_el_analisis_usa_el_feed_real_en_vez_del_avatar_del_perfil(monkeypatch):
    """El perfil deja de quedarse sin posts cuando Social Tools lo cubre."""

    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_api))

    def _no_deberia(_url, timeout):  # el lector publico no hace falta aqui
        raise AssertionError("Social Tools debia resolver este perfil")

    monkeypatch.setattr(campaign_analysis, "inspect_public_url", _no_deberia)
    campaign = Campaign(
        client_id="cliente",
        name="Cyber",
        social_urls=["https://www.instagram.com/marcimexec/"],
    )

    avisos = campaign_analysis._collect_social_evidence(campaign)

    [evidencia] = campaign.meta["social_evidence"]
    assert evidencia["accessible"] is True
    assert evidencia["source"] == "socialtools"
    assert len(evidencia["posts"]) == 2
    assert any("Social Tools" in aviso for aviso in avisos)


def test_una_url_que_social_tools_no_cubre_sigue_por_el_lector_publico(monkeypatch):
    monkeypatch.setattr(social_tools.httpx, "Client", _transporte(_api))
    leidas: list[str] = []

    def _publico(url, timeout):
        leidas.append(url)
        return {
            "url": url, "title": "Marcimex", "description": "",
            "posts": ["https://marcimex.com/banner.jpg"],
            "accessible": True, "blocked_reason": "",
        }

    monkeypatch.setattr(campaign_analysis, "inspect_public_url", _publico)
    campaign = Campaign(
        client_id="cliente", name="Cyber",
        social_urls=["https://www.marcimex.com/"],
    )

    campaign_analysis._collect_social_evidence(campaign)

    assert leidas == ["https://www.marcimex.com/"]
    [evidencia] = campaign.meta["social_evidence"]
    assert evidencia["posts"] == ["https://marcimex.com/banner.jpg"]
