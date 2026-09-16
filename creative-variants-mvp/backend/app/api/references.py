"""Lectura segura y mínima de referencias públicas de una campaña."""
from __future__ import annotations

import ipaddress
import re
import socket
from html import unescape
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl

from .deps import bind_session

router = APIRouter(
    prefix="/references", tags=["referencias"], dependencies=[Depends(bind_session)]
)


class InspectRequest(BaseModel):
    url: HttpUrl


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


@router.post("/inspect")
def inspect_reference(request: InspectRequest) -> dict[str, str]:
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
    }
