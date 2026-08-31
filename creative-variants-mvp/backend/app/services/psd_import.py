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
- Un PSD puede traer **varias piezas** en un mismo lienzo (un pliego). Photoshop
  las guarda como *artboards*, así que se detectan por su rectángulo exacto y
  cada una puede importarse como un proyecto propio. Si el PSD no usa artboards
  se detectan por geometría (bandas vacías entre piezas).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
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

#: Detección geométrica de piezas (solo si el PSD no usa artboards).
GRID_MIN_AREA_RATIO = 0.02   # una pieza ocupa al menos el 2 % del pliego
GRID_MIN_SIDE = 120          # y mide al menos 120 px de lado
GRID_MAX_SIDE_ANALYSIS = 1400  # el análisis se hace sobre una copia reducida
GRID_CLOSE_KERNEL = 5        # cierra huecos pequeños dentro de una misma pieza

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


# ------------------------------------------------------------------ piezas
@dataclass(frozen=True)
class PsdPiece:
    """Una pieza dentro de un PSD: su rectángulo y de dónde salió.

    `artboard_path` es el nombre del artboard que la contiene. Cuando existe, las
    capas se toman de su subárbol (exacto) en lugar de filtrarse por geometría.
    """

    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    origin: str  # artboard | grid | canvas
    artboard_path: str | None = None

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height

    @property
    def label(self) -> str:
        return f"{self.name} · {self.width}×{self.height}"


def _artboard_nodes(node, depth: int = 0) -> list[Any]:
    """Artboards del PSD. Photoshop los deja arriba, pero se busca un nivel más."""
    found: list[Any] = []
    for layer in node:
        if str(getattr(layer, "kind", "")) == "artboard":
            found.append(layer)
        elif depth == 0 and layer.is_group():
            found.extend(_artboard_nodes(layer, depth + 1))
    return found


def _clip_to_canvas(bbox, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = (int(value) for value in bbox)
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(canvas_w, x1), min(canvas_h, y1)
    if cx1 - cx0 < GRID_MIN_SIDE or cy1 - cy0 < GRID_MIN_SIDE:
        return None
    return cx0, cy0, cx1 - cx0, cy1 - cy0


def _reading_order(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Ordena arriba→abajo, izquierda→derecha, tolerando filas desalineadas."""
    if not boxes:
        return []
    tolerance = max(24, min(box[3] for box in boxes) // 2)
    return sorted(boxes, key=lambda box: (box[1] // max(1, tolerance), box[0]))


def _content_mask(flat: Image.Image) -> np.ndarray:
    """Máscara del contenido real: descarta el lienzo vacío entre piezas.

    Si el pliego es transparente fuera de las piezas basta el alfa. Si es opaco
    (lienzo negro o blanco), el fondo es el color dominante del borde.
    """
    rgba = flat.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
    if float((alpha > 8).mean()) < 0.995:
        return ((alpha > 8).astype(np.uint8)) * 255

    rgb = np.asarray(rgba.convert("RGB"), dtype=np.int16)
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]], axis=0)
    background = np.median(border, axis=0)
    distance = np.abs(rgb - background).sum(axis=2)
    return ((distance > 30).astype(np.uint8)) * 255


def _snap_box(
    mask: np.ndarray, box: tuple[int, int, int, int], margin: int
) -> tuple[int, int, int, int] | None:
    """Ajusta la caja al contenido real a resolución completa.

    El análisis se hace sobre una copia reducida, así que el borde puede quedar
    uno o dos píxeles corrido. Un desfase de 1 px desplaza todas las capas de la
    pieza, así que aquí se aprieta la caja mirando el original.
    """
    height, width = mask.shape[:2]
    x, y, w, h = box
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(width, x + w + margin), min(height, y + h + margin)
    window = mask[y0:y1, x0:x1]
    rows = np.where(window.any(axis=1))[0]
    columns = np.where(window.any(axis=0))[0]
    if rows.size == 0 or columns.size == 0:
        return None
    return (
        x0 + int(columns[0]),
        y0 + int(rows[0]),
        int(columns[-1] - columns[0]) + 1,
        int(rows[-1] - rows[0]) + 1,
    )


def _grid_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Piezas como componentes conexas del contenido.

    Cada pieza publicitaria trae su propia plancha de fondo, así que forma un
    bloque continuo. Los pasillos vacíos del pliego las separan.
    """
    height, width = mask.shape[:2]
    scale = min(1.0, GRID_MAX_SIDE_ANALYSIS / max(width, height))
    if scale < 1.0:
        small = cv2.resize(
            mask, (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = mask
    small = (small > 96).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (GRID_CLOSE_KERNEL, GRID_CLOSE_KERNEL)
    )
    closed = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)

    count, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    canvas_area = float(width * height)
    binary = mask > 96
    margin = int(round(GRID_CLOSE_KERNEL / scale)) + 4
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        sx, sy, sw, sh, _area = stats[index]
        box = _clip_to_canvas(
            (sx / scale, sy / scale, (sx + sw) / scale, (sy + sh) / scale), width, height
        )
        if box is None:
            continue
        snapped = _snap_box(binary, box, margin)
        if snapped is not None:
            box = snapped
        if min(box[2], box[3]) < GRID_MIN_SIDE:
            continue
        if (box[2] * box[3]) / canvas_area < GRID_MIN_AREA_RATIO:
            continue
        boxes.append(box)
    return _reading_order(boxes)


def detect_pieces(psd_path: Path, *, analyse_pixels: bool = True) -> tuple[list[PsdPiece], list[str]]:
    """Piezas que contiene el PSD. Siempre devuelve al menos una.

    `analyse_pixels=False` limita la detección a los artboards (solo lee el árbol
    de capas): es lo que usa el listado de la carpeta de ingesta, donde aplanar
    un PSD de 100 MB por archivo sería inaceptable.
    """
    warnings: list[str] = []
    available, reason = psd_available()
    if not available:
        return [], [reason or "psd-tools no disponible."]

    from psd_tools import PSDImage

    try:
        psd = PSDImage.open(psd_path)
    except Exception as exc:  # noqa: BLE001
        return [], [f"No se pudo leer el PSD: {exc}"]

    canvas_w, canvas_h = psd.width, psd.height
    whole = PsdPiece(
        index=0, name=psd_path.stem[:80], x=0, y=0,
        width=canvas_w, height=canvas_h, origin="canvas",
    )

    # 1 · Artboards: es como Photoshop guarda un pliego de varias piezas.
    artboards: list[tuple[tuple[int, int, int, int], str]] = []
    for node in _artboard_nodes(psd):
        box = _clip_to_canvas(node.bbox, canvas_w, canvas_h)
        if box is not None:
            artboards.append((box, str(node.name or "")))
    if artboards:
        by_box = {box: name for box, name in artboards}
        ordered = _reading_order(list(by_box))
        pieces = [
            PsdPiece(
                index=index,
                name=(by_box[box] or f"Pieza {index + 1}")[:80],
                x=box[0], y=box[1], width=box[2], height=box[3],
                origin="artboard",
                artboard_path=by_box[box] or None,
            )
            for index, box in enumerate(ordered)
        ]
        if len(pieces) > 1:
            warnings.append(
                f"El PSD trae {len(pieces)} piezas como artboards de Photoshop: "
                "cada una se puede importar por separado con sus capas reales."
            )
        return pieces, warnings

    if not analyse_pixels:
        return [whole], warnings

    # 2 · Sin artboards: se buscan las piezas por geometría sobre el aplanado.
    try:
        flat = psd.composite()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"No se pudo aplanar el PSD para detectar piezas: {exc}")
        return [whole], warnings
    if flat is None:
        return [whole], warnings

    boxes = _grid_boxes(_content_mask(flat))
    if len(boxes) <= 1:
        return [whole], warnings

    warnings.append(
        f"El PSD no usa artboards, pero se detectaron {len(boxes)} piezas separadas "
        "por espacio vacío. Revise el recorte antes de producir."
    )
    return [
        PsdPiece(
            index=index,
            name=f"Pieza {index + 1}",
            x=box[0], y=box[1], width=box[2], height=box[3],
            origin="grid",
        )
        for index, box in enumerate(boxes)
    ], warnings


def piece_preview(psd_path: Path, piece: PsdPiece, max_side: int = 420) -> Image.Image:
    """Miniatura de una pieza, para elegirla antes de importar."""
    from psd_tools import PSDImage

    psd = PSDImage.open(psd_path)
    flat = psd.composite()
    if flat is None:
        raise ValueError("La pieza no tiene contenido visible.")
    image = flat.convert("RGB").crop(piece.box)
    scale = min(1.0, max_side / max(image.width, image.height))
    if scale < 1.0:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return image


# --------------------------------------------------------------------- aplanado
def flatten_with_pillow(
    psd_path: Path, target_png: Path, box: tuple[int, int, int, int] | None = None
) -> tuple[int, int]:
    """Composición plana del PSD usando Pillow (siempre disponible)."""
    with Image.open(psd_path) as image:
        flat = image.convert("RGB")
        if box is not None:
            flat = flat.crop(box)
        target_png.parent.mkdir(parents=True, exist_ok=True)
        flat.save(target_png, format="PNG", optimize=True)
        return flat.width, flat.height


def flatten_psd(
    psd_path: Path, target_png: Path, box: tuple[int, int, int, int] | None = None
) -> tuple[int, int]:
    """Aplana el PSD con psd-tools si está, con Pillow si no.

    `box` recorta el resultado a una pieza del pliego (coordenadas del PSD).
    """
    available, _ = psd_available()
    if not available:
        return flatten_with_pillow(psd_path, target_png, box)
    try:
        from psd_tools import PSDImage

        psd = PSDImage.open(psd_path)
        flat = psd.composite()
        if flat is None:
            raise ValueError("composición vacía")
        target_png.parent.mkdir(parents=True, exist_ok=True)
        rgb = flat.convert("RGB")
        # Ojo: `PSDImage.composite(viewport=…)` ignora el viewport y devuelve el
        # lienzo completo (a nivel de capa sí lo respeta). Por eso se recorta aquí.
        if box is not None:
            rgb = rgb.crop(box)
        rgb.save(target_png, format="PNG", optimize=True)
        return rgb.width, rgb.height
    except Exception as exc:  # noqa: BLE001
        logger.warning("psd-tools no pudo aplanar %s (%s); se usa Pillow", psd_path, exc)
        return flatten_with_pillow(psd_path, target_png, box)


def crop_flat(flat_png: Path, target_png: Path, box: tuple[int, int, int, int]) -> tuple[int, int]:
    """Recorta una pieza de un aplanado ya generado.

    Componer un PSD de 100 MB cuesta segundos; con cuatro piezas se haría cuatro
    veces. Se aplana una sola vez y cada pieza sale de un recorte en PNG.
    """
    with Image.open(flat_png) as flat:
        piece = flat.convert("RGB").crop(box)
        target_png.parent.mkdir(parents=True, exist_ok=True)
        piece.save(target_png, format="PNG", optimize=True)
        return piece.width, piece.height


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


def _clip_box(
    bbox, view: tuple[int, int, int, int]
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """Recorta la capa al viewport.

    Devuelve (caja_relativa_al_viewport, viewport_absoluto_de_render). Lo primero
    son las coordenadas que ve el proyecto; lo segundo, lo que hay que pedirle a
    psd-tools, que trabaja siempre en coordenadas del PSD.
    """
    vx0, vy0, vx1, vy1 = view
    x0, y0, x1, y1 = (int(v) for v in bbox)
    cx0, cy0 = max(vx0, x0), max(vy0, y0)
    cx1, cy1 = min(vx1, x1), min(vy1, y1)
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    return (cx0 - vx0, cy0 - vy0, cx1 - cx0, cy1 - cy0), (cx0, cy0, cx1, cy1)


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
    piece: PsdPiece | None = None,
) -> tuple[list[Layer], list[str]]:
    """Crea las capas del proyecto a partir del PSD. Devuelve (capas, avisos).

    `psd_path` es absoluta: el PSD puede vivir en la carpeta de ingesta y no hace
    falta copiar 90 MB dentro del proyecto.

    `piece` limita la importación a una pieza del pliego: las capas se recortan a
    su rectángulo y sus coordenadas quedan relativas a ella, de modo que el
    proyecto resultante mide lo que mide la pieza y no el pliego completo.
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

    view = piece.box if piece is not None else (0, 0, psd.width, psd.height)
    view_w, view_h = view[2] - view[0], view[3] - view[1]
    if (view_w, view_h) != (canvas_w, canvas_h):
        origen = f"La pieza '{piece.name}'" if piece is not None else "El PSD"
        warnings.append(
            f"{origen} mide {view_w}x{view_h} y el lienzo del proyecto "
            f"{canvas_w}x{canvas_h}: se recorta al lienzo."
        )

    root = psd
    if piece is not None and piece.artboard_path:
        # El artboard es un grupo real: sus capas son exactamente las de la pieza.
        match = next(
            (
                node
                for node in _artboard_nodes(psd)
                if str(node.name or "") == piece.artboard_path
            ),
            None,
        )
        if match is not None:
            root = match
        else:
            warnings.append(
                f"No se encontró el artboard '{piece.artboard_path}': las capas de la "
                "pieza se filtran por posición."
            )

    if root is psd:
        leaves = _leaf_layers(psd)
    else:
        board_opacity = max(0.0, min(1.0, float(getattr(root, "opacity", 255) or 255) / 255.0))
        leaves = _leaf_layers(root, str(root.name or ""), board_opacity)
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
        clipped = _clip_box(psd_layer.bbox, view)
        if clipped is None:
            empty += 1
            continue
        (x, y, width, height), render_view = clipped

        try:
            rendered = psd_layer.composite(viewport=render_view)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"No se pudo renderizar la capa '{name}': {exc}")
            continue
        if rendered is None:
            empty += 1
            continue

        rgba = _apply_opacity(rendered.convert("RGBA"), opacity)
        if rgba.size != (width, height):
            fitted = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            fitted.paste(rgba, (0, 0))
            rgba = fitted
        alpha = np.asarray(rgba.split()[-1], dtype=np.uint8)
        opaque_ratio = float((alpha > 8).mean()) if alpha.size else 0.0
        if opaque_ratio < settings.psd_min_opaque_ratio:
            empty += 1
            continue

        category, confidence = classify_layer_name(name, group_path)
        if category is None or _is_generic(name):
            geometry_category, geometry_confidence = classify_by_geometry(
                (x, y, width, height), (canvas_w, canvas_h), assigned, opaque_ratio
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
