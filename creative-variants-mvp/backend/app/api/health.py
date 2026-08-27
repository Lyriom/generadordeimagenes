"""Endpoints de estado y capacidades."""
from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..models import CapabilitiesResponse, HealthResponse
from ..models.schemas import INTENSITIES, SUPPORTED_FORMATS
from ..providers import provider_status
from ..services.layout_engine import layout_catalog
from ..services.psd_import import psd_available

router = APIRouter(tags=["sistema"])


@router.get("/health", response_model=HealthResponse, summary="Estado del servicio")
def health() -> HealthResponse:
    providers = provider_status()
    psd_ok, psd_detail = psd_available()
    providers["psd"] = {"available": psd_ok, "detail": psd_detail}
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        providers=providers,
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    summary="Proveedores disponibles, formatos y layouts",
)
def capabilities() -> CapabilitiesResponse:
    status_info = provider_status()
    psd_ok, psd_detail = psd_available()
    status_info["segmentation"]["psd_import"] = psd_ok
    status_info["segmentation"]["psd_detail"] = psd_detail
    return CapabilitiesResponse(
        segmentation=status_info["segmentation"],
        ocr=status_info["ocr"],
        inpainting=status_info["inpainting"],
        formats={key: list(value) for key, value in SUPPORTED_FORMATS.items()},
        layouts=layout_catalog(),
        intensities=list(INTENSITIES),
    )
