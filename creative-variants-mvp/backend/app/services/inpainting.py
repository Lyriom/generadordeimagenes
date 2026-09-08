"""Reconstrucción aproximada del fondo detrás de los elementos extraídos."""
from __future__ import annotations

import logging

import numpy as np

from ..models import BackgroundInfo, LayerType, Project, utcnow
from ..providers import ProviderUnavailableError, get_inpainting_provider
from ..providers.opencv_inpaint import OpenCVInpaintProvider
from . import layer_extraction, storage
from .imaging import dilate_mask, fill_looks_invented, load_flat_rgb, save_mask

logger = logging.getLogger(__name__)

ERASE_MASK_REL = "backgrounds/erase_mask.png"
BACKGROUND_REL = "backgrounds/background.png"

#: Cuánto se agranda la máscara de un texto por encima de la de un objeto. La
#: caja del OCR viene pegada a las letras, y con el margen normal quedaban
#: fantasmas: el copy medio borrado asomando debajo del copy nuevo. Un objeto
#: recortado no necesita esto, su máscara sigue su silueta.
TEXT_DILATE_FACTOR = 2.5


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
    # El texto se borra con más margen que un objeto: su caja viene pegada a las
    # letras y con el margen normal quedaban fantasmas.
    textos = [
        layer
        for layer in project.layers
        if layer.type == LayerType.TEXT and (layer_ids is None or layer.id in set(layer_ids))
    ]
    if textos:
        solo_texto = layer_extraction.union_mask(project, [layer.id for layer in textos])
        mask = np.maximum(
            dilate_mask(mask, dilate),
            dilate_mask(solo_texto, int(round(dilate * TEXT_DILATE_FACTOR))),
        )
    else:
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

    # Y se comprueba lo que devolvió. Un modelo de imagen al que se le pide
    # borrar un producto y dejar fondo liso a veces escribe letras en el hueco
    # —«IND MERR» donde estaba el producto— o deja los restos del texto que
    # tenía que quitar. Las dos cosas se delatan por los bordes, y las dos
    # salían en la plancha sin que nadie se enterara.
    if provider_name != "opencv":
        motivo = fill_looks_invented(load_flat_rgb(target), load_flat_rgb(original), mask)
        if motivo is not None:
            logger.info("plancha descartada en %s: %s", project.project_id, motivo)
            warnings.append(
                f"El fondo que devolvió {provider_name} se descartó porque {motivo}: "
                "se reconstruyó con OpenCV Inpaint local."
            )
            OpenCVInpaintProvider().fill(
                str(original), str(mask_path), prompt=prompt, output_path=str(target)
            )
            provider_name = "opencv"
            provider_model = None

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
