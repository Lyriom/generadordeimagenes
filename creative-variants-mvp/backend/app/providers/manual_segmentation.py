"""Proveedor manual: siempre funciona, no usa modelos ni heurísticas.

Genera máscaras a partir de rectángulos/elipses y permite pintar/borrar zonas.
Es el fallback definitivo cuando SAM y el proveedor local no aportan nada útil.
"""
from __future__ import annotations

import cv2
import numpy as np

from .base import Detection, ProviderUnavailableError


class ManualSegmentationProvider:
    name = "manual"

    def available(self) -> bool:
        return True

    def _size(self, image_path: str) -> tuple[int, int]:
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise ProviderUnavailableError(f"No se pudo leer la imagen: {image_path}")
        return image.shape[0], image.shape[1]

    def detect(self, image_path: str) -> list[Detection]:
        """El modo manual no propone regiones: el usuario las dibuja."""
        return []

    def segment(
        self,
        image_path: str,
        box: tuple[int, int, int, int] | None = None,
        points: list[tuple[int, int, int]] | None = None,
        text_prompt: str | None = None,
    ) -> np.ndarray:
        h, w = self._size(image_path)
        mask = np.zeros((h, w), np.uint8)
        if box is not None:
            x, y, bw, bh = (int(round(v)) for v in box)
            x = max(0, min(x, w - 1))
            y = max(0, min(y, h - 1))
            bw = max(1, min(bw, w - x))
            bh = max(1, min(bh, h - y))
            mask[y : y + bh, x : x + bw] = 255
        for px, py, label in points or []:
            radius = max(6, min(h, w) // 40)
            cv2.circle(mask, (int(px), int(py)), radius, 255 if label > 0 else 0, -1)
        return mask

    # ------------------------------------------------------------- edición
    @staticmethod
    def apply_operation(
        mask: np.ndarray,
        op: str,
        shape: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> np.ndarray:
        """Aplica un trazo (pincel rectangular o elíptico) sobre la máscara."""
        h, w = mask.shape[:2]
        value = 255 if op == "add" else 0
        x0 = max(0, min(int(x), w - 1))
        y0 = max(0, min(int(y), h - 1))
        x1 = max(x0 + 1, min(int(x) + int(width), w))
        y1 = max(y0 + 1, min(int(y) + int(height), h))
        result = mask.copy()
        if shape == "ellipse":
            center = ((x0 + x1) // 2, (y0 + y1) // 2)
            axes = (max(1, (x1 - x0) // 2), max(1, (y1 - y0) // 2))
            cv2.ellipse(result, center, axes, 0, 0, 360, value, -1)
        else:
            result[y0:y1, x0:x1] = value
        return result
