"""Fábricas de proveedores con degradación automática.

Orden de preferencia:
- Segmentación: SAM (si está habilitado y el checkpoint existe) → local OpenCV → manual.
- OCR: PaddleOCR (si está instalado) → ninguno (advertencia + creación manual).
- Inpainting: Magnific / OpenAI / FLUX / Adobe (si hay credenciales) → OpenCV.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from ..config import settings
from .adobe_inpaint import AdobeInpaintProvider
from .base import (  # noqa: F401
    Detection,
    InpaintingProvider,
    OcrProvider,
    OcrResult,
    ProviderUnavailableError,
    SegmentationProvider,
    TextRegion,
)
from .flux_inpaint import FluxInpaintProvider
from .local_segmentation import LocalSegmentationProvider
from .magnific import MagnificInpaintProvider, model_catalog as magnific_catalog
from .manual_segmentation import ManualSegmentationProvider
from .opencv_inpaint import OpenCVInpaintProvider
from .openai_inpaint import OpenAIInpaintProvider
from .paddle_ocr import PaddleOcrProvider
from .sam_segmentation import SamSegmentationProvider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_segmentation_provider() -> Any:
    choice = settings.segmentation_provider
    if choice == "manual":
        return ManualSegmentationProvider()
    if choice in {"sam", "auto"}:
        sam = SamSegmentationProvider()
        if sam.available():
            logger.info("Segmentación: SAM habilitado (%s)", sam.checkpoint)
            return sam
        if choice == "sam":
            logger.warning(
                "SEGMENTATION_PROVIDER=sam pero SAM no está disponible (%s). "
                "Se usa el proveedor local.",
                sam.load_error,
            )
    return LocalSegmentationProvider()


@lru_cache(maxsize=1)
def get_manual_segmentation_provider() -> ManualSegmentationProvider:
    """El proveedor manual siempre está disponible como último recurso."""
    return ManualSegmentationProvider()


@lru_cache(maxsize=1)
def get_local_segmentation_provider() -> LocalSegmentationProvider:
    return LocalSegmentationProvider()


@lru_cache(maxsize=1)
def get_ocr_provider() -> PaddleOcrProvider:
    return PaddleOcrProvider()


def get_inpainting_provider(
    preferred: str | None = None, model: str | None = None
) -> Any:
    """Devuelve el proveedor pedido; `model` solo aplica al catálogo de Magnific."""
    choice = (preferred or settings.inpainting_provider or "auto").lower()
    if choice == "opencv":
        return OpenCVInpaintProvider()
    if choice in {"magnific", "auto"}:
        magnific = MagnificInpaintProvider(model=model)
        if magnific.available():
            return magnific
        if choice == "magnific":
            logger.warning(
                "INPAINTING_PROVIDER=magnific sin MAGNIFIC_API_KEY (o modelo "
                "desconocido): se usa OpenCV."
            )
    if choice in {"openai", "auto"}:
        openai = OpenAIInpaintProvider()
        if openai.available():
            return openai
        if choice == "openai":
            logger.warning("INPAINTING_PROVIDER=openai sin OPENAI_API_KEY: se usa OpenCV.")
    if choice in {"flux", "auto"}:
        flux = FluxInpaintProvider()
        if flux.available():
            return flux
        if choice == "flux":
            logger.warning("INPAINTING_PROVIDER=flux sin BFL_API_KEY: se usa OpenCV.")
    if choice in {"adobe", "auto"}:
        adobe = AdobeInpaintProvider()
        if adobe.available():
            return adobe
        if choice == "adobe":
            logger.warning("INPAINTING_PROVIDER=adobe sin credenciales: se usa OpenCV.")
    return OpenCVInpaintProvider()


def provider_status() -> dict[str, dict[str, Any]]:
    """Estado legible para /health y /capabilities (sin exponer claves)."""
    sam = SamSegmentationProvider()
    sam_ok = sam.available()
    ocr = get_ocr_provider()
    ocr_ok = ocr.available()
    flux = FluxInpaintProvider()
    adobe = AdobeInpaintProvider()
    openai = OpenAIInpaintProvider()
    magnific = MagnificInpaintProvider()
    active_segmentation = get_segmentation_provider()
    active_inpainting = get_inpainting_provider()
    return {
        "segmentation": {
            "active": getattr(active_segmentation, "name", "unknown"),
            "sam_available": sam_ok,
            "sam_detail": None if sam_ok else sam.load_error,
            "local_available": True,
            "manual_available": True,
        },
        "ocr": {
            "active": ocr.name if ocr_ok else "none",
            "available": ocr_ok,
            "detail": None if ocr_ok else ocr.error,
            "lang": settings.ocr_lang,
        },
        "inpainting": {
            "active": getattr(active_inpainting, "name", "opencv"),
            "magnific_available": magnific.available(),
            "magnific_model": settings.magnific_model,
            "magnific_models": magnific_catalog(),
            "openai_available": openai.available(),
            "openai_model": settings.openai_image_model,
            "flux_available": flux.available(),
            "adobe_available": adobe.available(),
            "opencv_available": True,
        },
    }
