"""Interfaces comunes de proveedores (segmentación, OCR e inpainting)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np


class ProviderUnavailableError(RuntimeError):
    """El proveedor no puede usarse en este entorno (falta modelo, clave o librería)."""


@dataclass
class Detection:
    """Región detectada en la imagen original (coordenadas en píxeles)."""

    x: int
    y: int
    width: int
    height: int
    score: float = 0.5
    label: str | None = None
    kind: str = "region"  # region | subject | text
    mask: np.ndarray | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)


@dataclass
class TextRegion:
    """Texto reconocido por OCR."""

    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float = 0.0
    angle: float = 0.0
    color: str = "#FFFFFF"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class OcrResult:
    regions: list[TextRegion] = field(default_factory=list)
    provider: str = "none"
    available: bool = True
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class SegmentationProvider(Protocol):
    """Contrato de segmentación usado por el resto del backend."""

    name: str

    def available(self) -> bool: ...

    def detect(self, image_path: str) -> list[Detection]:
        """Propone regiones de interés sobre la imagen completa."""
        ...

    def segment(
        self,
        image_path: str,
        box: tuple[int, int, int, int] | None = None,
        points: list[tuple[int, int, int]] | None = None,
        text_prompt: str | None = None,
    ) -> np.ndarray:
        """Devuelve una máscara uint8 (0-255) del tamaño de la imagen."""
        ...


@runtime_checkable
class OcrProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def read(self, image_path: str) -> OcrResult: ...


@runtime_checkable
class InpaintingProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fill(
        self,
        image_path: str,
        mask_path: str,
        prompt: str | None = None,
        output_path: str | None = None,
    ) -> str:
        """Rellena la zona blanca de la máscara y devuelve la ruta del resultado."""
        ...
