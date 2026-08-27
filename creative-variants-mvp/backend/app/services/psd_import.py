"""Importación de PSD con capas reales.

Un PSD **sí** contiene las capas originales, así que aquí no se adivina nada: se
importa cada capa con su recorte exacto, su alfa, su posición y su orden. Es la
diferencia entre descomponer (aproximado) e importar (exacto).

- `psd-tools` es la librería usada; si no está instalada, solo se aplana el PSD
  con Pillow y se avisa.
- Cada capa se renderiza recortada al lienzo (`viewport`): los objetos
  inteligentes suelen tener bounding boxes gigantes fuera del arte y
  renderizarlos completos agota la memoria.
- Las capas de tipo texto llegan con su contenido real y se vuelven editables.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..config import settings
from ..models import (
    BackgroundInfo,
    CATEGORY_LABELS_ES,
    Layer,
    LayerCategory,
    LayerType,
    Project,
    utcnow,
)
from . import storage
from .imaging import save_mask

logger = logging.getLogger(__name__)

PSD_MAGIC = b"8BPS"

# Piezas de identidad y venta que se conservan exactamente como se diseñaron en
# Photoshop. Aunque Photoshop permita leer el contenido de una capa de texto,
# redibujarla sin la fuente original altera kerning, saltos, efectos y jerarquía.
MANDATORY_ART_CATEGORIES = {
    LayerCategory.LOGO,
    LayerCategory.HEADLINE,
    LayerCategory.SUBHEADLINE,
    LayerCategory.PRICE,
    LayerCategory.CTA,
    LayerCategory.LEGAL,
}

#: Palabras clave por categoría aplicadas al nombre de la capa.
#: Los tokens cortos se comparan con límite de palabra para no dar falsos positivos
#: ("off" dentro de otra palabra, "cta" dentro de "actual", etc.).
NAME_RULES: tuple[tuple[LayerCategory, tuple[str, ...]], ...] = (
    (LayerCategory.LEGAL, ("legal", "termino", "condicion", "restriccion", "vigencia")),
    (LayerCategory.PRICE, ("precio", "price", "descuento", "dcto", "off", "%", "hasta")),
    (LayerCategory.CTA, ("cta", "boton", "button", "comprar", "compra", "shop")),
    (LayerCategory.HEADLINE, ("titular", "headline", "title", "claim")),
    (LayerCategory.SUBHEADLINE, ("bajada", "subtitulo", "subhead")),
    (LayerCategory.LOGO, ("logo", "marca", "brand", "isotipo")),
    (LayerCategory.PERSON, ("modelo", "persona", "people", "model")),
    (
        LayerCategory.PRODUCT,
        ("producto", "product", "zapato", "shoe", "ropa", "prenda", "accesorio"),
    ),
    (LayerCategory.BACKGROUND, ("fondo", "background", "relleno de color", "bg", "backplate")),
    (LayerCategory.DECORATION, ("sello", "cece", "adorno", "franja")),
)

#: Nombres genéricos de Photoshop que no aportan información.
GENERIC_NAMES = (
    "capa", "layer", "objeto inteligente", "smart object", "grupo", "group",
    "rectangulo", "rectangle", "shape", "elipse", "ellipse", "copia", "copy",
)


def psd_available() -> tuple[bool, str | None]:
    """(disponible, motivo si no lo está). No importa la librería si no hace falta."""
    import importlib.util

    if importlib.util.find_spec("psd_tools") is None:
        return False, (
            "psd-tools no está instalado: el PSD se aplana pero no se importan sus capas."
        )
    return True, None


def is_psd(payload_head: bytes) -> bool:
    return payload_head.startswith(PSD_MAGIC)


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", text or "")
    ascii_only = raw.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_only).strip()


def _is_generic(name: str) -> bool:
    normalized = _normalize(name)
    stripped = re.sub(r"[\d\s]+", " ", normalized).strip()
    return any(stripped.startswith(token) for token in GENERIC_NAMES)


def _matches(haystack: str, keyword: str) -> bool:
    if len(keyword) <= 4 and keyword.isalpha():
        return re.search(rf"\b{re.escape(keyword)}\b", haystack) is not None
    return keyword in haystack


def classify_layer_name(name: str, group_path: str = "") -> tuple[LayerCategory | None, float]:
    """Categoría deducida del nombre de la capa.

    El nombre propio manda. Del grupo se ignora el primer segmento: en los PSD
    reales es el artboard y describe la pieza completa ("CYBER-MS-1200X400-ROPA"),
    así que contagiaría su categoría a todas las capas de dentro.
    """
    own = _normalize(name)
    inner_groups = _normalize("/".join(str(group_path).split("/")[1:]))

    for haystack, confidence in ((own, 0.8), (inner_groups, 0.6)):
        if not haystack:
            continue
        # Los sellos de certificación no son el logo de la marca.
        if any(token in haystack for token in ("cece", "sello", "camara")):
            return LayerCategory.DECORATION, confidence
        for category, keywords in NAME_RULES:
            if any(_matches(haystack, keyword) for keyword in keywords):
                return category, confidence
    return None, 0.0


def classify_by_geometry(
    box: tuple[int, int, int, int],
    canvas: tuple[int, int],
    assigned: dict[LayerCategory, int],
    opaque_ratio: float = 1.0,
) -> tuple[LayerCategory, float]:
    """Respaldo cuando el nombre no dice nada (muy común en PSD reales).

    `opaque_ratio` evita el error clásico: una capa que ocupa todo el lienzo pero
    está casi vacía es un elemento superpuesto, no el fondo.
    """
    canvas_w, canvas_h = canvas
    x, y, w, h = box
    area_ratio = (w * h) / float(max(1, canvas_w * canvas_h))
    cy = (y + h / 2) / canvas_h
    cx = (x + w / 2) / canvas_w
    aspect = w / max(1, h)

    height_ratio = h / canvas_h

    if area_ratio >= 0.92 and opaque_ratio >= 0.85:
        return LayerCategory.BACKGROUND, 0.6
    # Bloque ancho y bajo: es una línea de texto o una franja, no un producto.
    if aspect >= 2.5 and height_ratio < 0.22:
        return LayerCategory.DECORATION, 0.45
    if area_ratio < 0.05 and (cy < 0.22 or cy > 0.85) and (cx < 0.32 or cx > 0.68):
        if LayerCategory.LOGO not in assigned:
            return LayerCategory.LOGO, 0.45
        return LayerCategory.DECORATION, 0.4
    # Proporción de objeto y altura relevante: candidato razonable a producto.
    if 0.35 <= aspect <= 2.4 and height_ratio >= 0.3 and area_ratio <= 0.6:
        return LayerCategory.PRODUCT, 0.5
    return LayerCategory.DECORATION, 0.35


# --------------------------------------------------------------------- aplanado
def flatten_with_pillow(psd_path: Path, target_png: Path) -> tuple[int, int]:
    """Composición plana del PSD usando Pillow (siempre disponible)."""
    with Image.open(psd_path) as image:
        flat = image.convert("RGB")
        target_png.parent.mkdir(parents=True, exist_ok=True)
        flat.save(target_png, format="PNG", optimize=True)
        return flat.width, flat.height


def flatten_psd(psd_path: Path, target_png: Path) -> tuple[int, int]:
    """Aplana el PSD con psd-tools si está, con Pillow si no."""
    available, _ = psd_available()
    if not available:
        return flatten_with_pillow(psd_path, target_png)
    try:
        from psd_tools import PSDImage

        psd = PSDImage.open(psd_path)
        flat = psd.composite()
        if flat is None:
            raise ValueError("composición vacía")
        target_png.parent.mkdir(parents=True, exist_ok=True)
        flat.convert("RGB").save(target_png, format="PNG", optimize=True)
        return psd.width, psd.height
    except Exception as exc:  # noqa: BLE001
        logger.warning("psd-tools no pudo aplanar %s (%s); se usa Pillow", psd_path, exc)
        return flatten_with_pillow(psd_path, target_png)


# ------------------------------------------------------------------ importación
def _leaf_layers(
    node, group_path: str = "", inherited_opacity: float = 1.0
) -> list[tuple[str, str, Any, float]]:
    """Recorre el árbol y devuelve (nombre, ruta_grupo, capa, opacidad_efectiva).

    La opacidad se acumula: una capa al 87 % dentro de un grupo al 50 % pinta al 43 %.
    """
    result: list[tuple[str, str, Any, float]] = []
    for layer in node:
        name = str(layer.name or "")
        own = max(0.0, min(1.0, float(getattr(layer, "opacity", 255) or 255) / 255.0))
        if layer.is_group():
            child_path = f"{group_path}/{name}" if group_path else name
            result.extend(_leaf_layers(layer, child_path, inherited_opacity * own))
        else:
            result.append((name, group_path, layer, inherited_opacity * own))
    return result


def _apply_opacity(rgba: Image.Image, opacity: float) -> Image.Image:
    """Aplica la opacidad de capa del PSD al alfa.

    `composite()` de psd-tools devuelve la capa a plena intensidad: sin esto, una
    forma decorativa al 34 % se ve tres veces más marcada que en el diseño original.
    """
    if opacity >= 0.999:
        return rgba
    alpha = rgba.getchannel("A").point(lambda value: int(round(value * opacity)))
    rgba.putalpha(alpha)
    return rgba


def _clip_box(bbox, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = (int(v) for v in bbox)
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(canvas_w, x1), min(canvas_h, y1)
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    return cx0, cy0, cx1 - cx0, cy1 - cy0


def _text_content(layer) -> str | None:
    try:
        raw = layer.text
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    cleaned = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{2,}", "\n", cleaned) or None


def import_psd_layers(
    project: Project,
    psd_path: Path,
    *,
    max_layers: int | None = None,
) -> tuple[list[Layer], list[str]]:
    """Crea las capas del proyecto a partir del PSD. Devuelve (capas, avisos).

    `psd_path` es absoluta: el PSD puede vivir en la carpeta de ingesta y no hace
    falta copiar 90 MB dentro del proyecto.
    """
    warnings: list[str] = []
    available, reason = psd_available()
    if not available:
        return [], [reason or "psd-tools no disponible."]

    from psd_tools import PSDImage

    max_layers = max_layers or settings.psd_max_layers
    canvas_w, canvas_h = project.canvas.width, project.canvas.height

    try:
        psd = PSDImage.open(psd_path)
    except Exception as exc:  # noqa: BLE001
        return [], [f"No se pudo leer el PSD: {exc}"]

    if (psd.width, psd.height) != (canvas_w, canvas_h):
        warnings.append(
            f"El PSD mide {psd.width}x{psd.height} y el lienzo del proyecto "
            f"{canvas_w}x{canvas_h}: se recorta al lienzo."
        )

    leaves = _leaf_layers(psd)
    if len(leaves) > max_layers:
        warnings.append(
            f"El PSD tiene {len(leaves)} capas; se importan las {max_layers} superiores "
            "para mantener el proyecto manejable."
        )
        leaves = leaves[-max_layers:]

    layers: list[Layer] = []
    assigned: dict[LayerCategory, int] = {}
    used_names: dict[str, int] = {}
    background_parts: list[tuple[Image.Image, tuple[int, int]]] = []
    hidden = 0
    empty = 0

    for index, (name, group_path, psd_layer, opacity) in enumerate(leaves):
        if not psd_layer.visible:
            hidden += 1
            continue
        box = _clip_box(psd_layer.bbox, canvas_w, canvas_h)
        if box is None:
            empty += 1
            continue
        x, y, width, height = box

        try:
            rendered = psd_layer.composite(viewport=(x, y, x + width, y + height))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"No se pudo renderizar la capa '{name}': {exc}")
            continue
        if rendered is None:
            empty += 1
            continue

        rgba = _apply_opacity(rendered.convert("RGBA"), opacity)
        alpha = np.asarray(rgba.split()[-1], dtype=np.uint8)
        opaque_ratio = float((alpha > 8).mean()) if alpha.size else 0.0
        if opaque_ratio < settings.psd_min_opaque_ratio:
            empty += 1
            continue

        category, confidence = classify_layer_name(name, group_path)
        if category is None or _is_generic(name):
            geometry_category, geometry_confidence = classify_by_geometry(
                box, (canvas_w, canvas_h), assigned, opaque_ratio
            )
            if category is None:
                category, confidence = geometry_category, geometry_confidence

        # Solo una capa realmente opaca puede hacer de plancha de fondo.
        if category == LayerCategory.BACKGROUND and opaque_ratio < 0.8:
            category, confidence = LayerCategory.DECORATION, 0.4

        kind = getattr(psd_layer, "kind", "")
        text = _text_content(psd_layer) if kind == "type" else None

        # El fondo no es una capa movible: se acumula en la plancha limpia.
        if category == LayerCategory.BACKGROUND:
            background_parts.append((rgba, (x, y)))
            continue

        assigned[category] = assigned.get(category, 0) + 1
        label = CATEGORY_LABELS_ES[category]
        readable = name if not _is_generic(name) else label
        used_names[readable] = used_names.get(readable, 0) + 1
        display = readable if used_names[readable] == 1 else f"{readable} {used_names[readable]}"

        preserve_as_art = category in MANDATORY_ART_CATEGORIES
        layer = Layer(
            name=display[:80],
            # El contenido queda en metadatos para una futura edición, pero la
            # variante usa el recorte real del PSD y no una imitación tipográfica.
            type=LayerType.IMAGE if preserve_as_art else (LayerType.TEXT if text else LayerType.IMAGE),
            category=category,
            x=x,
            y=y,
            width=width,
            height=height,
            z_index=index,
            locked=category in MANDATORY_ART_CATEGORIES
            | {LayerCategory.PRODUCT, LayerCategory.PERSON},
            preserve_aspect_ratio=True,
            confidence=round(min(0.95, confidence + 0.15), 3),  # el PSD da recortes exactos
            source="upload",
            content=text,
            extracted=True,
        )
        layer.meta.update(
            {
                "psd_kind": kind,
                "psd_name": name,
                "psd_group": group_path,
                "psd_index": index,
                "opaque_ratio": round(opaque_ratio, 4),
                "mandatory_art": preserve_as_art,
                "editable_content": text if preserve_as_art and text else None,
            }
        )
        if text and not preserve_as_art:
            layer.font_size = max(10, int(height * 0.8))
            layer.warnings.append(
                "Texto importado del PSD: revise tipografía, tamaño y color."
            )

        # PNG de la capa (recorte exacto con su alfa) y máscara a tamaño de lienzo.
        rel_png = f"layers/{category.value}_{layer.id[:8]}.png"
        target = storage.abs_path(project.project_id, rel_png)
        target.parent.mkdir(parents=True, exist_ok=True)
        rgba.save(target, format="PNG", optimize=True)
        layer.src = rel_png

        mask = np.zeros((canvas_h, canvas_w), np.uint8)
        mask[y : y + height, x : x + width] = alpha
        layer.mask = f"masks/{category.value}_{layer.id[:8]}.png"
        save_mask(storage.abs_path(project.project_id, layer.mask), mask)

        layers.append(layer)

    # ------------------------------------------------------- plancha de fondo
    if background_parts:
        plate = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
        for image, position in background_parts:
            plate.paste(image, position, image)
        rel_bg = "backgrounds/background.png"
        plate.save(storage.abs_path(project.project_id, rel_bg), format="PNG", optimize=True)
        project.background = BackgroundInfo(
            path=rel_bg,
            provider="psd",
            generated_at=utcnow(),
            warnings=[
                "Fondo tomado directamente de las capas de fondo del PSD: no hace falta "
                "reconstruirlo con inpainting."
            ],
        )
        warnings.append(
            f"Se usaron {len(background_parts)} capa(s) de fondo del PSD como plancha limpia."
        )

    if hidden:
        warnings.append(f"Se omitieron {hidden} capa(s) oculta(s) en el PSD.")
    if empty:
        warnings.append(f"Se omitieron {empty} capa(s) vacía(s) o fuera del lienzo.")
    if not layers:
        warnings.append(
            "No se pudo importar ninguna capa utilizable del PSD: trabaje sobre la "
            "versión aplanada con el flujo manual."
        )
    else:
        warnings.append(
            f"Se importaron {len(layers)} capas del PSD con recortes exactos. Revise las "
            "categorías en Ajustes finos: los nombres de capa de Photoshop suelen ser genéricos."
        )
    return layers, warnings
