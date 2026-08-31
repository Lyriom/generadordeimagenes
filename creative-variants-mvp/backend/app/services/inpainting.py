"""Reconstrucción aproximada del fondo detrás de los elementos extraídos."""
from __future__ import annotations

import logging

from ..models import BackgroundInfo, Project, utcnow
from ..providers import ProviderUnavailableError, get_inpainting_provider
from ..providers.opencv_inpaint import OpenCVInpaintProvider
from . import layer_extraction, storage
from .imaging import dilate_mask, save_mask

logger = logging.getLogger(__name__)

ERASE_MASK_REL = "backgrounds/erase_mask.png"
BACKGROUND_REL = "backgrounds/background.png"


def reconstruct_background(
    project: Project,
    layer_ids: list[str] | None = None,
    prompt: str | None = None,
    dilate: int = 6,
    preferred_provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str, list[str]]:
    """Rellena las zonas de las capas extraídas. Devuelve (ruta_rel, proveedor, avisos)."""
    warnings: list[str] = []
    original = storage.abs_path(project.project_id, project.source.path)
    if not original.exists():
        raise FileNotFoundError("No se encuentra el arte original.")

    mask = layer_extraction.union_mask(project, layer_ids)
    if int((mask > 127).sum()) == 0:
        warnings.append(
            "No hay máscaras que borrar: el fondo reconstruido es una copia del original."
        )
    mask = dilate_mask(mask, dilate)
    mask_path = storage.abs_path(project.project_id, ERASE_MASK_REL)
    save_mask(mask_path, mask)

    target = storage.abs_path(project.project_id, BACKGROUND_REL)
    provider = get_inpainting_provider(preferred_provider, model)
    provider_name = getattr(provider, "name", "opencv")
    provider_model = getattr(provider, "model_id", None)
    try:
        provider.fill(str(original), str(mask_path), prompt=prompt, output_path=str(target))
    except (ProviderUnavailableError, Exception) as exc:  # noqa: BLE001
        if provider_name != "opencv":
            warnings.append(
                f"El proveedor {provider_name} falló ({exc}); se usó OpenCV Inpaint local."
            )
            OpenCVInpaintProvider().fill(
                str(original), str(mask_path), prompt=prompt, output_path=str(target)
            )
            provider_name = "opencv"
        else:
            raise

    coverage = float((mask > 127).mean())
    if provider_name == "opencv" and coverage > 0.35:
        warnings.append(
            "Se reconstruyó más del 35% del fondo con OpenCV: el resultado puede verse "
            "borroso. Configure MAGNIFIC_API_KEY para usar Magnific y elegir un modelo."
        )

    if provider_model and provider_name != "opencv":
        provider_name = f"{provider_name}:{provider_model}"

    project.background = BackgroundInfo(
        path=BACKGROUND_REL,
        provider=provider_name,
        generated_at=utcnow(),
        warnings=warnings,
    )
    return BACKGROUND_REL, provider_name, warnings
