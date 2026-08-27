"""Servicio de segmentación: elige proveedor y degrada sin romper el flujo."""
from __future__ import annotations

import logging

import numpy as np

from ..providers import (
    Detection,
    ProviderUnavailableError,
    get_local_segmentation_provider,
    get_manual_segmentation_provider,
    get_segmentation_provider,
)
from ..providers.local_segmentation import feather_mask, refine_mask

logger = logging.getLogger(__name__)


def active_provider_name() -> str:
    return getattr(get_segmentation_provider(), "name", "unknown")


def detect_regions(image_path: str, max_regions: int = 12) -> tuple[list[Detection], list[str]]:
    """Propone regiones. Si el proveedor activo no propone nada, usa el local."""
    warnings: list[str] = []
    provider = get_segmentation_provider()
    detections: list[Detection] = []
    try:
        try:
            detections = provider.detect(image_path, max_regions=max_regions)  # type: ignore[call-arg]
        except TypeError:
            detections = provider.detect(image_path)
    except (ProviderUnavailableError, Exception) as exc:  # noqa: BLE001
        warnings.append(f"El proveedor {active_provider_name()} falló al detectar: {exc}")
        detections = []

    if not detections and getattr(provider, "name", "") != "opencv-local":
        local = get_local_segmentation_provider()
        try:
            detections = local.detect(image_path, max_regions=max_regions)
            warnings.append(
                "El proveedor activo no propuso regiones; se usó la detección local OpenCV."
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"La detección local también falló: {exc}")

    if not detections:
        warnings.append(
            "No se detectaron regiones automáticamente. Cree las capas manualmente "
            "en Ajustes finos."
        )
    return detections, warnings


def segment_box(
    image_path: str,
    box: tuple[int, int, int, int] | None = None,
    points: list[tuple[int, int, int]] | None = None,
    text_prompt: str | None = None,
) -> tuple[np.ndarray, str, list[str]]:
    """Segmenta con cadena de respaldo: activo → local → manual (rectángulo)."""
    warnings: list[str] = []
    chain = [get_segmentation_provider(), get_local_segmentation_provider(), get_manual_segmentation_provider()]
    seen: set[str] = set()
    for provider in chain:
        name = getattr(provider, "name", "unknown")
        if name in seen:
            continue
        seen.add(name)
        try:
            mask = provider.segment(image_path, box=box, points=points, text_prompt=text_prompt)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Segmentación con {name} falló: {exc}")
            continue
        if mask is not None and int((mask > 127).sum()) > 0:
            return mask.astype(np.uint8), name, warnings
        warnings.append(f"{name} devolvió una máscara vacía.")
    raise ProviderUnavailableError("Ningún proveedor de segmentación produjo una máscara.")


def apply_mask_operations(mask: np.ndarray, operations: list) -> np.ndarray:
    """Aplica trazos de pincel (add/subtract, rect/ellipse) sobre la máscara."""
    manual = get_manual_segmentation_provider()
    result = mask
    for op in operations:
        result = manual.apply_operation(
            result,
            op=getattr(op, "op", "add"),
            shape=getattr(op, "shape", "rect"),
            x=getattr(op, "x", 0),
            y=getattr(op, "y", 0),
            width=getattr(op, "width", 1),
            height=getattr(op, "height", 1),
        )
    return result


__all__ = [
    "active_provider_name",
    "apply_mask_operations",
    "detect_regions",
    "feather_mask",
    "refine_mask",
    "segment_box",
]
