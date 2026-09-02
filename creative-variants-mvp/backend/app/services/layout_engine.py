"""Motor de layouts: reorganiza realmente las capas en cada variante.

Cada layout define zonas RELATIVAS al lienzo (x, y, width, height en 0..1), por
lo que funciona en cualquier formato. Sobre esas zonas se aplican variaciones
deterministas (semilla) de escala, alineación, espaciado, orden y fondo.

Reglas garantizadas por el motor:
- Las imágenes se ajustan con "contain": nunca se deforman ni se recortan.
- Todo queda dentro de los márgenes seguros del lienzo.
- Los solapamientos graves se resuelven por prioridad.
- El texto legal siempre permanece visible dentro del lienzo.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models import Layer, LayerCategory, LayerType, Project
from ..models.formats import format_safe_area
from ..models.schemas import SUPPORTED_FORMATS
from .imaging import fit_contain

Zone = tuple[float, float, float, float]

# --------------------------------------------------------------------- layouts
LAYOUTS: dict[str, dict[str, Any]] = {
    # Reproduce el diseño del arte original: cada elemento en su posición relativa.
    # Es la variante que un KV con el producto cambiado necesita, y la que se usa
    # cuando el formato de salida tiene una proporción parecida a la del original.
    "faithful": {
        "label": "Fiel al original (mismo diseño)",
        "keep_all_relative": True,
        "zones": {
            "logo": [0.05, 0.04, 0.20, 0.09],
            "product": [0.28, 0.22, 0.44, 0.56],
            "person": [0.26, 0.22, 0.48, 0.62],
            "headline": [0.08, 0.10, 0.84, 0.16],
            "subheadline": [0.08, 0.28, 0.84, 0.10],
            "price": [0.08, 0.72, 0.40, 0.12],
            "cta": [0.56, 0.74, 0.34, 0.09],
            "legal": [0.05, 0.93, 0.90, 0.05],
            "decoration": [0.05, 0.04, 0.28, 0.12],
        },
        "align": "center",
    },
    "product_left": {
        "label": "Producto izquierda / texto derecha",
        "zones": {
            "logo": [0.05, 0.04, 0.20, 0.09],
            "product": [0.04, 0.22, 0.48, 0.60],
            "person": [0.02, 0.24, 0.50, 0.66],
            "headline": [0.56, 0.18, 0.39, 0.22],
            "subheadline": [0.56, 0.42, 0.39, 0.14],
            "price": [0.56, 0.58, 0.30, 0.12],
            "cta": [0.56, 0.72, 0.32, 0.09],
            "legal": [0.05, 0.93, 0.90, 0.05],
            "decoration": [0.62, 0.04, 0.32, 0.12],
        },
        "align": "left",
    },
    "product_right": {
        "label": "Producto derecha / texto izquierda",
        "zones": {
            "logo": [0.05, 0.04, 0.20, 0.09],
            "product": [0.50, 0.22, 0.46, 0.60],
            "person": [0.48, 0.24, 0.50, 0.66],
            "headline": [0.05, 0.18, 0.40, 0.22],
            "subheadline": [0.05, 0.42, 0.40, 0.14],
            "price": [0.05, 0.58, 0.30, 0.12],
            "cta": [0.05, 0.72, 0.32, 0.09],
            "legal": [0.05, 0.93, 0.90, 0.05],
            "decoration": [0.05, 0.04, 0.28, 0.12],
        },
        "align": "left",
    },
    "product_center_headline_top": {
        "label": "Producto centrado / titular arriba",
        "zones": {
            "logo": [0.40, 0.03, 0.20, 0.08],
            "headline": [0.10, 0.13, 0.80, 0.16],
            "subheadline": [0.15, 0.30, 0.70, 0.09],
            "product": [0.22, 0.40, 0.56, 0.40],
            "person": [0.20, 0.38, 0.60, 0.44],
            "price": [0.06, 0.44, 0.20, 0.10],
            "cta": [0.34, 0.82, 0.32, 0.08],
            "legal": [0.05, 0.93, 0.90, 0.05],
            "decoration": [0.72, 0.30, 0.24, 0.14],
        },
        "align": "center",
    },
    "headline_center_product_bottom": {
        "label": "Titular centrado / producto abajo / CTA inferior",
        "zones": {
            "logo": [0.40, 0.04, 0.20, 0.08],
            "headline": [0.08, 0.15, 0.84, 0.18],
            "subheadline": [0.14, 0.34, 0.72, 0.08],
            "price": [0.66, 0.44, 0.28, 0.11],
            "product": [0.16, 0.44, 0.50, 0.38],
            "person": [0.14, 0.42, 0.56, 0.42],
            "cta": [0.34, 0.84, 0.32, 0.07],
            "legal": [0.05, 0.94, 0.90, 0.04],
            "decoration": [0.04, 0.44, 0.16, 0.14],
        },
        "align": "center",
    },
    "vertical_stack": {
        "label": "Composición vertical apilada",
        "zones": {
            "logo": [0.06, 0.03, 0.22, 0.07],
            "headline": [0.06, 0.12, 0.88, 0.15],
            "product": [0.18, 0.29, 0.64, 0.34],
            "person": [0.16, 0.28, 0.68, 0.38],
            "subheadline": [0.06, 0.65, 0.88, 0.08],
            "price": [0.06, 0.74, 0.40, 0.09],
            "cta": [0.54, 0.74, 0.40, 0.09],
            "legal": [0.06, 0.93, 0.88, 0.05],
            "decoration": [0.70, 0.03, 0.26, 0.08],
        },
        "align": "center",
    },
    "diagonal_flow": {
        "label": "Composición diagonal",
        "zones": {
            "logo": [0.06, 0.04, 0.20, 0.08],
            "headline": [0.06, 0.14, 0.52, 0.18],
            "product": [0.42, 0.34, 0.52, 0.42],
            "person": [0.40, 0.32, 0.56, 0.46],
            "subheadline": [0.06, 0.35, 0.34, 0.12],
            "price": [0.06, 0.50, 0.26, 0.11],
            "cta": [0.06, 0.66, 0.30, 0.09],
            "legal": [0.06, 0.93, 0.88, 0.05],
            "decoration": [0.68, 0.06, 0.28, 0.14],
        },
        "align": "left",
    },
    "split_blocks": {
        "label": "Dos bloques divididos",
        "zones": {
            "logo": [0.06, 0.05, 0.20, 0.08],
            "product": [0.08, 0.20, 0.38, 0.55],
            "person": [0.06, 0.20, 0.42, 0.58],
            "headline": [0.54, 0.16, 0.40, 0.20],
            "subheadline": [0.54, 0.38, 0.40, 0.12],
            "price": [0.54, 0.52, 0.24, 0.12],
            "cta": [0.54, 0.68, 0.34, 0.10],
            "legal": [0.06, 0.92, 0.88, 0.06],
            "decoration": [0.06, 0.80, 0.38, 0.10],
        },
        "align": "left",
    },
    "hero_product_overlay": {
        "label": "Producto grande con texto en zona segura",
        "zones": {
            "logo": [0.06, 0.03, 0.18, 0.07],
            "product": [0.12, 0.11, 0.76, 0.50],
            "person": [0.10, 0.10, 0.80, 0.52],
            "headline": [0.06, 0.63, 0.60, 0.11],
            "price": [0.70, 0.62, 0.24, 0.09],
            "subheadline": [0.06, 0.755, 0.62, 0.055],
            "cta": [0.06, 0.825, 0.34, 0.06],
            "legal": [0.06, 0.935, 0.88, 0.04],
            "decoration": [0.78, 0.03, 0.18, 0.08],
        },
        "align": "left",
    },
}

#: Tope de tamaño de fuente por categoría (fracción de la altura del lienzo).
#: Mantiene la jerarquía tipográfica en cualquier formato.
CATEGORY_FONT_CAPS: dict[LayerCategory, float] = {
    LayerCategory.HEADLINE: 0.115,
    LayerCategory.SUBHEADLINE: 0.045,
    LayerCategory.PRICE: 0.085,
    LayerCategory.CTA: 0.038,
    LayerCategory.LEGAL: 0.016,
}

#: Máximo de líneas por categoría: evita titulares partidos en columnas estrechas.
CATEGORY_MAX_LINES: dict[LayerCategory, int] = {
    LayerCategory.HEADLINE: 3,
    LayerCategory.SUBHEADLINE: 2,
    # "60% DE DESCUENTO" necesita dos líneas para verse grande; forzar una sola
    # obligaba a reducir la fuente hasta perder el impacto.
    LayerCategory.PRICE: 2,
    LayerCategory.CTA: 1,
    LayerCategory.LEGAL: 2,
}

#: Anclaje vertical del texto dentro de su zona.
CATEGORY_VALIGN: dict[LayerCategory, str] = {
    LayerCategory.HEADLINE: "top",
    LayerCategory.SUBHEADLINE: "top",
    LayerCategory.PRICE: "center",
    LayerCategory.CTA: "center",
    LayerCategory.LEGAL: "bottom",
}

# ------------------------------------------------------- adaptación por formato
# Un layout de columnas no funciona igual en 9:16 que en 16:9. Estas variantes se
# aplican automáticamente según la relación de aspecto del lienzo.
VERTICAL_OVERRIDES: dict[str, dict[str, list[float]]] = {
    "product_left": {
        "logo": [0.06, 0.03, 0.20, 0.06],
        "product": [0.08, 0.12, 0.84, 0.40],
        "person": [0.06, 0.11, 0.88, 0.43],
        "headline": [0.06, 0.56, 0.88, 0.12],
        "subheadline": [0.06, 0.69, 0.88, 0.06],
        "price": [0.06, 0.77, 0.40, 0.07],
        "cta": [0.52, 0.77, 0.42, 0.07],
        "legal": [0.06, 0.93, 0.88, 0.04],
        "decoration": [0.72, 0.03, 0.22, 0.07],
    },
    "product_right": {
        "logo": [0.06, 0.03, 0.20, 0.06],
        "headline": [0.06, 0.11, 0.88, 0.12],
        "subheadline": [0.06, 0.24, 0.88, 0.06],
        "product": [0.08, 0.33, 0.84, 0.40],
        "person": [0.06, 0.32, 0.88, 0.43],
        "price": [0.06, 0.76, 0.40, 0.07],
        "cta": [0.52, 0.76, 0.42, 0.07],
        "legal": [0.06, 0.93, 0.88, 0.04],
        "decoration": [0.72, 0.03, 0.22, 0.07],
    },
    "split_blocks": {
        "logo": [0.40, 0.03, 0.20, 0.06],
        "headline": [0.06, 0.12, 0.88, 0.13],
        "price": [0.06, 0.27, 0.36, 0.08],
        "subheadline": [0.46, 0.27, 0.48, 0.07],
        "product": [0.10, 0.38, 0.80, 0.36],
        "person": [0.08, 0.37, 0.84, 0.39],
        "cta": [0.30, 0.78, 0.40, 0.07],
        "legal": [0.06, 0.93, 0.88, 0.04],
        "decoration": [0.06, 0.03, 0.20, 0.06],
    },
    "diagonal_flow": {
        "logo": [0.06, 0.03, 0.20, 0.06],
        "headline": [0.06, 0.11, 0.70, 0.13],
        "product": [0.18, 0.28, 0.76, 0.40],
        "person": [0.16, 0.27, 0.80, 0.43],
        "subheadline": [0.06, 0.70, 0.58, 0.06],
        "price": [0.62, 0.70, 0.32, 0.07],
        "cta": [0.06, 0.79, 0.40, 0.07],
        "legal": [0.06, 0.93, 0.88, 0.04],
        "decoration": [0.70, 0.03, 0.24, 0.07],
    },
}

LANDSCAPE_OVERRIDES: dict[str, dict[str, list[float]]] = {
    "vertical_stack": {
        "logo": [0.05, 0.06, 0.14, 0.11],
        "headline": [0.05, 0.22, 0.42, 0.22],
        "product": [0.52, 0.10, 0.44, 0.72],
        "person": [0.50, 0.08, 0.46, 0.76],
        "subheadline": [0.05, 0.46, 0.42, 0.10],
        "price": [0.05, 0.58, 0.18, 0.13],
        "cta": [0.05, 0.74, 0.24, 0.11],
        "legal": [0.05, 0.90, 0.60, 0.06],
        "decoration": [0.30, 0.06, 0.16, 0.10],
    },
    "product_center_headline_top": {
        "logo": [0.44, 0.04, 0.12, 0.09],
        "headline": [0.10, 0.15, 0.80, 0.16],
        "subheadline": [0.20, 0.33, 0.60, 0.09],
        "product": [0.32, 0.42, 0.36, 0.42],
        "person": [0.30, 0.40, 0.40, 0.46],
        "price": [0.06, 0.44, 0.16, 0.13],
        "cta": [0.74, 0.46, 0.20, 0.11],
        "legal": [0.06, 0.90, 0.60, 0.06],
        "decoration": [0.06, 0.06, 0.14, 0.10],
    },
    "headline_center_product_bottom": {
        "logo": [0.44, 0.04, 0.12, 0.09],
        "headline": [0.08, 0.14, 0.84, 0.20],
        "subheadline": [0.16, 0.36, 0.68, 0.09],
        "product": [0.36, 0.44, 0.28, 0.40],
        "person": [0.34, 0.42, 0.32, 0.44],
        "price": [0.70, 0.46, 0.22, 0.13],
        "cta": [0.10, 0.50, 0.20, 0.11],
        "legal": [0.06, 0.90, 0.60, 0.06],
        "decoration": [0.08, 0.06, 0.14, 0.10],
    },
    "hero_product_overlay": {
        "logo": [0.05, 0.06, 0.14, 0.10],
        "product": [0.46, 0.08, 0.50, 0.78],
        "person": [0.44, 0.06, 0.52, 0.82],
        "headline": [0.05, 0.22, 0.38, 0.22],
        "subheadline": [0.05, 0.46, 0.38, 0.10],
        "price": [0.05, 0.58, 0.18, 0.12],
        "cta": [0.05, 0.73, 0.24, 0.11],
        "legal": [0.05, 0.90, 0.40, 0.06],
        "decoration": [0.24, 0.06, 0.16, 0.10],
    },
}

#: Umbrales de relación de aspecto (ancho/alto) para elegir el juego de zonas.
#: 0.7 cubre tanto 9:16 como 2:3 (900x1350), que también necesita apilado.
VERTICAL_ASPECT = 0.7
LANDSCAPE_ASPECT = 1.5
#: Los banners 3:1 necesitan una composición propia; tratarlos como un landscape
#: común produce prendas gigantes y tres propuestas prácticamente idénticas.
BANNER_ASPECT = 2.4

DEFAULT_ZONES: dict[str, Zone] = {
    "logo": (0.06, 0.04, 0.20, 0.09),
    "product": (0.15, 0.25, 0.70, 0.45),
    "person": (0.15, 0.25, 0.70, 0.50),
    "headline": (0.08, 0.10, 0.84, 0.14),
    "subheadline": (0.08, 0.72, 0.84, 0.08),
    "price": (0.08, 0.80, 0.35, 0.08),
    "cta": (0.55, 0.80, 0.35, 0.08),
    "legal": (0.06, 0.94, 0.88, 0.045),
    "decoration": (0.70, 0.05, 0.25, 0.10),
}

#: Prioridad al resolver solapamientos (mayor = se mantiene en su lugar).
PRIORITY: dict[LayerCategory, int] = {
    LayerCategory.LOGO: 100,
    LayerCategory.PRODUCT: 92,
    LayerCategory.PERSON: 90,
    # El legal está anclado al pie y debe permanecer visible: solo el producto,
    # la persona y el logo tienen prioridad sobre él.
    LayerCategory.LEGAL: 85,
    LayerCategory.HEADLINE: 74,
    LayerCategory.PRICE: 68,
    LayerCategory.CTA: 62,
    LayerCategory.SUBHEADLINE: 52,
    LayerCategory.DECORATION: 12,
    LayerCategory.BACKGROUND: 0,
}

BACKGROUND_STYLES = ("plate", "plate_blur", "solid", "gradient", "duotone", "plate_zoom")

INTENSITY_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "scale": (0.96, 1.04),
        "offset": 0.012,
        "allow_reorder": False,
        "allow_mirror": False,
        "align_variation": False,
        "backgrounds": ("plate",),
        "spacing": (0.98, 1.02),
    },
    "moderate": {
        "scale": (0.88, 1.12),
        "offset": 0.032,
        "allow_reorder": True,
        "allow_mirror": False,
        "align_variation": True,
        "backgrounds": ("plate", "plate_blur", "solid"),
        "spacing": (0.94, 1.08),
    },
    "creative": {
        "scale": (0.80, 1.26),
        "offset": 0.058,
        "allow_reorder": True,
        "allow_mirror": True,
        "align_variation": True,
        "backgrounds": BACKGROUND_STYLES,
        "spacing": (0.88, 1.14),
    },
}

SAFE_MARGIN = 0.035


@dataclass
class Placement:
    layer: Layer
    x: int
    y: int
    width: int
    height: int
    z_index: int
    align: str = "left"
    valign: str = "center"
    #: Capa importada que conserva su sitio del diseño original: no se recoloca.
    pinned: bool = False
    #: Escenografía a sangre autorizada a estirarse hasta el borde. Nunca se activa
    #: para producto, logo o persona: esos jamás se deforman.
    stretch: bool = False
    font_size: int | None = None
    color: str | None = None

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    @property
    def area(self) -> int:
        return max(1, self.width) * max(1, self.height)


@dataclass
class VariantPlan:
    index: int
    layout: str
    layout_label: str
    format: str
    width: int
    height: int
    seed: int
    intensity: str
    background_style: str
    placements: list[Placement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ instrucción
def parse_instruction(instruction: str | None) -> dict[str, Any]:
    """Interpreta una instrucción textual simple (palabras clave, sin IA)."""
    bias: dict[str, Any] = {
        "product_scale": 1.0,
        "text_scale": 1.0,
        "preferred_layouts": [],
        "force_align": None,
        "extra_air": 1.0,
    }
    if not instruction:
        return bias
    text = instruction.lower()

    if any(word in text for word in ("producto grande", "product big", "hero", "protagonista")):
        bias["product_scale"] *= 1.18
        bias["preferred_layouts"].append("hero_product_overlay")
    if any(word in text for word in ("producto pequeño", "producto pequeno", "product small")):
        bias["product_scale"] *= 0.85
    if any(word in text for word in ("texto grande", "titular grande", "big headline")):
        bias["text_scale"] *= 1.15
    if any(word in text for word in ("texto arriba", "titular arriba", "headline top")):
        bias["preferred_layouts"] += ["product_center_headline_top", "headline_center_product_bottom"]
    if any(word in text for word in ("centrado", "centered", "simétrico", "simetrico")):
        bias["preferred_layouts"] += ["product_center_headline_top", "vertical_stack"]
        bias["force_align"] = "center"
    if any(word in text for word in ("vertical", "apilado", "stack")):
        bias["preferred_layouts"].append("vertical_stack")
    if any(word in text for word in ("diagonal", "dinámico", "dinamico")):
        bias["preferred_layouts"].append("diagonal_flow")
    if any(word in text for word in ("dividido", "split", "dos bloques")):
        bias["preferred_layouts"].append("split_blocks")
    if any(word in text for word in ("izquierda", "left")):
        bias["preferred_layouts"].append("product_left")
    if any(word in text for word in ("derecha", "right")):
        bias["preferred_layouts"].append("product_right")
    if any(word in text for word in ("minimal", "limpio", "aire", "espacio", "respirar")):
        bias["extra_air"] = 0.9
        bias["product_scale"] *= 0.94
    return bias


# ------------------------------------------------------------------- utilidades
def zones_for_format(layout_key: str, canvas_w: int, canvas_h: int) -> dict[str, Any]:
    """Juego de zonas del layout adaptado a la forma del lienzo."""
    base = dict(LAYOUTS.get(layout_key, LAYOUTS["product_left"]).get("zones", {}))
    aspect = canvas_w / max(1, canvas_h)
    if aspect <= VERTICAL_ASPECT:
        base.update(VERTICAL_OVERRIDES.get(layout_key, {}))
    elif aspect >= LANDSCAPE_ASPECT:
        base.update(LANDSCAPE_OVERRIDES.get(layout_key, {}))
    return base


def _zone_for(zones: dict[str, Any], category: LayerCategory) -> Zone:
    raw = zones.get(category.value) or DEFAULT_ZONES.get(category.value)
    if raw is None:
        raw = DEFAULT_ZONES["decoration"]
    return tuple(float(v) for v in raw)  # type: ignore[return-value]


def _split_zone(
    zone: Zone,
    count: int,
    gap: float = 0.012,
    canvas: tuple[int, int] = (1080, 1080),
    group_aspect: float = 1.0,
    arrangement: str = "auto",
) -> list[Zone]:
    """Reparte una zona entre varias capas de la misma categoría.

    Se prueban los dos cortes (en fila y en columna) y gana el que deja las piezas
    MÁS GRANDES para la proporción del grupo. Tres prendas verticales en una franja
    ancha van una al lado de la otra; apiladas quedarían diminutas.
    """
    if count <= 1:
        return [zone]
    x, y, w, h = zone
    canvas_w, canvas_h = canvas
    total_gap = gap * (count - 1)

    slot_w_h = max(0.02, (w - total_gap) / count)  # corte en fila
    slot_h_v = max(0.02, (h - total_gap) / count)  # corte en columna

    def fitted_height(slot_w: float, slot_h: float) -> float:
        """Alto real que alcanzaría una pieza del grupo en ese hueco."""
        px_w, px_h = slot_w * canvas_w, slot_h * canvas_h
        return min(px_h, px_w / max(group_aspect, 0.01))

    if arrangement == "overlap":
        # Cada producto conserva casi toda la zona y se desplaza ligeramente. Las
        # capas se mantienen independientes, de modo que luego siguen siendo
        # editables en PSD/SVG.
        slot_w = max(0.02, w * min(0.82, 1.0 - 0.04 * (count - 1)))
        slot_h = max(0.02, h * min(0.94, 1.0 - 0.025 * (count - 1)))
        travel_x = max(0.0, w - slot_w)
        travel_y = max(0.0, h - slot_h)
        return [
            (
                x + (travel_x * index / max(1, count - 1)),
                y + (travel_y * (index % 2) / max(1, min(2, count - 1))),
                slot_w,
                slot_h,
            )
            for index in range(count)
        ]

    horizontal = fitted_height(slot_w_h, h) >= fitted_height(w, slot_h_v)
    if arrangement == "horizontal":
        horizontal = True
    elif arrangement == "vertical":
        horizontal = False
    if horizontal:
        return [(x + i * (slot_w_h + gap), y, slot_w_h, h) for i in range(count)]
    return [(x, y + i * (slot_h_v + gap), w, slot_h_v) for i in range(count)]


def _clamp_box(
    x: int, y: int, w: int, h: int, canvas_w: int, canvas_h: int, margin: int
) -> tuple[int, int, int, int]:
    """Mantiene la caja dentro del área segura sin deformarla."""
    max_w = canvas_w - 2 * margin
    max_h = canvas_h - 2 * margin
    if w > max_w:
        scale = max_w / w
        w = max(1, int(w * scale))
        h = max(1, int(h * scale))
    if h > max_h:
        scale = max_h / h
        w = max(1, int(w * scale))
        h = max(1, int(h * scale))
    x = max(margin, min(x, canvas_w - margin - w))
    y = max(margin, min(y, canvas_h - margin - h))
    return int(x), int(y), int(w), int(h)


def _safe_bounds(
    canvas_w: int, canvas_h: int, safe_area: dict[str, float] | None
) -> tuple[int, int, int, int]:
    """Rectángulo de contenido esencial de un preset de plataforma."""
    safe_area = safe_area or {}
    left = max(0, int(round(canvas_w * float(safe_area.get("left", SAFE_MARGIN)))))
    top = max(0, int(round(canvas_h * float(safe_area.get("top", SAFE_MARGIN)))))
    right = max(0, int(round(canvas_w * float(safe_area.get("right", SAFE_MARGIN)))))
    bottom = max(0, int(round(canvas_h * float(safe_area.get("bottom", SAFE_MARGIN)))))
    return left, top, max(left + 1, canvas_w - right), max(top + 1, canvas_h - bottom)


def _constrain_to_safe_bounds(
    placement: Placement, bounds: tuple[int, int, int, int]
) -> None:
    """Mueve/reduce una capa esencial para que la UI de la red no la tape."""
    x0, y0, x1, y1 = bounds
    available_w = max(1, x1 - x0)
    available_h = max(1, y1 - y0)
    old_w, old_h = placement.width, placement.height
    scale = min(1.0, available_w / max(1, old_w), available_h / max(1, old_h))
    if scale < 1.0:
        placement.width = max(1, int(round(old_w * scale)))
        placement.height = max(1, int(round(old_h * scale)))
        if placement.font_size:
            placement.font_size = max(8, int(round(placement.font_size * scale)))
    placement.x = max(x0, min(placement.x, x1 - placement.width))
    placement.y = max(y0, min(placement.y, y1 - placement.height))


def _overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0
    return (x1 - x0) * (y1 - y0)


def overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Solape relativo al elemento más pequeño (0..1)."""
    inter = _overlap_area(a, b)
    if inter == 0:
        return 0.0
    smaller = min(a[2] * a[3], b[2] * b[3])
    return inter / float(max(1, smaller))


def _pair_threshold(a: Placement, b: Placement) -> float:
    """Solape tolerado: el texto exige más separación que dos imágenes."""
    return 0.16 if (a.layer.is_text or b.layer.is_text) else 0.30


def _conflict_score(
    box: tuple[int, int, int, int], others: list[Placement], reference: Placement
) -> float:
    """Área de solape que excede el umbral frente a las capas de mayor prioridad."""
    total = 0.0
    for other in others:
        threshold = _pair_threshold(reference, other)
        ratio = overlap_ratio(box, other.box)
        if ratio > threshold:
            smaller = min(box[2] * box[3], other.width * other.height)
            total += (ratio - threshold) * smaller
    return total


def _resolve_overlaps(
    placements: list[Placement], canvas_w: int, canvas_h: int, margin: int
) -> list[str]:
    """Recoloca los elementos de menor prioridad hasta eliminar los solapes graves."""
    notes: list[str] = []
    # Las capas ancladas no se mueven. Las piezas pequeñas/medianas (sellos, CTA,
    # logos rasterizados) sí son obstáculos para el producto nuevo; solo se ignora la
    # escenografía grande o a sangre. Antes se ignoraban todas y el producto terminaba
    # encima de sellos y copies del KV.
    movable_placements = [p for p in placements if not p.pinned]
    fixed_obstacles = [
        p
        for p in placements
        if p.pinned
        and not p.stretch
        and p.width < canvas_w * 0.75
        and p.height < canvas_h * 0.75
        and (
            p.layer.category
            in {
                LayerCategory.LOGO,
                LayerCategory.HEADLINE,
                LayerCategory.SUBHEADLINE,
                LayerCategory.PRICE,
                LayerCategory.CTA,
                LayerCategory.LEGAL,
            }
            or p.area < canvas_w * canvas_h * 0.28
        )
    ]
    ordered = sorted(
        movable_placements, key=lambda p: PRIORITY.get(p.layer.category, 10), reverse=True
    )
    # Dos pasadas: la segunda corrige conflictos creados por los movimientos.
    for _ in range(2):
        for index in range(1, len(ordered)):
            low = ordered[index]
            if low.layer.category == LayerCategory.BACKGROUND:
                continue
            higher = [*fixed_obstacles, *ordered[:index]]
            if _conflict_score(low.box, higher, low) <= 0:
                continue
            _relocate(low, higher, canvas_w, canvas_h, margin)

    for i, first in enumerate(movable_placements):
        for second in movable_placements[i + 1 :]:
            if overlap_ratio(first.box, second.box) > _pair_threshold(first, second) * 2.2:
                notes.append(
                    f"'{first.layer.name}' y '{second.layer.name}' siguen solapados "
                    "tras el ajuste automático."
                )
        for fixed in fixed_obstacles:
            if overlap_ratio(first.box, fixed.box) > _pair_threshold(first, fixed) * 2.2:
                notes.append(
                    f"'{first.layer.name}' invade un elemento fijo de la plantilla."
                )
    return notes


def _relocate(
    low: Placement, higher: list[Placement], canvas_w: int, canvas_h: int, margin: int
) -> None:
    """Búsqueda local determinista: prueba huecos alrededor y reduce si hace falta."""
    gap = max(4, margin // 2)
    anchored_bottom = low.layer.category == LayerCategory.LEGAL
    origin = (low.x, low.y)

    candidates: list[tuple[int, int]] = []
    for other in higher:
        ox, oy, ow, oh = other.box
        candidates.extend(
            [
                (low.x, oy + oh + gap),
                (low.x, oy - gap - low.height),
                (ox + ow + gap, low.y),
                (ox - gap - low.width, low.y),
                (ox, oy + oh + gap),
                (ox, oy - gap - low.height),
            ]
        )
    candidates.append((margin, low.y))
    candidates.append((canvas_w - margin - low.width, low.y))

    best: tuple[float, int, tuple[int, int, int, int]] | None = None
    for shrink in (1.0, 0.86, 0.72):
        width = max(24, int(low.width * shrink))
        height = max(16, int(low.height * shrink))
        for cx, cy in candidates:
            x, y, w, h = _clamp_box(cx, cy, width, height, canvas_w, canvas_h, margin)
            if anchored_bottom:
                y = max(int(canvas_h * 0.86), min(y, canvas_h - margin - h))
            box = (x, y, w, h)
            conflict = _conflict_score(box, higher, low)
            displacement = abs(x - origin[0]) + abs(y - origin[1]) + (1 - shrink) * 400
            ranked = (conflict, int(displacement), box)
            if best is None or ranked[:2] < best[:2]:
                best = ranked
        if best is not None and best[0] <= 0:
            break

    if best is None:
        return
    _, _, (x, y, w, h) = best
    if low.layer.is_text and low.font_size and (w < low.width or h < low.height):
        ratio = min(w / max(1, low.width), h / max(1, low.height))
        low.font_size = max(10, int(low.font_size * ratio))
    low.x, low.y, low.width, low.height = x, y, w, h


def _align_x(zone_x: int, zone_w: int, item_w: int, align: str) -> int:
    if align == "center":
        return zone_x + (zone_w - item_w) // 2
    if align == "right":
        return zone_x + max(0, zone_w - item_w)
    return zone_x


# ------------------------------------------------------------------ composición
#: Categorías que conservan su posición relativa original en vez de ir a una zona.
#: Los PSD reales traen muchas decoraciones incidentales (franjas, sellos, viñetas):
#: amontonarlas en una sola zona destruye el diseño y genera solapes imposibles.
KEEP_RELATIVE_CATEGORIES = {LayerCategory.DECORATION}

#: Layout que respeta el diseño completo del arte original.
FAITHFUL_LAYOUT = "faithful"

#: Diferencia máxima de proporción para que reproducir el diseño original tenga
#: sentido. Un banner 1200x400 volcado a 1080x1920 no se "conserva": se destruye.
FAITHFUL_ASPECT_TOLERANCE = 0.18


def build_placements(
    layers: Iterable[Layer],
    layout_key: str,
    canvas_w: int,
    canvas_h: int,
    rng: random.Random,
    intensity: str = "moderate",
    bias: dict[str, Any] | None = None,
    movable: set[str] | None = None,
    resizable: set[str] | None = None,
    reorderable: set[str] | None = None,
    source_canvas: tuple[int, int] | None = None,
    safe_area: dict[str, float] | None = None,
    product_arrangement: str = "auto",
) -> tuple[list[Placement], list[str]]:
    """Coloca cada capa en la zona del layout con variación determinista."""
    layout = LAYOUTS.get(layout_key, LAYOUTS["product_left"])
    zones = zones_for_format(layout_key, canvas_w, canvas_h)
    preset = INTENSITY_PRESETS.get(intensity, INTENSITY_PRESETS["moderate"])
    bias = bias or parse_instruction(None)
    margin = int(round(SAFE_MARGIN * min(canvas_w, canvas_h) * bias.get("extra_air", 1.0)))
    notes: list[str] = []

    layers = [layer for layer in layers if layer.visible and layer.category != LayerCategory.BACKGROUND]
    by_category: dict[LayerCategory, list[Layer]] = {}
    for layer in sorted(layers, key=lambda item: item.z_index):
        by_category.setdefault(layer.category, []).append(layer)

    base_align = bias.get("force_align") or layout.get("align", "left")
    mirror = preset["allow_mirror"] and rng.random() < 0.35

    placements: list[Placement] = []
    for category, group in by_category.items():
        learned_product_zone = (
            source_canvas is not None
            and category == LayerCategory.PRODUCT
            and all(len(layer.meta.get("replacement_box") or []) == 4 for layer in group)
        )
        keep_relative = source_canvas is not None and (
            bool(layout.get("keep_all_relative"))
            or category in KEEP_RELATIVE_CATEGORIES
            # Solo los recortes confirmados del PSD conservan la composición
            # original. Las capas manuales de la misma categoría siguen editables.
            or all(layer.meta.get("mandatory_art") for layer in group)
        )
        zone = _zone_for(zones, category)
        if learned_product_zone:
            source_w, source_h = source_canvas
            bx, by, bw, bh = (int(value) for value in group[0].meta["replacement_box"])
            output_aspect = canvas_w / max(1, canvas_h)
            if output_aspect >= BANNER_ASPECT:
                # En un banner la marca/copy suele vivir a la izquierda y el CTA
                # en el extremo derecho. El producto se contiene en la franja
                # central-derecha y cada familia introduce una variación pequeña,
                # sin abandonar esa zona segura.
                banner_x = {
                    "product_left": 0.35,
                    "vertical_stack": 0.38,
                    "product_center_headline_top": 0.40,
                    "headline_center_product_bottom": 0.42,
                    "product_right": 0.44,
                }.get(layout_key, 0.39)
                if len(group) == 1:
                    bx = int(source_w * banner_x)
                    by = int(source_h * 0.12)
                    bw = int(source_w * 0.36)
                    bh = int(source_h * 0.70)
                else:
                    bx = int(source_w * 0.34)
                    by = int(source_h * 0.10)
                    bw = int(source_w * 0.48)
                    bh = int(source_h * 0.76)
                notes.append("Composición optimizada para banner panorámico.")
            elif len(group) == 1:
                # El smart object reemplazado puede haber sido un pack de varias
                # piezas. Una sola prenda no debe inflarse hasta llenar todo el
                # bloque que ocupaba el pack original.
                aspect = output_aspect
                if aspect >= LANDSCAPE_ASPECT:
                    max_w, max_h = int(source_w * 0.50), int(source_h * 0.72)
                elif aspect <= VERTICAL_ASPECT:
                    max_w, max_h = int(source_w * 0.72), int(source_h * 0.48)
                else:
                    max_w, max_h = int(source_w * 0.58), int(source_h * 0.55)
                new_w, new_h = min(bw, max_w), min(bh, max_h)
                bx += (bw - new_w) // 2
                by += (bh - new_h) // 2
                bw, bh = new_w, new_h
            zone = (bx / source_w, by / source_h, bw / source_w, bh / source_h)
            notes.append("Productos restringidos a la zona aprendida del PSD.")
        if mirror and category not in {LayerCategory.LEGAL} and not keep_relative and not learned_product_zone:
            zx, zy, zw, zh = zone
            zone = (1.0 - zx - zw, zy, zw, zh)
        if keep_relative:
            slots = [_relative_zone(layer, source_canvas) for layer in group]
        else:
            aspects = [
                layer.width / max(1, layer.height) for layer in group if not layer.is_text
            ]
            slots = _split_zone(
                zone,
                len(group),
                gap=0.012 * preset["spacing"][1],
                canvas=(canvas_w, canvas_h),
                group_aspect=sum(aspects) / len(aspects) if aspects else 1.0,
                arrangement=(
                    product_arrangement
                    if category == LayerCategory.PRODUCT
                    else "auto"
                ),
            )

        for layer, slot in zip(group, slots):
            if keep_relative:
                x, y, width, height, stretch = _pinned_box(
                    layer, source_canvas, canvas_w, canvas_h
                )
                placements.append(
                    Placement(
                        layer=layer,
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        z_index=layer.z_index,
                        align=base_align,
                        valign="center",
                        pinned=True,
                        stretch=stretch,
                        font_size=(
                            _fit_font_size(layer, width, height, canvas_h)
                            if layer.is_text
                            else None
                        ),
                        color=layer.color,
                    )
                )
                continue

            zx = int(slot[0] * canvas_w)
            zy = int(slot[1] * canvas_h)
            zw = max(24, int(slot[2] * canvas_w))
            zh = max(18, int(slot[3] * canvas_h))

            can_resize = layer.resizable and (resizable is None or layer.id in resizable)
            can_move = layer.movable and (movable is None or layer.id in movable)
            if learned_product_zone:
                can_resize = False
                can_move = False
            if keep_relative:
                # Se respeta el diseño original: sin escalado extra ni saltos.
                can_resize = False
                can_move = False

            scale = 1.0
            if can_resize:
                scale = rng.uniform(*preset["scale"])
                if category == LayerCategory.PRODUCT:
                    scale *= bias.get("product_scale", 1.0)
                elif layer.is_text:
                    scale *= bias.get("text_scale", 1.0)
            target_w = max(20, int(zw * scale))
            target_h = max(16, int(zh * scale))

            align = base_align
            if preset["align_variation"] and not bias.get("force_align"):
                align = rng.choice(["left", "center", "right"]) if layer.is_text else align

            if layer.is_text:
                width, height = target_w, target_h
                font_size = _fit_font_size(layer, width, height, canvas_h)
            else:
                # "contain": jamás se deforma ni se recorta el elemento.
                width, height = fit_contain(
                    layer.width, layer.height, target_w, target_h, allow_upscale=True
                )
                font_size = None

            valign = CATEGORY_VALIGN.get(category, "center") if layer.is_text else "center"
            x = _align_x(zx, zw, width, align if layer.is_text else "center")
            if valign == "top":
                y = zy
            elif valign == "bottom":
                y = zy + max(0, zh - height)
            else:
                y = zy + max(0, (zh - height) // 2)

            if can_move:
                jitter = preset["offset"]
                # El legal está anclado: solo admite un ajuste mínimo.
                if category == LayerCategory.LEGAL:
                    jitter *= 0.25
                x += int(rng.uniform(-jitter, jitter) * canvas_w)
                y += int(rng.uniform(-jitter, jitter) * canvas_h)

            # El texto legal se ancla al pie: debe permanecer visible.
            if category == LayerCategory.LEGAL:
                y = min(y, canvas_h - margin - height)
                y = max(int(canvas_h * 0.86), y)

            # Un elemento anclado reproduce el diseño original, que puede ir a sangre:
            # aplicarle el margen de seguridad lo encoge y desplaza toda la pieza.
            box_margin = 0 if (keep_relative or learned_product_zone) else margin
            x, y, width, height = _clamp_box(
                x, y, width, height, canvas_w, canvas_h, box_margin
            )
            placements.append(
                Placement(
                    layer=layer,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    z_index=layer.z_index,
                    align=align,
                    valign=valign,
                    pinned=keep_relative or learned_product_zone,
                    font_size=font_size,
                    color=layer.color,
                )
            )

    # ------------------------------------------------- reordenamiento de capas
    if preset["allow_reorder"]:
        protected_order = {
            LayerCategory.LOGO,
            LayerCategory.HEADLINE,
            LayerCategory.SUBHEADLINE,
            LayerCategory.PRICE,
            LayerCategory.CTA,
            LayerCategory.LEGAL,
            LayerCategory.PRODUCT,
        }
        pool = [
            p
            for p in placements
            if p.layer.reorderable
            and (reorderable is None or p.layer.id in reorderable)
            and not p.layer.locked
            and not p.pinned
            and p.layer.category not in protected_order
        ]
        if len(pool) >= 2 and rng.random() < 0.75:
            z_values = sorted(p.z_index for p in pool)
            rng.shuffle(z_values)
            for placement, z_value in zip(pool, z_values):
                placement.z_index = z_value
            notes.append("Orden visual de capas reorganizado.")

    notes.extend(_resolve_overlaps(placements, canvas_w, canvas_h, margin))
    # Decoraciones pueden ir a sangre. Producto, marca y textos deben permanecer
    # dentro del área que cada plataforma deja libre de controles y overlays.
    bounds = _safe_bounds(canvas_w, canvas_h, safe_area)
    for placement in placements:
        if placement.layer.category != LayerCategory.DECORATION:
            before = placement.box
            _constrain_to_safe_bounds(placement, bounds)
            if placement.box != before:
                notes.append(
                    f"'{placement.layer.name}' se ajustó al área segura del formato."
                )
    placements.sort(key=lambda p: (p.z_index, PRIORITY.get(p.layer.category, 10)))
    return placements, notes


#: Un elemento que cubre casi todo el arte original va a sangre: al cambiar de
#: formato debe seguir cubriendo (escala "cover"), no encogerse con el grupo.
FULL_BLEED_RATIO = 0.92


def _pinned_box(
    layer: Layer, source_canvas: tuple[int, int], canvas_w: int, canvas_h: int
) -> tuple[int, int, int, int, bool]:
    """Coloca una capa anclada conservando el diseño original.

    Un **único** factor de escala para todo (el que hace caber el arte completo), de
    modo que las distancias entre elementos se mantienen: el texto de un CTA sigue
    dentro de su píldora y una franja no tapa el logo que tenía debajo. Mezclar
    factores —posición con uno y tamaño con otro— es lo que descuadraba las piezas.

    Excepción: lo que abarcaba un eje entero (un fondo, una franja de lado a lado) se
    estira en ese eje hasta el borde. Es escenografía: un degradado o un listón
    repetido no acusan el estirado, y en cambio un hueco blanco a los lados se ve
    como un error.
    """
    source_w, source_h = source_canvas
    scale = min(canvas_w / max(1, source_w), canvas_h / max(1, source_h))
    offset_x = (canvas_w - source_w * scale) / 2
    offset_y = (canvas_h - source_h * scale) / 2

    stretch = False
    if layer.width >= source_w * FULL_BLEED_RATIO and not layer.pixel_critical:
        x, width, stretch = 0, canvas_w, True
    else:
        width = max(1, int(round(layer.width * scale)))
        x = int(round(offset_x + layer.x * scale))

    if layer.height >= source_h * FULL_BLEED_RATIO and not layer.pixel_critical:
        y, height, stretch = 0, canvas_h, True
    else:
        height = max(1, int(round(layer.height * scale)))
        y = int(round(offset_y + layer.y * scale))

    return x, y, width, height, stretch


def _relative_zone(layer: Layer, source_canvas: tuple[int, int]) -> Zone:
    """Zona equivalente a la posición que la capa ocupaba en el arte original."""
    source_w, source_h = source_canvas
    return (
        max(0.0, min(1.0, layer.x / max(1, source_w))),
        max(0.0, min(1.0, layer.y / max(1, source_h))),
        max(0.01, min(1.0, layer.width / max(1, source_w))),
        max(0.01, min(1.0, layer.height / max(1, source_h))),
    )


def _fit_font_size(layer: Layer, box_w: int, box_h: int, canvas_h: int) -> int:
    """Tamaño de partida: llena la caja pero respeta el tope de su categoría.

    El renderer reduce después hasta que el texto envuelto quepa realmente, así
    que conviene empezar optimista para aprovechar el espacio en formatos altos.
    """
    content = (layer.content or "").strip() or " "
    explicit_lines = max(1, content.count("\n") + 1)
    by_height = int(box_h / (explicit_lines * 1.2))
    cap_ratio = CATEGORY_FONT_CAPS.get(layer.category, 0.06)
    cap = int(canvas_h * cap_ratio)
    return max(10, min(by_height, cap))


# --------------------------------------------------------------------- planning
def choose_layouts(
    count: int,
    rng: random.Random,
    allowed: list[str] | None = None,
    preferred: list[str] | None = None,
) -> list[str]:
    """Reparte layouts garantizando variedad (cada familia aparece antes de repetir)."""
    default_pool = [key for key in LAYOUTS if key != FAITHFUL_LAYOUT]
    pool = [key for key in (allowed or default_pool) if key in LAYOUTS] or default_pool
    preferred = [key for key in (preferred or []) if key in pool]
    result: list[str] = []
    if preferred:
        result.extend(preferred[: max(1, count // 3)])
    cycle = list(pool)
    rng.shuffle(cycle)
    index = 0
    while len(result) < count:
        if index % len(cycle) == 0 and index > 0:
            rng.shuffle(cycle)
        result.append(cycle[index % len(cycle)])
        index += 1
    return result[:count]


def plan_variants(project: Project, request) -> tuple[list[VariantPlan], list[str]]:
    """Construye el plan completo de variantes (sin renderizar)."""
    warnings: list[str] = []
    bias = parse_instruction(getattr(request, "instruction", None))
    intensity = getattr(request, "intensity", "moderate")
    preset = INTENSITY_PRESETS.get(intensity, INTENSITY_PRESETS["moderate"])
    seed = int(getattr(request, "seed", 42))
    count = int(getattr(request, "count", 12))
    formats = list(getattr(request, "formats", ["1080x1080"]))

    hidden = set(getattr(request, "hidden_layers", []) or [])
    locked = set(getattr(request, "locked_layers", []) or [])
    movable = getattr(request, "movable_layers", None)
    resizable = getattr(request, "resizable_layers", None)
    reorderable = getattr(request, "reorderable_layers", None)
    movable_set = set(movable) if movable is not None else None
    resizable_set = set(resizable) if resizable is not None else None
    reorderable_set = set(reorderable) if reorderable is not None else None

    working_layers: list[Layer] = []
    for layer in project.layers:
        if layer.category == LayerCategory.BACKGROUND:
            continue
        if layer.id in hidden or not layer.visible:
            continue
        clone = layer.model_copy(deep=True)
        if clone.id in locked:
            clone.locked = True
        if clone.type == LayerType.IMAGE and not clone.src:
            warnings.append(
                f"La capa '{clone.name}' no tiene PNG extraído: se omite en las variantes."
            )
            continue
        if clone.type == LayerType.TEXT and not (clone.content or "").strip():
            warnings.append(f"La capa de texto '{clone.name}' está vacía: se omite.")
            continue
        working_layers.append(clone)

    if not working_layers:
        warnings.append(
            "No hay elementos utilizables. Marque y recorte al menos uno en Ajustes finos."
        )
        return [], warnings

    layout_rng = random.Random(seed)
    layout_keys = choose_layouts(
        count,
        layout_rng,
        allowed=getattr(request, "layouts", None),
        preferred=bias.get("preferred_layouts"),
    )

    # La primera variante de cada formato compatible reproduce el diseño original.
    # Así, tras cambiar el producto de un KV, siempre hay una pieza "igual al KV".
    source_aspect = project.canvas.width / max(1, project.canvas.height)
    faithful_pending: set[str] = set()
    if getattr(request, "layouts", None) is None:
        for fmt in dict.fromkeys(formats):
            fmt_w, fmt_h = SUPPORTED_FORMATS[fmt]
            if abs((fmt_w / fmt_h) / source_aspect - 1.0) <= FAITHFUL_ASPECT_TOLERANCE:
                faithful_pending.add(fmt)

    plans: list[VariantPlan] = []
    for index in range(count):
        fmt = formats[index % len(formats)]
        width, height = SUPPORTED_FORMATS[fmt]
        if fmt in faithful_pending:
            layout_key = FAITHFUL_LAYOUT
            faithful_pending.discard(fmt)
        else:
            layout_key = layout_keys[index]
        variant_seed = seed * 1000 + index
        rng = random.Random(variant_seed)
        background_style = preset["backgrounds"][index % len(preset["backgrounds"])]
        if layout_key == FAITHFUL_LAYOUT:
            # Conservar el diseño exige conservar su fondo real, no uno generado.
            background_style = "plate"

        placements, notes = build_placements(
            working_layers,
            layout_key,
            width,
            height,
            rng,
            intensity=intensity,
            bias=bias,
            movable=movable_set,
            resizable=resizable_set,
            reorderable=reorderable_set,
            source_canvas=(project.canvas.width, project.canvas.height),
            safe_area=format_safe_area(fmt),
            product_arrangement=getattr(request, "product_arrangement", "auto"),
        )
        plans.append(
            VariantPlan(
                index=index,
                layout=layout_key,
                layout_label=LAYOUTS[layout_key]["label"],
                format=fmt,
                width=width,
                height=height,
                seed=variant_seed,
                intensity=intensity,
                background_style=background_style,
                placements=placements,
                notes=notes,
            )
        )
    return plans, warnings


def layout_catalog() -> list[dict[str, str]]:
    return [{"key": key, "label": value["label"]} for key, value in LAYOUTS.items()]
