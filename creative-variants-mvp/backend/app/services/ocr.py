"""Servicio OCR: ejecuta RapidOCR (si está) y clasifica los textos en categorías.

No se intenta identificar la tipografía real: se usa una fuente por defecto y el
usuario puede cambiarla en Ajustes finos.
"""
from __future__ import annotations

import re

from ..models import LayerCategory
from ..providers import OcrResult, TextRegion, get_ocr_provider

PRICE_RE = re.compile(
    r"(?:^|\s)(?:[$€£¢]|usd|ars|mxn|cop|clp|pen|s/\.?|bs\.?)\s?\d|"
    r"\d+[.,]\d{2}\s?(?:usd|€|\$)?|"
    r"\d+\s?%\s?(?:dcto|desc|off)",
    re.IGNORECASE,
)

CTA_KEYWORDS = (
    "compra", "comprar", "conoce", "descubre", "click", "clic", "visita",
    "pide", "pedir", "ordena", "reserva", "regístrate", "registrate",
    "descarga", "suscríbete", "suscribete", "solicita", "llama", "escríbenos",
    "escribenos", "ver más", "ver mas", "más info", "mas info", "shop now",
    "buy now", "learn more", "sign up", "aprovecha", "quiero",
)

LEGAL_KEYWORDS = (
    "aplican", "términos", "terminos", "condiciones", "restricciones",
    "válido", "valido", "vigencia", "consulta", "promoción válida",
    "hasta agotar", "stock", "superintendencia", "registro sanitario",
    "imagen referencial", "no incluye", "*", "©", "®",
)

PERCENT_RE = re.compile(r"\d+\s?%")


def run_ocr(image_path: str) -> OcrResult:
    return get_ocr_provider().read(image_path)


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = f" {text.lower()} "
    return any(keyword in lowered for keyword in keywords)


def categorize_text(
    region: TextRegion,
    image_size: tuple[int, int],
    max_height: int,
) -> tuple[LayerCategory, float]:
    """Clasifica un texto por contenido, tamaño y posición. Devuelve (categoría, confianza)."""
    width, height = image_size
    text = region.text.strip()
    rel_height = region.height / max(1, max_height)
    rel_y = (region.y + region.height / 2) / max(1, height)
    words = len(text.split())

    if PRICE_RE.search(text) or (PERCENT_RE.search(text) and words <= 3):
        return LayerCategory.PRICE, 0.72

    if _has_keyword(text, CTA_KEYWORDS) and words <= 6:
        return LayerCategory.CTA, 0.7

    small = rel_height <= 0.45
    if small and (words >= 8 or _has_keyword(text, LEGAL_KEYWORDS) or rel_y > 0.86):
        return LayerCategory.LEGAL, 0.6

    if rel_height >= 0.8:
        return LayerCategory.HEADLINE, 0.68
    if rel_height >= 0.5:
        return LayerCategory.SUBHEADLINE, 0.6
    return LayerCategory.SUBHEADLINE, 0.45


def estimate_font_size(region: TextRegion) -> int:
    """Tamaño de fuente aproximado desde la altura del bounding box."""
    lines = max(1, round(region.height / max(1, region.height)))  # una línea por región OCR
    return max(10, int(round(region.height / lines * 0.86)))


def build_text_layer_payloads(
    result: OcrResult, image_size: tuple[int, int]
) -> list[dict]:
    """Convierte regiones OCR en descripciones de capas de texto editables."""
    if not result.regions:
        return []
    max_height = max(region.height for region in result.regions)
    payloads: list[dict] = []
    for region in result.regions:
        category, category_confidence = categorize_text(region, image_size, max_height)
        confidence = round(min(1.0, (region.confidence * 0.6) + (category_confidence * 0.4)), 3)
        warnings: list[str] = []
        if region.confidence < 0.7:
            warnings.append(
                f"Texto reconocido con baja confianza ({region.confidence:.2f}): revise el contenido."
            )
        if abs(region.angle) > 6:
            warnings.append(
                f"Texto rotado aproximadamente {region.angle:.0f}°: se renderiza horizontal."
            )
        payloads.append(
            {
                "content": region.text,
                "category": category,
                "x": region.x,
                "y": region.y,
                "width": region.width,
                "height": region.height,
                "font_size": estimate_font_size(region),
                "color": region.color,
                "confidence": confidence,
                "angle": region.angle,
                "warnings": warnings,
            }
        )
    return payloads
