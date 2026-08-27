"""Reemplazo del producto de un KV por otro recorte.

El caso de uso real de una agencia: un KV de campaña y muchos productos. Aquí no
se genera nada: se cambia el PNG de una capa y se recalcula su caja para que el
producto nuevo quepa donde estaba el viejo, sin deformarse.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image

from ..models import Layer, LayerCategory, LayerType, Project
from . import storage
from .imaging import fit_contain, load_rgba

logger = logging.getLogger(__name__)

#: Un recorte con menos de este porcentaje de píxeles opacos es sospechoso.
MIN_OPAQUE_RATIO = 0.005


def _gap(a: Layer, b: Layer) -> tuple[int, int]:
    """Distancia horizontal/vertical entre cajas; cero cuando se solapan."""
    dx = max(a.x - (b.x + b.width), b.x - (a.x + a.width), 0)
    dy = max(a.y - (b.y + b.height), b.y - (a.y + a.height), 0)
    return dx, dy


def _looks_like_generic_subject(layer: Layer, anchor: Layer, project: Project) -> bool:
    """Detecta objetos de producto vecinos que el PSD nombró como ``Capa 15``.

    Los KV reales agrupan varios productos como capas consecutivas y cercanas, pero
    sus nombres no dicen "producto". Solo incluimos imágenes compactas con nombre
    genérico y contacto espacial con un producto confirmado; así no se borran logos,
    copies, franjas ni sellos.
    """
    if layer.type != LayerType.IMAGE or layer.category != LayerCategory.DECORATION:
        return False
    raw_name = str(layer.meta.get("psd_name") or layer.name).strip().lower()
    if not re.match(r"^(capa|layer)\s*\d+", raw_name):
        return False
    aspect = layer.width / max(1, layer.height)
    area_ratio = layer.width * layer.height / max(
        1, project.canvas.width * project.canvas.height
    )
    if not (0.45 <= aspect <= 2.4 and 0.015 <= area_ratio <= 0.25):
        return False
    dx, dy = _gap(layer, anchor)
    proximity = max(12, int(min(project.canvas.width, project.canvas.height) * 0.04))
    return dx <= proximity and dy <= proximity


def original_product_layers(project: Project, anchor: Layer) -> list[Layer]:
    """Productos confirmados + objetos genéricos contiguos del mismo conjunto."""
    confirmed = [
        item
        for item in project.layers
        if item.id != anchor.id
        and item.category == LayerCategory.PRODUCT
        and item.visible
        and not item.meta.get("external")
    ]
    anchors = [anchor, *confirmed]
    related = [
        item
        for item in project.layers
        if item.id != anchor.id
        and item.visible
        and not item.meta.get("external")
        and any(_looks_like_generic_subject(item, candidate, project) for candidate in anchors)
    ]
    return list({item.id: item for item in [*confirmed, *related]}.values())


def candidate_layers(project: Project) -> list[Layer]:
    """Capas imagen que se pueden reemplazar, las de producto primero."""
    usable = [
        layer
        for layer in project.layers
        if layer.type == LayerType.IMAGE
        and layer.category != LayerCategory.BACKGROUND
        and layer.replaceable
    ]
    return sorted(
        usable,
        key=lambda layer: (
            0 if layer.category == LayerCategory.PRODUCT else 1,
            -layer.width * layer.height,
        ),
    )


def resolve_target(project: Project, layer_id: str | None) -> Layer:
    """Capa a reemplazar: la indicada, o el producto más grande del KV."""
    if layer_id:
        layer = project.layer_by_id(layer_id)
        if layer is None:
            raise ValueError(f"Capa inexistente: {layer_id}")
        if layer.type != LayerType.IMAGE:
            raise ValueError("Solo se puede reemplazar el contenido de una capa imagen.")
        return layer

    options = candidate_layers(project)
    if not options:
        raise ValueError(
            "El KV no tiene ninguna capa imagen reemplazable. Importe un PSD con capas "
            "o cree la capa del producto en Ajustes finos."
        )
    return options[0]


def _trim_to_content(image: Image.Image) -> tuple[Image.Image, list[str]]:
    """Recorta al contenido visible: un PNG con mucho aire dejaría el producto diminuto."""
    warnings: list[str] = []
    alpha = image.getchannel("A")
    box = alpha.getbbox()
    if box is None:
        raise ValueError("La imagen está completamente transparente.")

    opaque_ratio = sum(1 for value in alpha.getdata() if value > 8) / max(
        1, image.width * image.height
    )
    if opaque_ratio < MIN_OPAQUE_RATIO:
        raise ValueError("La imagen casi no tiene contenido visible.")
    if alpha.getextrema() == (255, 255):
        warnings.append(
            "La imagen no tiene transparencia: se verá su fondo rectangular sobre el KV. "
            "Use un PNG recortado (sin fondo)."
        )
    return image.crop(box), warnings


def _target_box(layer: Layer, doomed: list[Layer]) -> tuple[int, int, int, int]:
    """Caja a rellenar: la de la capa, o la que ocupaban todos los productos juntos.

    Si el KV mostraba tres prendas y solo queda una, esa una debe ocupar el hueco de
    las tres; si no, queda diminuta en medio de un vacío.
    """
    x1, y1 = layer.x, layer.y
    x2, y2 = layer.x + layer.width, layer.y + layer.height
    for other in doomed:
        x1 = min(x1, other.x)
        y1 = min(y1, other.y)
        x2 = max(x2, other.x + other.width)
        y2 = max(y2, other.y + other.height)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def replace_layer_image(
    project: Project, layer: Layer, source: Path, *, hide_others: bool = False
) -> list[str]:
    """Sustituye el PNG de `layer` por `source`, ajustado a su caja original.

    La máscara **no** se toca: sigue marcando dónde estaba el producto viejo, que es
    lo que permite borrarlo del fondo. La caja se recalcula con ajuste por contención
    y centrado, así que el producto nuevo nunca se deforma.
    """
    incoming, warnings = _trim_to_content(load_rgba(source))

    if hide_others:
        # Limpia el grupo cargado en la tanda anterior antes de construir uno nuevo.
        project.layers = [
            item
            for item in project.layers
            if not (
                item.id != layer.id
                and item.category == LayerCategory.PRODUCT
                and item.meta.get("external")
            )
        ]
    doomed = original_product_layers(project, layer) if hide_others else []
    detected_box = _target_box(layer, doomed)
    saved_box = layer.meta.get("replacement_box")
    if saved_box and len(saved_box) == 4:
        old_x, old_y, old_w, old_h = (int(value) for value in saved_box)
        new_x, new_y, new_w, new_h = detected_box
        box_x = min(old_x, new_x)
        box_y = min(old_y, new_y)
        box_w = max(old_x + old_w, new_x + new_w) - box_x
        box_h = max(old_y + old_h, new_y + new_h) - box_y
        layer.meta["replacement_box"] = [box_x, box_y, box_w, box_h]
    else:
        box_x, box_y, box_w, box_h = detected_box
        # Un lote sustituye esta capa varias veces. Todos los recortes deben usar el
        # hueco original del KV, no la caja (posiblemente menor) del producto anterior.
        layer.meta["replacement_box"] = [box_x, box_y, box_w, box_h]
    new_w, new_h = fit_contain(incoming.width, incoming.height, box_w, box_h)

    rel = f"layers/{layer.category.value}_{layer.id[:8]}_swap.png"
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Conserva todos los píxeles del recorte. El renderer hace una única reducción
    # al tamaño de salida; reducir aquí y ampliar después degradaba el producto.
    incoming.save(target, format="PNG", optimize=True)

    layer.meta.setdefault("original_src", layer.src)
    layer.meta["replaced_from"] = source.name[:120]
    # La máscara ya no debe seguir a la caja: describe el hueco del producto viejo.
    layer.meta["mask_edited"] = True
    layer.src = rel
    layer.extracted = True
    layer.x = box_x + (box_w - new_w) // 2
    layer.y = box_y + (box_h - new_h) // 2
    layer.width = new_w
    layer.height = new_h
    layer.preserve_aspect_ratio = True
    layer.visible = True
    layer.confidence = 1.0
    layer.warnings = warnings[:2]

    for other in doomed:
        other.visible = False
    if doomed:
        warnings.append(
            f"Se ocultaron {len(doomed) + 1} elementos de producto del KV original; "
            "la plantilla queda limpia antes de colocar el nuevo."
        )

    logger.info(
        "producto reemplazado en %s: capa %s -> %sx%s",
        project.project_id,
        layer.id,
        new_w,
        new_h,
    )
    return warnings


def append_product_image(project: Project, anchor: Layer, source: Path) -> tuple[Layer, list[str]]:
    """Añade otro recorte como capa de producto independiente para una pieza grupal."""
    incoming, warnings = _trim_to_content(load_rgba(source))
    box = anchor.meta.get("replacement_box") or [
        anchor.x, anchor.y, anchor.width, anchor.height
    ]
    box_x, box_y, box_w, box_h = (int(value) for value in box)
    new_w, new_h = fit_contain(incoming.width, incoming.height, box_w, box_h)
    layer = Layer(
        name=f"Producto añadido {sum(1 for item in project.layers if item.category == LayerCategory.PRODUCT) + 1}",
        type=LayerType.IMAGE,
        category=LayerCategory.PRODUCT,
        x=box_x + (box_w - new_w) // 2,
        y=box_y + (box_h - new_h) // 2,
        width=new_w,
        height=new_h,
        z_index=anchor.z_index + 1,
        locked=True,
        preserve_aspect_ratio=True,
        confidence=1.0,
        source="upload",
        extracted=True,
    )
    rel = f"layers/product_{layer.id[:8]}_external.png"
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    incoming.save(target, format="PNG", optimize=True)
    layer.src = rel
    layer.meta.update(
        {
            "external": True,
            "replaced_from": source.name[:120],
            "replacement_box": [box_x, box_y, box_w, box_h],
        }
    )
    project.layers.append(layer)
    return layer, warnings
