"""Fallback local de reconstrucción de fondo con OpenCV (siempre disponible).

Combina TELEA + NS y aplica una mezcla suavizada en los bordes de la máscara.
En fondos complejos el resultado es aproximado; se advierte al usuario.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .base import ProviderUnavailableError


class OpenCVInpaintProvider:
    name = "opencv"

    def available(self) -> bool:
        return True

    def fill(
        self,
        image_path: str,
        mask_path: str,
        prompt: str | None = None,
        output_path: str | None = None,
    ) -> str:
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ProviderUnavailableError(f"No se pudo leer la imagen: {image_path}")
        if mask is None:
            raise ProviderUnavailableError(f"No se pudo leer la máscara: {mask_path}")
        if mask.shape[:2] != image.shape[:2]:
            mask = cv2.resize(
                mask, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST
            )

        binary = (mask > 127).astype(np.uint8) * 255
        if binary.max() == 0:  # nada que rellenar
            result = image
        else:
            radius = max(3, int(min(image.shape[:2]) * 0.01))
            telea = cv2.inpaint(image, binary, radius, cv2.INPAINT_TELEA)
            ns = cv2.inpaint(image, binary, radius, cv2.INPAINT_NS)
            blended = cv2.addWeighted(telea, 0.55, ns, 0.45, 0)

            # Suaviza únicamente el interior reconstruido para ocultar artefactos.
            smooth = cv2.medianBlur(blended, 5)
            soft = cv2.GaussianBlur(
                binary.astype(np.float32) / 255.0, (0, 0), sigmaX=radius, sigmaY=radius
            )
            soft = np.clip(soft, 0.0, 1.0)[..., None]
            result = (smooth.astype(np.float32) * soft + blended.astype(np.float32) * (1 - soft))
            result = result.astype(np.uint8)

        target = Path(output_path) if output_path else Path(image_path).with_name("background.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(target), result)
        return str(target)
