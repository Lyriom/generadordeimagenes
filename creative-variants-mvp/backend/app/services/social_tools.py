"""Lectura de publicaciones reales de Instagram vía Social Tools.

Un perfil de Instagram no entrega las imágenes de su feed a quien no ha
iniciado sesión: devuelve la aplicación en JavaScript y, como mucho, el avatar.
Eso dejaba al brief sin la evidencia más valiosa que tiene una marca, que es
cómo componen sus propias piezas publicadas.

Social Tools (``api.mysocialtools.net``) es un proveedor de solo lectura al que
la casa ya está suscrita. Su endpoint ``Data`` devuelve, día a día, los posts de
una cuenta con ``Url_media`` y ``Thumbnail``: las imágenes de verdad.

Dos límites que conviene tener presentes:

* **Solo Instagram.** El ``Data`` de Facebook no expone la imagen del post, así
  que para Facebook seguimos dependiendo de enlaces a publicaciones concretas.
* **Solo cuentas del catálogo.** Social Tools cubre las cuentas que tiene dadas
  de alta (~65.000); si la marca no está, se dice y se sigue con el resto del
  material en vez de fingir que no había nada que leer.

La API es POST con ``login``/``password`` en el cuerpo y el resto en el query
string. No tiene GET, PUT ni DELETE: no hay forma de escribir nada.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class SocialToolsError(RuntimeError):
    """Fallo al hablar con Social Tools; nunca debe tumbar un análisis."""


#: La API tope un mes por petición.
MAX_RANGE_DAYS = 31
#: Suficiente para leer el lenguaje visual de una marca sin inflar el prompt.
MAX_POSTS = 24


def available() -> bool:
    return bool(settings.socialtools_login and settings.socialtools_password)


def instagram_handle(url: str) -> str | None:
    """Extrae el usuario de una URL de perfil de Instagram.

    Un enlace a una publicación concreta NO es un perfil: ese camino ya
    funciona leyendo su ``og:image``, y no hay que gastar una llamada de API.
    """

    try:
        parsed = urlparse(url if "//" in url else f"https://{url}")
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host.endswith("instagram.com"):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if parts[0].casefold() in {"p", "reel", "reels", "tv", "stories", "explore"}:
        return None
    handle = parts[0].casefold()
    return handle if re.fullmatch(r"[a-z0-9._]{1,40}", handle) else None


def _request(segment: str, endpoint: str, query: dict[str, object]) -> object:
    if not available():
        raise SocialToolsError("Social Tools no está configurado.")
    url = f"{settings.socialtools_base_url}/{segment}/{endpoint}.php"
    try:
        with httpx.Client(timeout=settings.socialtools_timeout, trust_env=False) as client:
            response = client.post(
                url,
                data={
                    "login": settings.socialtools_login,
                    "password": settings.socialtools_password,
                },
                params={key: value for key, value in query.items() if value not in (None, "")},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise SocialToolsError(
            f"Social Tools respondió {exc.response.status_code}."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SocialToolsError("No se pudo leer la respuesta de Social Tools.") from exc


def _catalog_path() -> Path:
    return settings.data_dir / "social_tools" / "instagram-catalog.json"


def _load_catalog() -> list[dict] | None:
    path = _catalog_path()
    if not path.is_file():
        return None
    edad = time.time() - path.stat().st_mtime
    if edad > settings.socialtools_catalog_hours * 3600:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entradas = payload.get("entries") if isinstance(payload, dict) else None
    return entradas if isinstance(entradas, list) else None


def _store_catalog(entries: list[dict]) -> None:
    path = _catalog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:  # noqa: BLE001 - la caché es una optimización, no un requisito
        logger.info("No se pudo guardar el catálogo de Social Tools")


def _rows(payload: object) -> list[dict]:
    """La API envuelve la lista de forma distinta según el endpoint."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        # GetPages envuelve en "export"; Data en "account". Verificado contra
        # el mapeador del servicio socialtools-explorer y su catalogo guardado.
        for clave in ("export", "account", "pages", "accounts", "data", "results"):
            valor = payload.get(clave)
            if isinstance(valor, list):
                return [item for item in valor if isinstance(item, dict)]
    return []


def instagram_catalog(*, refresh: bool = False) -> list[dict]:
    """Cuentas de Instagram dadas de alta, con su id interno y su enlace."""

    if not refresh:
        cacheado = _load_catalog()
        if cacheado is not None:
            return cacheado
    filas = _rows(_request("Instagram", "GetPages", {}))
    entradas = []
    for fila in filas:
        identificador = fila.get("id_page") or fila.get("Id") or fila.get("id")
        try:
            numero = int(str(identificador))
        except (TypeError, ValueError):
            continue
        entradas.append({
            "id": numero,
            "name": str(fila.get("nom_page") or fila.get("Name") or "")[:240],
            # El enlace se llama lien_page: de ahi sale el usuario con el que
            # se empareja la URL que puso la persona en la campaña.
            "link": str(
                fila.get("lien_page") or fila.get("Link") or fila.get("link") or ""
            )[:500],
            "audience": fila.get("nbFollowers_page") or fila.get("nbFan_page") or 0,
        })
    if entradas:
        _store_catalog(entradas)
    return entradas


def find_account(handle: str) -> dict | None:
    """Empareja un usuario de Instagram con su ficha en Social Tools."""

    objetivo = handle.casefold().strip("/")
    if not objetivo:
        return None
    for entrada in instagram_catalog():
        enlace = str(entrada.get("link") or "")
        usuario = instagram_handle(enlace) if enlace else None
        if usuario == objetivo:
            return entrada
    return None


def _post_images(payload: object) -> list[dict]:
    """Aplana la respuesta Data (cuenta → días → posts) a una lista de piezas."""

    cuentas = _rows(payload)
    piezas: list[dict] = []
    for cuenta in cuentas:
        dias = cuenta.get("Dates")
        if not isinstance(dias, list):
            continue
        for dia in dias:
            if not isinstance(dia, dict):
                continue
            posts = dia.get("Posts")
            if not isinstance(posts, list):
                continue
            for post in posts:
                if not isinstance(post, dict):
                    continue
                media = str(post.get("Url_media") or post.get("Thumbnail") or "")
                if not media.startswith(("http://", "https://")):
                    continue
                piezas.append({
                    "media": media[:1000],
                    "link": str(post.get("Link") or "")[:1000],
                    "text": str(post.get("Text") or "")[:1000],
                    "date": f"{dia.get('Date', '')} {post.get('Date', '')}".strip()[:40],
                    "likes": post.get("Likes"),
                    "comments": post.get("Comments"),
                })
    return piezas


def recent_posts(id_page: int, *, days: int | None = None) -> list[dict]:
    """Publicaciones recientes de una cuenta, las más nuevas primero."""

    ventana = min(max(1, days or settings.socialtools_days), MAX_RANGE_DAYS)
    fin = date.today()
    inicio = fin - timedelta(days=ventana)
    payload = _request(
        "Instagram",
        "Data",
        {
            "idPage": int(id_page),
            "dateDebut": inicio.isoformat(),
            "dateFin": fin.isoformat(),
            "Post": 1,
        },
    )
    piezas = _post_images(payload)
    piezas.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    vistos: set[str] = set()
    unicas: list[dict] = []
    for pieza in piezas:
        if pieza["media"] in vistos:
            continue
        vistos.add(pieza["media"])
        unicas.append(pieza)
        if len(unicas) >= MAX_POSTS:
            break
    return unicas


def profile_posts(url: str, *, days: int | None = None) -> dict | None:
    """Lee las publicaciones de un perfil de Instagram, o explica por qué no.

    Devuelve ``None`` cuando la URL no es un perfil de Instagram: ese caso lo
    resuelve el lector público y no debe gastar una llamada de API.
    """

    handle = instagram_handle(url)
    if handle is None:
        return None
    if not available():
        return {
            "handle": handle,
            "posts": [],
            "reason": "no_configurado",
            "detail": (
                "Social Tools no está configurado en este servidor, así que el feed "
                "del perfil no se puede leer."
            ),
        }
    try:
        cuenta = find_account(handle)
    except SocialToolsError as exc:
        return {
            "handle": handle, "posts": [], "reason": "error",
            "detail": f"No se pudo consultar Social Tools: {exc}",
        }
    if cuenta is None:
        return {
            "handle": handle, "posts": [], "reason": "sin_cobertura",
            "detail": (
                f"Social Tools no tiene dada de alta la cuenta @{handle}. "
                "Pide que la añadan, o pega enlaces de publicaciones concretas."
            ),
        }
    try:
        posts = recent_posts(int(cuenta["id"]), days=days)
    except SocialToolsError as exc:
        return {
            "handle": handle, "posts": [], "reason": "error",
            "detail": f"No se pudo leer el feed de @{handle}: {exc}",
            "account": cuenta,
        }
    return {
        "handle": handle,
        "posts": posts,
        "reason": "" if posts else "sin_publicaciones",
        "detail": "" if posts else (
            f"@{handle} está en Social Tools pero no publicó nada en la ventana consultada."
        ),
        "account": cuenta,
    }


__all__ = [
    "MAX_POSTS", "SocialToolsError", "available", "find_account",
    "instagram_catalog", "instagram_handle", "profile_posts", "recent_posts",
]
