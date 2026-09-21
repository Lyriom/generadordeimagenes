from __future__ import annotations

import pytest

from app.services import public_references


def test_una_pared_de_login_no_se_convierte_en_posts_ni_logo_social():
    html = """
    <html><head>
      <meta property="og:title" content="Instagram · Log in">
      <meta property="og:image" content="https://cdn.example/instagram-logo.png">
    </head><body><img src="https://cdn.example/profile_pic.jpg"></body></html>
    """

    assert public_references.public_image_urls(
        html, "https://instagram.com/marca", login_wall=True
    ) == []


def test_un_perfil_social_no_usa_su_og_image_como_si_fuera_un_post(monkeypatch):
    html = """
    <meta property="og:image" content="https://cdn.instagram.example/avatar-de-marca.jpg">
    <img src="https://cdn.instagram.example/posts/campana-001.jpg">
    """
    monkeypatch.setattr(public_references, "ensure_public_host", lambda _host: None)

    assert public_references.public_image_urls(
        html, "https://instagram.com/marca"
    ) == ["https://cdn.instagram.example/posts/campana-001.jpg"]


def test_filtra_iconos_pero_conserva_una_pieza_publica(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(public_references, "ensure_public_host", lambda _host: None)
    html = """
    <meta property="og:image" content="/assets/facebook-logo.png">
    <img src="/assets/avatar-marca.jpg">
    <img src="https://cdn.example/posts/campana-001.jpg">
    """

    assert public_references.public_image_urls(
        html, "https://marca.example/perfil"
    ) == ["https://cdn.example/posts/campana-001.jpg"]


def test_rechaza_hosts_locales_para_evitar_ssrf():
    with pytest.raises(public_references.PublicReferenceError):
        public_references.ensure_public_host("127.0.0.1")
