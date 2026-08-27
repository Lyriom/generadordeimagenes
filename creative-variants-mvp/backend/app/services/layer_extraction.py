"""Extracción de capas: máscara → PNG RGBA transparente con píxeles originales.

Reglas:
- Nunca se reescala el recorte (se conserva la resolución original).
- Los bordes se suavizan solo en el canal alfa.
- El archivo original nunca se modifica.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..models import Layer, LayerType, Project
from ..providers.local_segmentation import feather_mask, keep_largest_component, refine_mask
from . import storage
from .imaging import box_mask, load_alpha, load_flat_rgb, mask_bbox, read_mask, save_mask


def layer_slug(layer: Layer) -> str:
    return f"{layer.category.value}_{layer.id[:8]}"


def mask_rel_path(layer: Layer) -> str:
    return f"masks/{layer_slug(layer)}.png"


def layer_rel_path(layer: Layer) -> str:
    return f"layers/{layer_slug(layer)}.png"


def canvas_shape(project: Project) -> tuple[int, int]:
    return project.canvas.height, project.canvas.width


def ensure_mask(project: Project, layer: Layer, persist: bool = True) -> np.ndarray:
    """Devuelve la máscara de la capa (crea una rectangular si no existe)."""
    shape = canvas_shape(project)
    if layer.mask:
        try:
            mask = read_mask(storage.abs_path(project.project_id, layer.mask))
            if mask.shape[:2] == shape:
                return mask
        except (FileNotFoundError, ValueError):
            pass
    mask = box_mask(shape, (layer.x, layer.y, layer.width, layer.height))
    if persist:
        rel = mask_rel_path(layer)
        save_mask(storage.abs_path(project.project_id, rel), mask)
        layer.mask = rel
    return mask


def write_mask(project: Project, layer: Layer, mask: np.ndarray) -> str:
    rel = mask_rel_path(layer)
    save_mask(storage.abs_path(project.project_id, rel), mask)
    layer.mask = rel
    return rel


def refine_layer_mask(
    mask: np.ndarray,
    *,
    refine: bool = True,
    largest_only: bool = False,
    feather: int = 0,
) -> np.ndarray:
    result = mask
    if refine:
        result = refine_mask(result)
    if largest_only:
        result = keep_largest_component(result)
    if feather:
        result = feather_mask(result, feather)
    return result


def extract_layer(
    project: Project,
    layer: Layer,
    *,
    feather: int = 2,
    force: bool = False,
) -> tuple[bool, str | None]:
    """Genera el PNG RGBA de la capa. Devuelve (ok, advertencia)."""
    original_path = storage.abs_path(project.project_id, project.source.path)
    if not original_path.exists():
        return False, "No se encuentra el arte original."

    if layer.category.value == "background":
        return False, "El fondo no se extrae como capa (usa reconstruct-background)."

    if layer.extracted and layer.src and not force:
        existing = storage.abs_path(project.project_id, layer.src)
        if existing.exists():
            return True, None

    mask = ensure_mask(project, layer)
    alpha_mask = feather_mask(mask, feather) if feather else mask

    # Si el arte original ya trae transparencia, se respeta: alfa final = alfa ∩ máscara.
    source_alpha = load_alpha(original_path)
    if source_alpha is not None and source_alpha.shape == alpha_mask.shape:
        alpha_mask = (
            alpha_mask.astype(np.uint16) * source_alpha.astype(np.uint16) // 255
        ).astype(np.uint8)

    bbox = mask_bbox(alpha_mask, threshold=8)
    if bbox is None:
        return False, f"La capa '{layer.name}' tiene una máscara vacía."

    x, y, w, h = bbox
    rgb = load_flat_rgb(original_path).crop((x, y, x + w, y + h))

    alpha_crop = alpha_mask[y : y + h, x : x + w]
    rgba = rgb.convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha_crop, mode="L"))

    rel = layer_rel_path(layer)
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(target, format="PNG", optimize=True)

    layer.x, layer.y, layer.width, layer.height = x, y, w, h
    layer.extracted = True
    if layer.type == LayerType.TEXT:
        # El texto se rerenderiza como capa editable; el raster queda como referencia.
        layer.meta["raster_src"] = rel
        layer.meta["raster_box"] = [x, y, w, h]
    else:
        layer.src = rel
    layer.meta["alpha_coverage"] = round(float((alpha_crop > 8).mean()), 4)
    return True, None


def extract_layers(
    project: Project,
    layer_ids: list[str] | None = None,
    *,
    feather: int = 2,
    force: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Extrae varias capas. Devuelve (extraídas, omitidas, advertencias)."""
    extracted: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    targets = [
        layer
        for layer in project.layers
        if layer_ids is None or layer.id in set(layer_ids)
    ]
    for layer in targets:
        ok, warning = extract_layer(project, layer, feather=feather, force=force)
        if ok:
            extracted.append(layer.id)
        else:
            skipped.append(layer.id)
            if warning:
                warnings.append(warning)
                if warning not in layer.warnings:
                    layer.warnings.append(warning)
    return extracted, skipped, warnings


def union_mask(project: Project, layer_ids: list[str] | None = None) -> np.ndarray:
    """Máscara combinada de las capas indicadas (para reconstruir el fondo)."""
    shape = canvas_shape(project)
    total = np.zeros(shape, np.uint8)
    for layer in project.layers:
        if layer.category.value == "background":
            continue
        if layer.meta.get("external"):
            continue  # recurso subido aparte: no está en el arte original
        if layer_ids is not None and layer.id not in set(layer_ids):
            continue
        mask = ensure_mask(project, layer, persist=False)
        total = np.maximum(total, (mask > 127).astype(np.uint8) * 255)
    return total
