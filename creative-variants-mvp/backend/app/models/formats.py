"""Catálogo único de formatos de producción.

Los identificadores describen la ubicación, no solo el tamaño. Esto importa
porque dos piezas de 1080×1920 (Stories y Reels) tienen zonas seguras distintas.
Los alias históricos por dimensiones se conservan para no romper proyectos ni
clientes anteriores, pero la interfaz nueva solo muestra los presets detallados.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

META_SOURCE = "https://www.facebook.com/business/ads-guide/update/image"
GOOGLE_SOURCE = "https://support.google.com/google-ads/answer/13676244?hl=es"
GOOGLE_SEARCH_SOURCE = "https://support.google.com/google-ads/answer/9566341?hl=es"
GOOGLE_PMAX_SOURCE = "https://support.google.com/google-ads/answer/17091269?hl=es"
GOOGLE_DEMAND_SOURCE = "https://support.google.com/google-ads/answer/17091672?hl=es"
GOOGLE_DISPLAY_SOURCE = "https://support.google.com/google-ads/answer/1722096?hl=es"
YOUTUBE_SOURCE = "https://support.google.com/google-ads/answer/13547298?hl=es"

DEFAULT_SAFE_AREA = {"left": 0.035, "top": 0.035, "right": 0.035, "bottom": 0.035}


def _preset(
    platform: str,
    family: str,
    placement: str,
    width: int,
    height: int,
    ratio: str,
    *,
    safe_area: dict[str, float] | None = None,
    recommended: bool = False,
    media_type: str = "image",
    note: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    return {
        "platform": platform,
        "family": family,
        "placement": placement,
        "label": f"{placement} · {width}×{height}",
        "width": width,
        "height": height,
        "ratio": ratio,
        "safe_area": deepcopy(safe_area or DEFAULT_SAFE_AREA),
        "recommended": recommended,
        "media_type": media_type,
        "note": note,
        "source_url": source_url,
        "last_verified": "2026-09-01",
    }


# Presets visibles en la interfaz. Los márgenes de Stories/Reels y el 80 %
# central de los recursos de Google vienen de la guía maestra suministrada por
# el usuario. En los demás formatos se aplica un margen operativo conservador.
FORMAT_PRESETS: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------ Meta
    "meta_instagram_feed_3_4": _preset(
        "Meta", "Feed", "Instagram Feed orgánico", 1080, 1440, "3:4",
        recommended=True, source_url=META_SOURCE,
    ),
    "meta_feed_4_5": _preset(
        "Meta", "Feed", "Feed vertical universal", 1080, 1350, "4:5",
        recommended=True, source_url=META_SOURCE,
    ),
    "meta_feed_square": _preset(
        "Meta", "Feed", "Feed cuadrado", 1080, 1080, "1:1", source_url=META_SOURCE,
    ),
    "meta_feed_landscape": _preset(
        "Meta", "Feed", "Feed horizontal", 1080, 566, "1.91:1", source_url=META_SOURCE,
    ),
    "meta_stories": _preset(
        "Meta", "Pantalla completa", "Stories", 1080, 1920, "9:16",
        safe_area={"left": 0.04, "top": 0.14, "right": 0.04, "bottom": 0.20},
        recommended=True,
        note="Reserva 14 % arriba y 20 % abajo para la interfaz.",
        source_url=META_SOURCE,
    ),
    "meta_reels": _preset(
        "Meta", "Pantalla completa", "Reels", 1080, 1920, "9:16",
        safe_area={"left": 0.06, "top": 0.14, "right": 0.06, "bottom": 0.35},
        recommended=True,
        note="Reserva 14 % arriba, 35 % abajo y 6 % a cada lado.",
        source_url=META_SOURCE,
    ),
    "meta_carousel_4_5": _preset(
        "Meta", "Carrusel", "Carrusel vertical", 1080, 1350, "4:5",
        recommended=True, source_url=META_SOURCE,
    ),
    "meta_carousel_square": _preset(
        "Meta", "Carrusel", "Carrusel cuadrado", 1080, 1080, "1:1",
        source_url=META_SOURCE,
    ),
    # ----------------------------------------------------------- Google Ads
    "google_business_square": _preset(
        "Google Ads", "Orgánico", "Google Business Profile", 720, 720, "1:1",
        recommended=True,
        note="Preset de la guía maestra entregada; no es un formato de Google Ads.",
    ),
    "google_search_square": _preset(
        "Google Ads", "Búsqueda", "Recurso de imagen cuadrado", 1200, 1200, "1:1",
        safe_area={"left": 0.10, "top": 0.10, "right": 0.10, "bottom": 0.10},
        recommended=True, note="Mantén la información clave en el 80 % central.",
        source_url=GOOGLE_SEARCH_SOURCE,
    ),
    "google_search_landscape": _preset(
        "Google Ads", "Búsqueda", "Recurso de imagen horizontal", 1200, 628, "1.91:1",
        safe_area={"left": 0.10, "top": 0.10, "right": 0.10, "bottom": 0.10},
        recommended=True, note="Mantén la información clave en el 80 % central.",
        source_url=GOOGLE_SEARCH_SOURCE,
    ),
    "google_pmax_landscape": _preset(
        "Google Ads", "Performance Max", "Performance Max horizontal", 1200, 628, "1.91:1",
        recommended=True, source_url=GOOGLE_PMAX_SOURCE,
    ),
    "google_pmax_square": _preset(
        "Google Ads", "Performance Max", "Performance Max cuadrado", 1200, 1200, "1:1",
        recommended=True, source_url=GOOGLE_PMAX_SOURCE,
    ),
    "google_pmax_vertical": _preset(
        "Google Ads", "Performance Max", "Performance Max vertical", 960, 1200, "4:5",
        recommended=True, source_url=GOOGLE_PMAX_SOURCE,
    ),
    "google_demand_gen_square": _preset(
        "Google Ads", "Demand Gen", "Demand Gen cuadrado", 1200, 1200, "1:1",
        recommended=True, source_url=GOOGLE_DEMAND_SOURCE,
    ),
    "google_demand_gen_landscape": _preset(
        "Google Ads", "Demand Gen", "Demand Gen horizontal", 1200, 628, "1.91:1",
        recommended=True, source_url=GOOGLE_DEMAND_SOURCE,
    ),
    "google_demand_gen_vertical": _preset(
        "Google Ads", "Demand Gen", "Demand Gen vertical", 960, 1200, "4:5",
        recommended=True, source_url=GOOGLE_DEMAND_SOURCE,
    ),
    "google_demand_gen_fullscreen": _preset(
        "Google Ads", "Demand Gen", "Demand Gen / Shorts vertical", 1080, 1920, "9:16",
        recommended=True, source_url=GOOGLE_DEMAND_SOURCE,
    ),
    "google_display_300x250": _preset(
        "Google Ads", "Display", "Banner mediano", 300, 250, "6:5", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    "google_display_336x280": _preset(
        "Google Ads", "Display", "Rectángulo grande", 336, 280, "6:5", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    "google_display_728x90": _preset(
        "Google Ads", "Display", "Leaderboard", 728, 90, "8.09:1", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    "google_display_970x90": _preset(
        "Google Ads", "Display", "Leaderboard grande", 970, 90, "10.78:1", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    "google_display_160x600": _preset(
        "Google Ads", "Display", "Skyscraper ancho", 160, 600, "4:15", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    "google_display_300x600": _preset(
        "Google Ads", "Display", "Media página", 300, 600, "1:2", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    "google_display_320x50": _preset(
        "Google Ads", "Display", "Banner móvil", 320, 50, "6.4:1", source_url=GOOGLE_DISPLAY_SOURCE,
    ),
    # --------------------------------------------------------------- YouTube
    "youtube_video_landscape": _preset(
        "YouTube", "Video", "Video horizontal", 1920, 1080, "16:9",
        recommended=True, media_type="video_frame",
        note="La app entrega el key visual estático, no codifica el video.",
        source_url=YOUTUBE_SOURCE,
    ),
    "youtube_video_vertical": _preset(
        "YouTube", "Video", "Video vertical / Shorts", 1080, 1920, "9:16",
        recommended=True, media_type="video_frame",
        note="La app entrega el key visual estático, no codifica el video.",
        source_url=YOUTUBE_SOURCE,
    ),
    "youtube_video_square": _preset(
        "YouTube", "Video", "Video cuadrado", 1080, 1080, "1:1",
        recommended=True, media_type="video_frame",
        note="La app entrega el key visual estático, no codifica el video.",
        source_url=YOUTUBE_SOURCE,
    ),
    "youtube_thumbnail": _preset(
        "YouTube", "Imagen", "Miniatura", 1280, 720, "16:9",
        recommended=True, source_url=YOUTUBE_SOURCE,
    ),
    "youtube_companion": _preset(
        "YouTube", "Imagen", "Banner complementario", 300, 60, "5:1",
        source_url=YOUTUBE_SOURCE,
    ),
}

# Compatibilidad con proyectos, API y pruebas existentes. No se muestran como
# presets porque carecen de contexto de plataforma y pueden duplicar dimensiones.
LEGACY_FORMATS: dict[str, tuple[int, int]] = {
    "1080x1080": (1080, 1080),
    "1080x1350": (1080, 1350),
    "1080x1920": (1080, 1920),
    "1920x1080": (1920, 1080),
    "900x660": (900, 660),
    "900x1350": (900, 1350),
    "1200x400": (1200, 400),
}

SUPPORTED_FORMATS: dict[str, tuple[int, int]] = {
    **LEGACY_FORMATS,
    **{
        key: (int(spec["width"]), int(spec["height"]))
        for key, spec in FORMAT_PRESETS.items()
    },
}


def format_catalog() -> list[dict[str, Any]]:
    """Catálogo serializable para ``GET /capabilities``."""
    return [{"id": key, **deepcopy(value)} for key, value in FORMAT_PRESETS.items()]


def format_spec(format_id: str) -> dict[str, Any]:
    """Metadatos de un preset o una descripción neutra para un alias antiguo."""
    if format_id in FORMAT_PRESETS:
        return {"id": format_id, **deepcopy(FORMAT_PRESETS[format_id])}
    width, height = SUPPORTED_FORMATS[format_id]
    return {
        "id": format_id,
        "platform": "Personalizado",
        "family": "Formato heredado",
        "placement": format_id,
        "label": format_id,
        "width": width,
        "height": height,
        "ratio": f"{width}:{height}",
        "safe_area": deepcopy(DEFAULT_SAFE_AREA),
        "recommended": False,
        "media_type": "image",
        "note": "",
        "source_url": "",
    }


def format_safe_area(format_id: str) -> dict[str, float]:
    """Márgenes fraccionales (izq., arriba, der., abajo) del preset."""
    return {
        key: float(value)
        for key, value in format_spec(format_id)["safe_area"].items()
    }
