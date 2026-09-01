"""Renderizado de variantes con Pillow.

Garantías:
- Las capas bloqueadas se pintan SIEMPRE desde su PNG extraído, con escala
  uniforme (nunca se deforman ni se regeneran).
- El texto se ajusta a su caja (wrap + autofit) y recibe un fondo translúcido
  cuando el contraste no alcanza el mínimo legible.
"""
from __future__ import annotations

import logging
import random
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..config import settings
from ..models import LayerCategory, LayerType, Project
from . import storage
from .imaging import (
    best_text_color,
    blur,
    dominant_colors,
    hex_to_rgb,
    load_alpha,
    load_flat_rgb,
    make_gradient,
    region_average_color,
    relative_luminance,
    resize_cover,
    rounded_rect,
    style_palette,
)
from .layout_engine import CATEGORY_MAX_LINES, Placement, VariantPlan

logger = logging.getLogger(__name__)

MIN_CONTRAST = 4.5
FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


@lru_cache(maxsize=64)
def _load_font(path: str | None, size: int):
    size = max(6, int(size))
    candidates = [path] if path else []
    candidates += [settings.default_font, settings.default_font_bold, *FALLBACK_FONTS]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def resolve_font_path(project: Project, weight: str) -> str | None:
    """Tipografía subida por el usuario > DejaVu (bold/regular) > default de PIL."""
    ref_font = project.references.font
    if ref_font:
        try:
            candidate = storage.abs_path(project.project_id, ref_font)
            if candidate.exists():
                return str(candidate)
        except Exception:  # noqa: BLE001
            pass
    return settings.default_font_bold if weight == "bold" else settings.default_font


# ------------------------------------------------------------------- background
def _background_source(project: Project) -> tuple[Image.Image, np.ndarray | None]:
    """Plancha limpia si existe; si no, el arte original aplanado sobre blanco.

    Devuelve también el alfa del origen: en un recorte de producto los píxeles
    transparentes no deben contaminar la paleta del fondo.
    """
    if project.background.path:
        path = storage.abs_path(project.project_id, project.background.path)
        if path.exists():
            return load_flat_rgb(path), load_alpha(path)
    path = storage.abs_path(project.project_id, project.source.path)
    return load_flat_rgb(path), load_alpha(path)


def _palette_source(project: Project) -> tuple[Image.Image, np.ndarray | None] | None:
    """El KV de referencia manda en la paleta cuando existe."""
    reference = project.references.kv
    if reference is None:
        return None
    path = storage.abs_path(project.project_id, reference.path)
    if not path.exists():
        return None
    return load_flat_rgb(path), load_alpha(path)


def build_background(project: Project, plan: VariantPlan) -> Image.Image:
    width, height = plan.width, plan.height
    source, source_alpha = _background_source(project)
    rng = random.Random(plan.seed)
    style = plan.background_style
    palette_image, palette_alpha = _palette_source(project) or (source, source_alpha)
    primary, secondary = style_palette(palette_image, alpha=palette_alpha)

    if style == "solid":
        return Image.new("RGB", (width, height), hex_to_rgb(primary))
    if style == "gradient":
        direction = rng.choice(["vertical", "horizontal", "diagonal"])
        return make_gradient(width, height, primary, secondary, direction)
    if style == "duotone":
        base = resize_cover(source, width, height).convert("L").convert("RGB")
        tint = make_gradient(width, height, primary, secondary, "diagonal")
        return Image.blend(base, tint, 0.55)
    if style == "plate_blur":
        return blur(resize_cover(source, width, height), max(6.0, min(width, height) * 0.02))
    if style == "plate_zoom":
        zoomed = resize_cover(source, int(width * 1.18), int(height * 1.18))
        left = (zoomed.width - width) // 2
        top = (zoomed.height - height) // 2
        return zoomed.crop((left, top, left + width, top + height))
    return resize_cover(source, width, height)


# ------------------------------------------------------------------------ texto
def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _text_block_size(
    lines: list[str], font, draw: ImageDraw.ImageDraw, line_height: float
) -> tuple[int, int]:
    if not lines:
        return 0, 0
    widths = [draw.textlength(line, font=font) for line in lines]
    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 0)
    line_px = int((ascent + descent) * line_height)
    return int(max(widths)), int(line_px * len(lines))


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str | None,
    box_w: int,
    box_h: int,
    start_size: int,
    line_height: float = 1.15,
    min_size: int = 9,
    max_lines: int = 3,
) -> tuple[object, list[str], tuple[int, int]]:
    """Reduce el tamaño hasta que el texto quepa en la caja y en `max_lines`."""
    size = max(min_size, int(start_size))
    explicit_lines = text.count("\n") + 1
    limit = max(max_lines, explicit_lines)
    for _ in range(80):
        font = _load_font(font_path, size)
        lines = _wrap_text(text, font, box_w, draw)
        block_w, block_h = _text_block_size(lines, font, draw, line_height)
        fits = block_w <= box_w and block_h <= box_h and len(lines) <= limit
        if fits or size <= min_size:
            return font, lines, (block_w, block_h)
        size = max(min_size, int(size * 0.94))
    font = _load_font(font_path, min_size)
    lines = _wrap_text(text, font, box_w, draw)
    return font, lines, _text_block_size(lines, font, draw, line_height)


def accent_color(canvas: Image.Image) -> tuple[int, int, int]:
    """Color de marca oscuro para botones, tomado de la paleta del lienzo."""
    candidates = [hex_to_rgb(value) for value in dominant_colors(canvas, 4)]
    darkest = min(candidates, key=relative_luminance) if candidates else (17, 17, 17)
    return darkest if relative_luminance(darkest) <= 0.5 else (17, 17, 17)


def draw_text_layer(
    canvas: Image.Image,
    placement: Placement,
    project: Project,
) -> tuple[tuple[int, int, int, int], list[str]]:
    """Dibuja una capa de texto. Devuelve la caja real usada y advertencias."""
    warnings: list[str] = []
    layer = placement.layer
    text = (layer.content or "").strip()
    if not text:
        return placement.box, warnings

    draw = ImageDraw.Draw(canvas)
    font_path = resolve_font_path(project, layer.font_weight)
    start_size = placement.font_size or layer.font_size
    font, lines, (block_w, block_h) = fit_text(
        draw,
        text,
        font_path,
        placement.width,
        placement.height,
        start_size,
        layer.line_height,
        max_lines=CATEGORY_MAX_LINES.get(layer.category, 3),
    )

    # Posición vertical del bloque dentro de la caja asignada.
    if placement.valign == "top":
        block_y = placement.y
    elif placement.valign == "bottom":
        block_y = placement.y + max(0, placement.height - block_h)
    else:
        block_y = placement.y + max(0, (placement.height - block_h) // 2)

    block_x = _block_x(placement, block_w)
    block_box = (block_x, block_y, max(1, block_w), max(1, block_h))

    bg_rgb = region_average_color(canvas, block_box)
    color = layer.color
    if layer.auto_contrast:
        color = best_text_color(bg_rgb, layer.color)

    from .imaging import contrast_ratio, region_std

    pad = max(6, int(min(canvas.size) * 0.011))
    pill_drawn = False
    if layer.category == LayerCategory.CTA and layer.meta.get("pill", True):
        # El CTA se dibuja como botón: es lo que se espera en un arte publicitario.
        fill_rgb = hex_to_rgb(layer.color)
        if relative_luminance(fill_rgb) > 0.5:  # color muy claro: no serviría de botón
            fill_rgb = accent_color(canvas)
        pill = rounded_rect(
            (block_w + pad * 3, block_h + pad * 2),
            radius=(block_h + pad * 2) // 2,
            fill=(*fill_rgb, 255),
        )
        canvas.paste(pill, (block_x - pad * 3 // 2, block_y - pad), pill)
        bg_rgb = fill_rgb
        color = best_text_color(fill_rgb, layer.color)
        pill_drawn = True

    ratio = contrast_ratio(hex_to_rgb(color), bg_rgb)
    noisy = region_std(canvas, block_box) > 58
    if not pill_drawn and (ratio < MIN_CONTRAST or noisy):
        # Zona sin contraste suficiente: se añade un scrim translúcido legible.
        scrim_rgb = (18, 18, 18) if sum(hex_to_rgb(color)) > 384 else (245, 245, 245)
        scrim = rounded_rect(
            (block_w + pad * 2, block_h + pad * 2),
            radius=max(6, pad),
            fill=(*scrim_rgb, 150),
        )
        canvas.paste(scrim, (block_x - pad, block_y - pad), scrim)
        warnings.append(
            f"'{layer.name}' se colocó sobre una zona de bajo contraste: se añadió fondo."
        )
        bg_rgb = scrim_rgb
        color = best_text_color(bg_rgb, layer.color)

    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 0)
    line_px = int((ascent + descent) * layer.line_height)
    y = block_y
    fill = hex_to_rgb(color)
    for line in lines:
        line_w = draw.textlength(line, font=font)
        if placement.align == "center":
            x = placement.x + (placement.width - line_w) / 2
        elif placement.align == "right":
            x = placement.x + (placement.width - line_w)
        else:
            x = placement.x
        draw.text((x, y), line, font=font, fill=fill)
        y += line_px

    placement.color = color
    placement.font_size = int(getattr(font, "size", start_size))
    return block_box, warnings


def _block_x(placement: Placement, block_w: int) -> int:
    if placement.align == "center":
        return placement.x + max(0, (placement.width - block_w) // 2)
    if placement.align == "right":
        return placement.x + max(0, placement.width - block_w)
    return placement.x


# ----------------------------------------------------------------------- imagen
def draw_image_layer(
    canvas: Image.Image, placement: Placement, project: Project
) -> tuple[tuple[int, int, int, int], list[str]]:
    """Pega una capa imagen desde su PNG extraído sin deformarla."""
    warnings: list[str] = []
    layer = placement.layer
    if not layer.src:
        return placement.box, [f"'{layer.name}' no tiene PNG extraído."]
    path = storage.abs_path(project.project_id, layer.src)
    if not path.exists():
        return placement.box, [f"No se encontró el PNG de '{layer.name}'."]

    with Image.open(path) as source:
        asset = source.convert("RGBA").copy()

    if placement.stretch:
        # Escenografía a sangre: llena el borde. El motor solo lo autoriza para
        # fondos y franjas, nunca para producto, logo o persona.
        new_w, new_h = max(1, placement.width), max(1, placement.height)
    else:
        # Escala uniforme: se preserva exactamente la relación de aspecto original.
        scale = min(placement.width / asset.width, placement.height / asset.height)
        new_w = max(1, int(round(asset.width * scale)))
        new_h = max(1, int(round(asset.height * scale)))
    resized = asset.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if layer.category in {LayerCategory.PRODUCT, LayerCategory.PERSON}:
        resized = resized.filter(
            ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3)
        )

    if layer.rotation and not layer.pixel_critical:
        resized = resized.rotate(-layer.rotation, expand=True, resample=Image.BICUBIC)

    x = placement.x + (placement.width - resized.width) // 2
    y = placement.y + (placement.height - resized.height) // 2
    canvas.paste(resized, (x, y), resized)

    placement.width, placement.height = resized.width, resized.height
    placement.x, placement.y = x, y
    return (x, y, resized.width, resized.height), warnings


# ---------------------------------------------------------------------- variant
def render_variant(project: Project, plan: VariantPlan) -> tuple[Image.Image, list[str]]:
    """Renderiza una variante completa y devuelve (imagen, advertencias)."""
    canvas = build_background(project, plan).convert("RGB")
    warnings: list[str] = list(plan.notes)

    for placement in sorted(plan.placements, key=lambda item: item.z_index):
        try:
            if placement.layer.type == LayerType.TEXT:
                _, layer_warnings = draw_text_layer(canvas, placement, project)
            else:
                _, layer_warnings = draw_image_layer(canvas, placement, project)
            warnings.extend(layer_warnings)
        except Exception as exc:  # noqa: BLE001 - una capa no debe romper el render
            logger.exception("Error renderizando capa %s", placement.layer.id)
            warnings.append(f"No se pudo renderizar '{placement.layer.name}': {exc}")
    return canvas, warnings


def save_variant_image(
    project: Project, variant_id: str, image: Image.Image
) -> tuple[str, str]:
    """Guarda PNG a tamaño real y un thumbnail JPEG. Devuelve (rel_png, rel_thumb)."""
    rel_png = f"variants/{variant_id}.png"
    target = storage.abs_path(project.project_id, rel_png)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG", optimize=True)

    rel_thumb = f"variants/{variant_id}_thumb.jpg"
    thumb = image.copy()
    thumb.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    thumb.convert("RGB").save(
        storage.abs_path(project.project_id, rel_thumb),
        format="JPEG",
        quality=94,
        subsampling=0,
    )
    return rel_png, rel_thumb


def render_detection_preview(project: Project) -> Image.Image:
    """Imagen original con bounding boxes, categorías y confianza (vista "Revisar lo detectado")."""
    path = storage.abs_path(project.project_id, project.source.path)
    canvas = load_flat_rgb(path)
    draw = ImageDraw.Draw(canvas, "RGBA")
    palette = {
        "product": (255, 92, 51),
        "person": (255, 196, 0),
        "logo": (0, 200, 255),
        "headline": (46, 204, 113),
        "subheadline": (26, 188, 156),
        "price": (231, 76, 60),
        "cta": (155, 89, 182),
        "legal": (149, 165, 166),
        "decoration": (241, 196, 15),
        "background": (100, 100, 100),
    }
    stroke = max(2, int(min(canvas.size) * 0.004))
    font = _load_font(settings.default_font_bold, max(14, int(min(canvas.size) * 0.028)))
    for layer in project.sorted_layers():
        if layer.category.value == "background":
            continue
        color = palette.get(layer.category.value, (255, 255, 255))
        box = [layer.x, layer.y, layer.x + layer.width, layer.y + layer.height]
        draw.rectangle(box, outline=(*color, 255), width=stroke)
        label = f"{layer.name} · {layer.confidence:.2f}"
        text_w = draw.textlength(label, font=font)
        pad = stroke * 2
        label_y = max(0, layer.y - int(font.size * 1.4))
        draw.rectangle(
            [layer.x, label_y, layer.x + text_w + pad * 2, label_y + int(font.size * 1.35)],
            fill=(*color, 210),
        )
        draw.text((layer.x + pad, label_y + pad // 2), label, font=font, fill=(0, 0, 0))
    return canvas


def render_mask_preview(project: Project, layer_id: str) -> Image.Image:
    """Original con la máscara de una capa resaltada (vista "Corregir un recorte")."""
    layer = project.layer_by_id(layer_id)
    if layer is None:
        raise KeyError(layer_id)
    path = storage.abs_path(project.project_id, project.source.path)
    base = load_flat_rgb(path)

    from .layer_extraction import ensure_mask

    mask = ensure_mask(project, layer, persist=False)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    mask_img = Image.fromarray(mask).convert("L").resize(base.size)
    tint = Image.new("RGBA", base.size, (0, 255, 140, 110))
    overlay.paste(tint, (0, 0), mask_img)
    composed = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(composed)
    stroke = max(2, int(min(base.size) * 0.004))
    draw.rectangle(
        [layer.x, layer.y, layer.x + layer.width, layer.y + layer.height],
        outline=(255, 255, 255),
        width=stroke,
    )
    return composed


def render_template_preview(project: Project) -> Image.Image:
    """El KV listo para recibir un producto nuevo: todo menos el producto.

    Enseñar la plancha desnuda asusta: es un degradado borroso donde antes había
    un mueble, y fuera de contexto parece un error. Compuesta con el logo, el
    precio y los legales encima se ve lo que de verdad va a pasar, que es una
    plantilla esperando el producto.
    """
    height, width = project.canvas.height, project.canvas.width
    if project.background.path:
        base = load_flat_rgb(storage.abs_path(project.project_id, project.background.path))
    else:
        base = load_flat_rgb(storage.abs_path(project.project_id, project.source.path))
    canvas = base.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)

    for layer in sorted(project.layers, key=lambda item: item.z_index):
        if layer.category in {LayerCategory.PRODUCT, LayerCategory.BACKGROUND}:
            continue
        if not layer.visible or not layer.src:
            continue
        path = storage.abs_path(project.project_id, layer.src)
        if not path.exists():
            continue
        with Image.open(path) as opened:
            pieza = opened.convert("RGBA")
            if pieza.size != (layer.width, layer.height):
                pieza = pieza.resize((layer.width, layer.height), Image.Resampling.LANCZOS)
            canvas.alpha_composite(pieza, (layer.x, layer.y))

    hueco = next(
        (item for item in project.layers if item.category == LayerCategory.PRODUCT),
        None,
    )
    if hueco is not None:
        _draw_product_slot(canvas, hueco)
    return canvas.convert("RGB")


def _draw_product_slot(canvas: Image.Image, layer) -> None:
    """Marca con trazo discontinuo dónde irá el producto.

    Sin la marca, el hueco del producto —un degradado liso donde antes había un
    mueble— se lee como una foto rota. Con ella se lee como lo que es: el espacio
    reservado.
    """
    trazo = max(2, round(min(canvas.width, canvas.height) / 360))
    guion = trazo * 6
    color = (255, 255, 255, 150)
    capa = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    pincel = ImageDraw.Draw(capa)
    x0, y0 = layer.x, layer.y
    x1, y1 = layer.x + layer.width, layer.y + layer.height
    for x in range(x0, x1, guion * 2):
        pincel.line([(x, y0), (min(x + guion, x1), y0)], fill=color, width=trazo)
        pincel.line([(x, y1), (min(x + guion, x1), y1)], fill=color, width=trazo)
    for y in range(y0, y1, guion * 2):
        pincel.line([(x0, y), (x0, min(y + guion, y1))], fill=color, width=trazo)
        pincel.line([(x1, y), (x1, min(y + guion, y1))], fill=color, width=trazo)
    canvas.alpha_composite(capa)
