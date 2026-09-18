"""Lectura segura de perfiles y sitios publicos usados como evidencia creativa."""
from __future__ import annotations

import ipaddress
import re
import socket
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx


class PublicReferenceError(ValueError):
    pass


def ensure_public_host(host: str) -> None:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PublicReferenceError("No se pudo resolver el sitio de referencia.") from exc
    if not addresses:
        raise PublicReferenceError("No se pudo resolver el sitio de referencia.")
    for item in addresses:
        if not ipaddress.ip_address(item[4][0]).is_global:
            raise PublicReferenceError("La referencia debe ser un sitio publico.")


def _meta(html: str, name: str) -> str:
    # El orden de content/property varia entre CMS; se prueban ambas formas.
    patterns = (
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(name) + r'["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I)
        if match:
            return unescape(match.group(1)).strip()[:1000]
    return ""


def _looks_like_login_wall(title: str, description: str) -> bool:
    value = f"{title} {description}".casefold()
    signals = (
        "log in", "login", "inicia sesion", "iniciar sesion", "create an account",
        "crea una cuenta", "sign up", "registrate para", "facebook – log in",
    )
    return any(signal in value for signal in signals)


def public_image_urls(html: str, page_url: str, *, login_wall: bool = False) -> list[str]:
    if login_wall:
        # Una pared de login suele anunciar como og:image el logo de la red. Es
        # exactamente el falso positivo que antes aparecia como «post».
        return []
    candidates = [_meta(html, "og:image"), _meta(html, "twitter:image")]
    candidates.extend(re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)', html, flags=re.I))
    clean: list[str] = []
    # Un HTML público puede declarar miles de <img>. Acotar la muestra evita
    # convertir una sola URL en cientos de resoluciones DNS durante el brief.
    for item in candidates[:80]:
        raw = unescape(item).strip()
        if not raw:
            continue
        url = urljoin(page_url, raw)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        marker = f"{parsed.netloc}{parsed.path}".casefold()
        if any(token in marker for token in (
            "favicon", "sprite", "logo", "icon", "avatar", "profile_pic",
            "instagram-logo", "facebook-logo", "apple-touch",
        )):
            continue
        try:
            ensure_public_host(parsed.hostname)
        except PublicReferenceError:
            continue
        if url not in clean:
            clean.append(url)
        if len(clean) == 12:
            break
    return clean


def inspect_public_url(url: str, *, timeout: float = 6.0) -> dict[str, object]:
    current = url
    raw = b""
    content_type = ""
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MisivaCreativeStudio/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            for _redirect in range(5):
                parsed = urlparse(current)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise PublicReferenceError("Usa una URL publica http o https.")
                # Se valida cada salto: un dominio publico no puede servir de
                # puente mediante 302 hacia localhost o la red del servidor.
                ensure_public_host(parsed.hostname)
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PublicReferenceError("La referencia redirigio sin destino.")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        remaining = 1_000_000 - size
                        if remaining <= 0:
                            break
                        chunks.append(chunk[:remaining])
                        size += min(len(chunk), remaining)
                    raw = b"".join(chunks)
                    break
            else:
                raise PublicReferenceError("La referencia redirige demasiadas veces.")
    except PublicReferenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - muchos perfiles bloquean bots
        raise PublicReferenceError("No se pudo leer esta referencia publica.") from exc
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise PublicReferenceError("La referencia debe ser una pagina web.")
    html = raw.decode("utf-8", errors="ignore")
    title = _meta(html, "og:title") or _meta(html, "twitter:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        title = (
            unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:180]
            if match else parsed.hostname
        )
    description = _meta(html, "og:description") or _meta(html, "description")
    blocked = _looks_like_login_wall(title, description)
    posts = public_image_urls(html, current, login_wall=blocked)
    return {
        "url": url,
        "title": title,
        "description": description,
        "image": posts[0] if posts else "",
        "posts": posts,
        "accessible": not blocked,
        "blocked_reason": "login_wall" if blocked else "",
    }


__all__ = ["PublicReferenceError", "ensure_public_host", "inspect_public_url"]
