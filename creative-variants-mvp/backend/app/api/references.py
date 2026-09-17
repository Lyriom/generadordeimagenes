"""Lectura segura y mínima de referencias públicas de una campaña."""
from __future__ import annotations

import ipaddress
import re
import socket
from html import unescape
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl

from ..config import settings
from .deps import bind_session

router = APIRouter(
    prefix="/references", tags=["referencias"], dependencies=[Depends(bind_session)]
)


class InspectRequest(BaseModel):
    url: HttpUrl


class ProfileAnalysisRequest(BaseModel):
    profile_url: HttpUrl
    images: list[HttpUrl] = []


def _public_host(host: str) -> None:
    """No permitir que una URL de referencia se use para leer la red interna."""
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(422, "No se pudo resolver el sitio de referencia.") from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise HTTPException(400, "La referencia debe ser un sitio público.")


def _meta(html: str, name: str) -> str:
    # Cubre property="og:image" y name="description" sin intentar interpretar
    # toda la página. Es deliberadamente una vista de dirección, no un scraper.
    pattern = r'<meta[^>]+(?:property|name)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']+)' 
    match = re.search(pattern, html, flags=re.I)
    return unescape(match.group(1)).strip()[:500] if match else ""


def _public_image_urls(html: str, page_url: str) -> list[str]:
    """Recoge una muestra de piezas visuales expuestas públicamente en la página."""
    candidates = [_meta(html, "og:image"), _meta(html, "twitter:image")]
    candidates.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)', html, flags=re.I))
    clean: list[str] = []
    for item in candidates:
        url = urljoin(page_url, unescape(item).strip())
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        try:
            _public_host(parsed.hostname)
        except HTTPException:
            continue
        # Las redes suelen exponer su propio logo, favicon y botones antes que
        # cualquier post. No son piezas creativas y contaminan la guía de IA.
        path = parsed.path.lower()
        if any(token in path for token in ("favicon", "logo", "icon", "instagram", "fb_logo")):
            continue
        if url not in clean:
            clean.append(url)
        if len(clean) == 12:
            break
    return clean


@router.post("/inspect")
def inspect_reference(request: InspectRequest) -> dict[str, object]:
    url = str(request.url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Usa una URL pública http o https.")
    _public_host(parsed.hostname)
    try:
        req = Request(url, headers={"User-Agent": "MisivaCreativeStudio/1.0"})
        with urlopen(req, timeout=8) as response:  # noqa: S310 - host validated above
            raw = response.read(750_000)
            content_type = response.headers.get_content_type()
    except Exception as exc:  # el sitio puede bloquear bots; se conserva la URL
        raise HTTPException(422, "No se pudo leer esta referencia pública.") from exc
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise HTTPException(422, "La referencia debe ser una página web, no un archivo.")
    html = raw.decode("utf-8", errors="ignore")
    title = _meta(html, "og:title") or _meta(html, "twitter:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        title = unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:180] if match else parsed.hostname
    return {
        "url": url,
        "title": title,
        "description": _meta(html, "og:description") or _meta(html, "description"),
        "image": _meta(html, "og:image") or _meta(html, "twitter:image"),
        "posts": _public_image_urls(html, url),
    }


@router.post("/profile-analysis")
def analyze_profile(request: ProfileAnalysisRequest) -> dict[str, str | list[str]]:
    """Pide a OpenAI una guía práctica a partir de los posts públicos hallados."""
    if not settings.openai_api_key:
        raise HTTPException(409, "No hay OPENAI_API_KEY configurada para analizar el perfil.")
    images = [str(url) for url in request.images[:8]]
    if not images:
        raise HTTPException(422, "No encontramos imágenes públicas del perfil. Sube capturas de sus posts.")
    content: list[dict] = [{
        "type": "text",
        "text": (
            "Analiza estos posts públicos como director de arte. Devuelve una guía breve en español "
            "para crear plantillas coherentes: paleta, tipografías aparentes, composición, tratamiento "
            "de producto, estilo de copy, CTA y elementos que deben permanecer fijos. No inventes datos."
        ),
    }]
    content.extend({"type": "image_url", "image_url": {"url": image, "detail": "low"}} for image in images)
    try:
        with __import__("httpx").Client(timeout=25) as client:
            response = client.post(
                settings.openai_vision_endpoint,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": settings.openai_vision_model, "messages": [{"role": "user", "content": content}]},
            )
        if response.status_code >= 400:
            raise HTTPException(502, "OpenAI no pudo analizar las referencias.")
        data = response.json()
        guide = str(data["choices"][0]["message"]["content"] or "").strip()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "No se pudo completar el análisis visual.") from exc
    return {"profile_url": str(request.profile_url), "guide": guide[:4000], "images": images}
