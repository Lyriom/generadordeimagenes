"""Separar un arte plano en capas, para poder recomponerlo en otra proporción.

Un KV que llega aplanado —un JPG, un PNG, un banner exportado— es una sola
imagen. Colocarla en un formato de proporción distinta solo puede salir de una
manera: la imagen encogida y el resto en color plano. Por eso un banner de
1920x325 en un 1080x1350 daba una pieza inservible.

La salida no es prohibir ese formato: es **separar el arte** y recomponerlo, que
es justo lo que el motor sabe hacer (OCR para el copy, SAM/OpenCV para los
objetos, reconstrucción del fondo). Eso ya existía, pero solo corría dentro del
modo automático; desde la pestaña de ajustes finos nunca llegaba a ejecutarse y
el usuario se quedaba con la única capa que hubiera.

Aquí está ese trabajo en un sitio del que puedan tirar los dos caminos.
"""
from __future__ import annotations

import logging

from ..models import LayerType, Project
from . import analysis, inpainting, layer_extraction

logger = logging.getLogger(__name__)

#: Con menos capas que esto no hay nada que recolocar: el motor solo puede
#: colocar la imagen entera. Es el mismo número que usa `layout_engine`.
MIN_LAYERS_TO_RECOMPOSE = 3


def usable_layers(project: Project) -> list:
    """Capas que el motor de composición puede usar tal como están."""
    from ..models import LayerCategory

    ready = []
    for layer in project.layers:
        if layer.category == LayerCategory.BACKGROUND or not layer.visible:
            continue
        if layer.type == LayerType.TEXT and (layer.content or "").strip():
            ready.append(layer)
        elif layer.type == LayerType.IMAGE and layer.src:
            ready.append(layer)
    return ready


def needs_separation(project: Project) -> bool:
    """¿Este arte tiene tan pocas piezas que no se puede recomponer?"""
    return len(usable_layers(project)) < MIN_LAYERS_TO_RECOMPOSE


def separate(project: Project, *, max_regions: int = 12) -> tuple[int, list[str]]:
    """Separa el arte en capas y deja el fondo listo. Devuelve (capas, avisos).

    Es el mismo trabajo que hace el modo automático antes de componer: leer los
    textos, recortar los objetos, y reconstruir el fondo que queda debajo. No
    lanza: si algo falla se dice y se sigue con lo que haya, porque quedarse sin
    generar nada es peor que generar con menos piezas.
    """
    warnings: list[str] = []

    try:
        layers, analyze_warnings, seg_provider, ocr_provider = analysis.analyze_project(
            project,
            run_segmentation=True,
            run_ocr=True,
            max_regions=max_regions,
            extract=True,
        )
        warnings.extend(analyze_warnings)
        textos = sum(1 for layer in layers if layer.type == LayerType.TEXT)
        logger.info(
            "separación de %s: %s elementos (%s de texto) con %s / %s",
            project.project_id,
            len(layers),
            textos,
            seg_provider,
            ocr_provider,
        )
        if not ocr_provider:
            warnings.append(
                "No hay lector de texto disponible: el copy del arte no se pudo separar."
            )
    except Exception as exc:  # noqa: BLE001 - separar es un intento, no un requisito
        logger.warning("separación fallida en %s: %s", project.project_id, exc)
        warnings.append(f"No se pudo separar el arte en capas: {exc}")
        return len(usable_layers(project)), warnings

    try:
        _, _, extract_warnings = layer_extraction.extract_layers(
            project, None, feather=2, force=False
        )
        warnings.extend(extract_warnings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("recorte fallido en %s: %s", project.project_id, exc)
        warnings.append(f"No se pudieron recortar todos los elementos: {exc}")

    # El fondo tiene que existir antes de recomponer: si no, las capas se dibujan
    # sobre el arte original y se ve el producto viejo debajo del nuevo.
    if not project.background.path:
        try:
            _, provider_name, background_warnings = inpainting.reconstruct_background(
                project, preferred_provider="opencv"
            )
            warnings.extend(background_warnings)
            logger.info("fondo reconstruido con %s en %s", provider_name, project.project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fondo no reconstruido en %s: %s", project.project_id, exc)
            warnings.append(f"No se pudo reconstruir el fondo: {exc}")

    return len(usable_layers(project)), warnings
