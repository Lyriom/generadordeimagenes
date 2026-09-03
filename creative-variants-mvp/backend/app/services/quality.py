"""Validación de calidad: puntaje 0-100 + advertencias accionables.

No hay modelo predictivo: son reglas de composición. La interfaz para conectar
un Predictor Creativo real está en `predictor.py`.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..models import LayerCategory, LayerType, Project, QualityReport
from .imaging import contrast_ratio, hex_to_rgb
from .layout_engine import SAFE_MARGIN, Placement, VariantPlan, overlap_ratio

MIN_LOGO_WIDTH_RATIO = 0.055
MIN_HEADLINE_FONT_RATIO = 0.022
MIN_LEGAL_FONT_RATIO = 0.008
MIN_TEXT_CONTRAST = 4.5
PRODUCT_COVERAGE_RANGE = (0.06, 0.68)
#: Cobertura de contenido (unión de cajas, sin escenografía a sangre).
FILL_RANGE = (0.16, 0.95)
#: Un elemento que cubre este porcentaje del lienzo es escenografía.
FULL_BLEED_RATIO = 0.92


def _is_full_bleed(placement, canvas_w: int, canvas_h: int) -> bool:
    """¿El elemento cubre el lienzo entero (fondo, franja, forma de fondo)?"""
    return (
        placement.width >= canvas_w * FULL_BLEED_RATIO
        and placement.height >= canvas_h * FULL_BLEED_RATIO
    )


def _union_coverage(placements, canvas_w: int, canvas_h: int, grid: int = 64) -> float:
    """Fracción del lienzo ocupada por la UNIÓN de las cajas.

    Sumar áreas cuenta dos veces lo que se solapa (y en un KV importado casi todo se
    solapa), así que la suma daba "saturado" siempre y la exclusión daba "vacío".
    """
    if not placements or canvas_w <= 0 or canvas_h <= 0:
        return 0.0
    mask = np.zeros((grid, grid), dtype=bool)
    for placement in placements:
        x0 = max(0, min(grid, int(placement.x / canvas_w * grid)))
        y0 = max(0, min(grid, int(placement.y / canvas_h * grid)))
        x1 = max(x0 + 1, min(grid, int((placement.x + placement.width) / canvas_w * grid)))
        y1 = max(y0 + 1, min(grid, int((placement.y + placement.height) / canvas_h * grid)))
        mask[y0:y1, x0:x1] = True
    return float(mask.mean())


def _text_background_color(
    image: Image.Image, box: tuple[int, int, int, int], text_color: str
) -> tuple[int, int, int]:
    """Color de fondo bajo un texto, excluyendo los píxeles del propio texto."""
    x, y, w, h = box
    x0 = max(0, min(int(x), image.width - 1))
    y0 = max(0, min(int(y), image.height - 1))
    x1 = max(x0 + 1, min(int(x + w), image.width))
    y1 = max(y0 + 1, min(int(y + h), image.height))
    crop = np.asarray(image.convert("RGB").crop((x0, y0, x1, y1)), dtype=np.float32)
    if crop.size == 0:
        return (0, 0, 0)
    target = np.array(hex_to_rgb(text_color), dtype=np.float32)
    distance = np.linalg.norm(crop - target[None, None, :], axis=2)
    background_pixels = crop[distance > 70]
    if background_pixels.size < 12:
        background_pixels = crop.reshape(-1, 3)
    mean = background_pixels.reshape(-1, 3).mean(axis=0)
    return tuple(int(round(v)) for v in mean)  # type: ignore[return-value]


def evaluate_variant(
    project: Project,
    plan: VariantPlan,
    image: Image.Image | None = None,
    extra_warnings: list[str] | None = None,
) -> QualityReport:
    canvas_w, canvas_h = plan.width, plan.height
    canvas_area = float(canvas_w * canvas_h)
    margin = int(round(SAFE_MARGIN * min(canvas_w, canvas_h)))
    placements: list[Placement] = list(plan.placements)

    penalties = 0.0
    warnings: list[str] = []
    metrics: dict[str, float] = {}

    # 1. Fuera del lienzo -------------------------------------------------
    outside = 0
    for placement in placements:
        if placement.pinned and _is_full_bleed(placement, canvas_w, canvas_h):
            # Sangrado deliberado: cubrir el lienzo obliga a sobresalir.
            continue
        x, y, w, h = placement.box
        if x < 0 or y < 0 or x + w > canvas_w or y + h > canvas_h:
            outside += 1
            warnings.append(f"'{placement.layer.name}' sale del lienzo.")
    metrics["outside_canvas"] = float(outside)
    penalties += outside * 12

    # 2. Solapamientos ----------------------------------------------------
    severe = 0
    moderate = 0
    # Solo se tolera el solape entre dos piezas que ya venían ancladas en el PSD.
    # Un producto nuevo encima del logo, descuento o CTA sí es un defecto grave.
    mandatory = {
        LayerCategory.LOGO,
        LayerCategory.HEADLINE,
        LayerCategory.SUBHEADLINE,
        LayerCategory.PRICE,
        LayerCategory.CTA,
        LayerCategory.LEGAL,
    }
    for i, first in enumerate(placements):
        for second in placements[i + 1 :]:
            # Una textura/plancha a sangre está detrás de todo por definición. Su caja
            # cubre el producto, pero sus píxeles no representan una invasión visual.
            if _is_full_bleed(first, canvas_w, canvas_h) or _is_full_bleed(
                second, canvas_w, canvas_h
            ):
                continue
            # Sombras, pedestales, brillos y formas del PSD están hechas para
            # quedar detrás del producto. El cruce de sus cajas es intencional y
            # no debe bloquear una propuesta. Logo, copy, CTA y legales sí se
            # protegen mediante sus categorías específicas.
            if LayerCategory.DECORATION in {
                first.layer.category,
                second.layer.category,
            }:
                continue
            if (
                first.pinned
                and second.pinned
                and first.layer.category != LayerCategory.PRODUCT
                and second.layer.category != LayerCategory.PRODUCT
            ):
                continue
            ratio = overlap_ratio(first.box, second.box)
            product_vs_mandatory = (
                first.layer.category == LayerCategory.PRODUCT
                and second.layer.category in mandatory
            ) or (
                second.layer.category == LayerCategory.PRODUCT
                and first.layer.category in mandatory
            )
            threshold = 0.08 if product_vs_mandatory else (
                0.16 if (first.layer.is_text or second.layer.is_text) else 0.30
            )
            if ratio > max(0.45, threshold):
                severe += 1
                warnings.append(
                    f"Solapamiento fuerte entre '{first.layer.name}' y '{second.layer.name}'."
                )
            elif ratio > threshold:
                moderate += 1
                if product_vs_mandatory:
                    protected = (
                        second.layer.name
                        if first.layer.category == LayerCategory.PRODUCT
                        else first.layer.name
                    )
                    warnings.append(f"El producto invade '{protected}'.")
    metrics["severe_overlaps"] = float(severe)
    metrics["moderate_overlaps"] = float(moderate)
    penalties += severe * 18 + moderate * 6

    # 3. Márgenes seguros -------------------------------------------------
    tight = 0
    for placement in placements:
        if placement.pinned:
            # Va donde el diseñador lo puso: una franja a sangre toca el borde
            # por definición, y penalizarla sería castigar la fidelidad.
            continue
        x, y, w, h = placement.box
        gap = min(x, y, canvas_w - (x + w), canvas_h - (y + h))
        if gap < margin * 0.6:
            tight += 1
            if placement.layer.category in {LayerCategory.CTA, LayerCategory.LOGO}:
                warnings.append(
                    f"'{placement.layer.name}' está demasiado cerca del margen."
                )
    metrics["tight_margins"] = float(tight)
    penalties += tight * 3

    # 4. Cobertura del producto ------------------------------------------
    products = [p for p in placements if p.layer.category == LayerCategory.PRODUCT]
    product = products[0] if products else None
    if product is not None:
        # Suma de todas las capas de producto: un pack de 3 prendas cubre el arte
        # aunque cada pieza sea pequeña.
        coverage = sum(p.width * p.height for p in products) / canvas_area
        metrics["product_coverage"] = round(coverage, 4)
        low, high = PRODUCT_COVERAGE_RANGE
        if coverage < low:
            penalties += 10
            warnings.append("El producto se ve demasiado pequeño en la composición.")
        elif coverage > high:
            penalties += 7
            warnings.append("El producto ocupa demasiado espacio y ahoga la composición.")
    else:
        metrics["product_coverage"] = 0.0

    # 5. Tamaño mínimo del logo ------------------------------------------
    logo = next((p for p in placements if p.layer.category == LayerCategory.LOGO), None)
    if logo is not None:
        ratio = logo.width / canvas_w
        metrics["logo_width_ratio"] = round(ratio, 4)
        if ratio < MIN_LOGO_WIDTH_RATIO:
            penalties += 8
            warnings.append("El logo queda por debajo del tamaño mínimo recomendado.")

    # 6-7. Legibilidad y contraste del texto -----------------------------
    small_text = 0
    low_contrast = 0
    for placement in placements:
        if placement.layer.type != LayerType.TEXT:
            continue
        font_size = placement.font_size or placement.layer.font_size
        ratio = font_size / canvas_h
        minimum = (
            MIN_HEADLINE_FONT_RATIO
            if placement.layer.category == LayerCategory.HEADLINE
            else MIN_LEGAL_FONT_RATIO
        )
        if ratio < minimum:
            small_text += 1
            warnings.append(
                f"'{placement.layer.name}' puede ser ilegible ({font_size}px en {canvas_h}px)."
            )
        if image is not None:
            color = placement.color or placement.layer.color
            bg = _text_background_color(image, placement.box, color)
            contrast = contrast_ratio(hex_to_rgb(color), bg)
            if contrast < MIN_TEXT_CONTRAST:
                low_contrast += 1
                warnings.append(
                    f"Contraste insuficiente en '{placement.layer.name}' "
                    f"({contrast:.1f}:1, mínimo {MIN_TEXT_CONTRAST}:1)."
                )
    metrics["small_text"] = float(small_text)
    metrics["low_contrast_text"] = float(low_contrast)
    penalties += small_text * 6 + low_contrast * 7

    # 8. Elementos obligatorios ------------------------------------------
    present = {p.layer.category for p in placements}
    # Lo que el usuario quitó del arte a propósito no es una falta: pedir el
    # logo de vuelta después de haberlo retirado sería discutirle la decisión.
    project_categories = {
        layer.category
        for layer in project.layers
        if not layer.meta.get("removed_from_art")
    }

    # Una pieza publicitaria necesita varias piezas: sin esto una composición casi
    # vacía obtenía 100/100 y ocultaba el problema real.
    visible_count = len(placements)
    metrics["elements"] = float(visible_count)
    if visible_count < 3:
        penalties += 10 * (3 - visible_count)
        warnings.append(
            f"La composición solo tiene {visible_count} elemento(s): falta contenido "
            "(producto, texto y logo)."
        )
    text_count = sum(1 for p in placements if p.layer.type == LayerType.TEXT)
    metrics["text_elements"] = float(text_count)
    if text_count == 0:
        if project.analysis.segmentation_provider == "psd":
            # El copy del PSD llegó rasterizado: el texto está, pero como imagen.
            warnings.append(
                "El copy viene del PSD como imagen, no como texto: se conserva tal cual. "
                "Para cambiarlo, reescríbalo en «Textos y logos del arte»."
            )
        else:
            penalties += 12
            warnings.append(
                "La variante no tiene texto: sin titular, precio ni CTA no es una pieza "
                "publicitaria utilizable."
            )
    for required, message in (
        (LayerCategory.LOGO, "Falta el logo en la variante."),
        (LayerCategory.CTA, "Falta el CTA en la variante."),
        (LayerCategory.LEGAL, "Falta el texto legal en la variante."),
    ):
        if required in project_categories and required not in present:
            penalties += 6
            warnings.append(message)

    # 9. Preservación de relación de aspecto ------------------------------
    distorted = 0
    for placement in placements:
        layer = placement.layer
        if layer.type != LayerType.IMAGE or not layer.preserve_aspect_ratio:
            continue
        if placement.stretch:
            # Estirado autorizado: escenografía a sangre (nunca producto ni logo, que
            # son pixel_critical y por eso el motor no les concede el permiso).
            continue
        if layer.height <= 0 or placement.height <= 0:
            continue
        original = layer.width / layer.height
        rendered = placement.width / placement.height
        # Con dimensiones enteras, una franja de 14 px de alto no puede expresar su
        # proporción con más precisión: la tolerancia crece cuando el lado es pequeño.
        tolerance = max(0.02, 1.5 / max(1, min(placement.width, placement.height)))
        if abs(original - rendered) / max(original, 0.0001) > tolerance:
            distorted += 1
            warnings.append(
                f"'{layer.name}' cambió su relación de aspecto (revisar renderer)."
            )
    metrics["distorted_layers"] = float(distorted)
    penalties += distorted * 15

    # 10. Espacio vacío ---------------------------------------------------
    # Escenografía (fondos y formas a sangre) no cuenta como contenido: lo que mide
    # esta regla es si la pieza tiene sustancia, no si tiene color de fondo.
    content = [p for p in placements if not _is_full_bleed(p, canvas_w, canvas_h)]
    filled = _union_coverage(content, canvas_w, canvas_h)
    metrics["fill_ratio"] = round(filled, 4)
    low, high = FILL_RANGE
    if not content:
        pass
    elif filled < low:
        penalties += 8
        warnings.append("La composición tiene demasiado espacio vacío.")
    elif filled > high:
        penalties += 6
        warnings.append("La composición está saturada: falta aire entre elementos.")

    for warning in extra_warnings or []:
        if warning not in warnings:
            warnings.append(warning)
            penalties += 2

    score = int(max(0, min(100, round(100 - penalties))))
    metrics["penalties"] = round(penalties, 2)
    # Se limita la lista para no saturar la interfaz.
    unique_warnings = list(dict.fromkeys(warnings))[:10]
    return QualityReport(score=score, warnings=unique_warnings, metrics=metrics)
