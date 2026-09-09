"""Fábricas de proveedores con degradación automática.

Orden de preferencia:
- Segmentación: SAM (si está habilitado y el checkpoint existe) → local OpenCV → manual.
- OCR: RapidOCR (si está instalado) → ninguno (advertencia + creación manual).
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
from .magnific import (
    MODELS as MAGNIFIC_MODELS,
    MagnificInpaintProvider,
    model_catalog as magnific_catalog,
)
from .manual_segmentation import ManualSegmentationProvider
from .opencv_inpaint import OpenCVInpaintProvider
from .openai_inpaint import (
    MODELS as OPENAI_IMAGE_MODELS,
    OpenAIInpaintProvider,
    model_catalog as openai_catalog,
)
from .openai_vision import OpenAIVisionProvider
from .rapid_ocr import RapidOcrProvider
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
def get_ocr_provider() -> RapidOcrProvider:
    return RapidOcrProvider()


#: Motores de pago. Elegir uno a mano es una decisión, no una preferencia: si no
#: se puede cumplir hay que decirlo, no entregar un fondo local con su nombre.
AI_INPAINTERS = ("magnific", "openai", "flux", "adobe")


def model_belongs_to(provider: str, model: str | None) -> bool:
    """¿Ese id de modelo es de ese motor? Cada uno tiene su catálogo."""
    if not model:
        return True
    key = model.strip().lower()
    if provider == "magnific":
        return key in MAGNIFIC_MODELS
    if provider == "openai":
        return key in OPENAI_IMAGE_MODELS
    return False


def _model_for(provider: str, model: str | None) -> str | None:
    """El modelo solo viaja al motor que lo conoce."""
    return model if model and model_belongs_to(provider, model) else None


def get_inpainting_provider(
    preferred: str | None = None, model: str | None = None
) -> Any:
    """Devuelve el proveedor pedido.

    `auto` va cascada abajo hasta OpenCV: eso es lo que significa. Pero un motor
    **pedido a mano** que no se puede usar ya no devuelve OpenCV con cara de
    haber funcionado: revienta con el motivo. Entregar un fondo local llamándolo
    Magnific era la forma más rápida de perder la confianza en el resultado.
    """
    choice = (preferred or settings.inpainting_provider or "auto").lower()
    if choice == "opencv":
        return OpenCVInpaintProvider()

    if choice in AI_INPAINTERS and not model_belongs_to(choice, model):
        raise ProviderUnavailableError(
            f"El modelo '{model}' no es del motor {choice}. Elija uno de su lista "
            "o cambie de motor."
        )

    if choice in {"magnific", "auto"}:
        magnific = MagnificInpaintProvider(model=_model_for("magnific", model))
        if magnific.available():
            return magnific
        if choice == "magnific":
            raise ProviderUnavailableError(
                "Magnific no está disponible: falta MAGNIFIC_API_KEY en el servidor. "
                "Elija otro motor o el local."
            )
    if choice in {"openai", "auto"}:
        openai = OpenAIInpaintProvider(model=_model_for("openai", model))
        if openai.available():
            return openai
        if choice == "openai":
            raise ProviderUnavailableError(
                "OpenAI no está disponible: falta OPENAI_API_KEY en el servidor. "
                "Elija otro motor o el local."
            )
    if choice in {"flux", "auto"}:
        flux = FluxInpaintProvider()
        if flux.available():
            return flux
        if choice == "flux":
            raise ProviderUnavailableError(
                "FLUX no está disponible: falta BFL_API_KEY en el servidor."
            )
    if choice in {"adobe", "auto"}:
        adobe = AdobeInpaintProvider()
        if adobe.available():
            return adobe
        if choice == "adobe":
            raise ProviderUnavailableError(
                "Adobe no está disponible: faltan sus credenciales en el servidor."
            )
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
    vision = OpenAIVisionProvider()
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
        # Reconocer el producto es lo que permite escalar bien un combo sin que el
        # usuario tenga que nombrar sus archivos. Se publica su estado porque una
        # capacidad apagada en silencio es una capacidad que no existe.
        "product_vision": {
            "active": vision.name if vision.available() else "none",
            "available": vision.available(),
            "enabled": settings.enable_product_vision,
            "model": settings.openai_vision_model,
        },
        "inpainting": {
            "active": getattr(active_inpainting, "name", "opencv"),
            "magnific_available": magnific.available(),
            "magnific_model": settings.magnific_model,
            "magnific_models": magnific_catalog(),
            "openai_available": openai.available(),
            "openai_model": openai.model_id,
            "openai_models": openai_catalog(),
            # Que haya clave no significa que el modelo exista. Un id que este
            # catálogo no reconoce se publica para que se vea, en vez de fallar
            # en la primera generación con un 400 de OpenAI.
            "openai_model_known": model_belongs_to("openai", openai.model_id),
            "flux_available": flux.available(),
            "adobe_available": adobe.available(),
            "opencv_available": True,
        },
    }
