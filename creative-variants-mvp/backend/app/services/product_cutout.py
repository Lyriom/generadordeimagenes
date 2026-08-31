"""Rescate del producto cuando el PSD no lo trae como capa.

Muchos KV llegan con el producto **aplanado dentro de una fotografía**: la sala
completa, el mueble en ambiente, el zapato sobre una mesa. El importador ve una
capa que cubre todo el lienzo y es opaca, así que la toma como fondo — que es lo
correcto para un fondo, pero deja la pieza sin nada que reemplazar.

Aquí se recupera: se recorta el sujeto de esa foto, se convierte en una capa
Producto reemplazable y se rellena el hueco que deja para que el fondo quede
limpio. A partir de ahí la pieza funciona como cualquier otra plantilla.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image

from ..models import (
    BackgroundInfo,
    CATEGORY_LABELS_ES,
    Layer,
    LayerCategory,
    LayerType,
    Project,
    utcnow,
)
from ..providers import ProviderUnavailableError, get_inpainting_provider
from ..providers.magnific import MagnificCutoutProvider
from . import layer_extraction, storage
from .imaging import dilate_mask, mask_bbox, save_mask

logger = logging.getLogger(__name__)

#: Por debajo de esto el recorte no es un producto, es ruido.
MIN_AREA_RATIO = 0.015
#: Por encima de esto lo recortado ya no es un producto sino la escena entera.
#:
#: Medido con KV reales: una mesa de centro sobre fondo liso ocupa ~12 %; una foto
#: de ambiente devuelve ~75 %, porque el recortador aísla "todo lo que no es pared"
#: y entrega la sala completa. Aceptarla no solo da una capa inútil: obliga después
#: a rellenar tres cuartos del lienzo, y ahí cualquier modelo alucina en vez de
#: reconstruir.
MAX_AREA_RATIO = 0.55
#: A partir de aquí el relleno deja de ser fiable y conviene avisarlo.
RISKY_FILL_RATIO = 0.35

class NoProductFoundError(RuntimeError):
    """El recorte no encontró un sujeto utilizable en la imagen."""


def has_product(project: Project) -> bool:
    return any(
        layer.category == LayerCategory.PRODUCT and layer.visible
        for layer in project.layers
    )


BACKGROUND_REL = "backgrounds/background.png"


def _plate_path(project: Project) -> Path:
    """La foto de donde sale el producto.

    Se prefiere la plancha de fondo del PSD: ya está limpia de logo, precio y
    legales, así que el recortador no se distrae con ellos. Si no hay plancha se
    lee el arte original, que **nunca** se modifica.
    """
    if project.background.path:
        plate = storage.abs_path(project.project_id, project.background.path)
        if plate.exists():
            return plate
    source = storage.abs_path(project.project_id, project.source.path)
    if not source.exists():
        raise FileNotFoundError("No se encuentra la imagen del proyecto.")
    return source


def detect_product(
    project: Project,
    *,
    inpaint_provider: str | None = None,
    inpaint_model: str | None = None,
    prompt: str | None = None,
    dilate: int = 4,
) -> tuple[Layer, list[str]]:
    """Recorta el producto de la foto y limpia el fondo. Devuelve (capa, avisos).

    Lanza `NoProductFoundError` si el recorte no separa nada aprovechable, para
    no ensuciar el proyecto con una capa inútil.
    """
    warnings: list[str] = []
    cutter = MagnificCutoutProvider()
    if not cutter.available():
        raise ProviderUnavailableError(
            "MAGNIFIC_API_KEY no está configurada: no se puede detectar el producto."
        )

    plate = _plate_path(project)
    canvas_h, canvas_w = layer_extraction.canvas_shape(project)

    workdir = storage.abs_path(project.project_id, "tmp")
    workdir.mkdir(parents=True, exist_ok=True)
    cut_path = workdir / "cutout_raw.png"
    cutter.cutout(str(plate), output_path=str(cut_path))

    try:
        with Image.open(cut_path) as opened:
            cut = opened.convert("RGBA")
            if cut.size != (canvas_w, canvas_h):
                cut = cut.resize((canvas_w, canvas_h), Image.Resampling.LANCZOS)
            alpha = np.asarray(cut.getchannel("A"), dtype=np.uint8)
            rgba = cut.copy()
    finally:
        cut_path.unlink(missing_ok=True)

    coverage = float((alpha > 24).mean())
    if coverage < MIN_AREA_RATIO:
        raise NoProductFoundError(
            "El recorte no encontró un producto en la imagen "
            f"(solo {coverage:.1%} del arte). Márquelo a mano en Ajustes finos."
        )
    if coverage > MAX_AREA_RATIO:
        raise NoProductFoundError(
            f"Lo recortado ocupa el {coverage:.0%} del arte: es la escena completa, "
            "no un producto. Suele pasar en las portadas y fotos de ambiente, donde "
            "el KV no tiene un producto que se pueda reemplazar. Si de verdad hay "
            "uno, márquelo a mano en Ajustes finos."
        )

    box = mask_bbox(alpha, threshold=24)
    if box is None:
        raise NoProductFoundError("El recorte quedó vacío.")
    x, y, width, height = box

    layer = Layer(
        name=CATEGORY_LABELS_ES[LayerCategory.PRODUCT],
        type=LayerType.IMAGE,
        category=LayerCategory.PRODUCT,
        x=x,
        y=y,
        width=width,
        height=height,
        z_index=max((item.z_index for item in project.layers), default=0) + 1,
        locked=True,
        replaceable=True,
        preserve_aspect_ratio=True,
        confidence=0.7,
        source="auto",
        extracted=True,
    )
    layer.meta.update(
        {
            "detected_by": cutter.name,
            "coverage": round(coverage, 4),
            "from_background_plate": project.background.path or None,
        }
    )
    layer.warnings.append(
        "Producto recortado automáticamente de la fotografía. Revise el borde en "
        "Ajustes finos antes de producir."
    )

    # PNG de la capa: el recorte real, sin reescalar.
    layer.src = layer_extraction.layer_rel_path(layer)
    crop = rgba.crop((x, y, x + width, y + height))
    target = storage.abs_path(project.project_id, layer.src)
    target.parent.mkdir(parents=True, exist_ok=True)
    crop.save(target, format="PNG", optimize=True)
    crop.close()
    rgba.close()

    layer.mask = layer_extraction.mask_rel_path(layer)
    save_mask(storage.abs_path(project.project_id, layer.mask), alpha)

    warnings.extend(
        _clean_plate(
            project, plate, alpha, dilate, inpaint_provider, inpaint_model, prompt
        )
    )

    project.layers.append(layer)
    logger.info(
        "producto detectado en %s: %sx%s (%.1f%% del arte)",
        project.project_id, width, height, coverage * 100,
    )
    return layer, warnings


def _clean_plate(
    project: Project,
    plate: Path,
    alpha: np.ndarray,
    dilate: int,
    provider_name: str | None,
    model: str | None,
    prompt: str | None,
) -> list[str]:
    """Rellena el hueco que deja el producto y lo deja como plancha de fondo.

    El resultado siempre va a `backgrounds/background.png`, nunca sobre el arte
    original: el archivo que subió el usuario no se toca. Si el proyecto aún no
    tenía plancha (un JPG plano, sin PSD), esta pasa a serlo.
    """
    warnings: list[str] = []
    coverage = float((alpha > 24).mean())
    if coverage > RISKY_FILL_RATIO:
        warnings.append(
            f"El producto ocupa el {coverage:.0%} del arte: reconstruir un hueco tan "
            "grande da resultados poco fiables. Revise el fondo antes de producir."
        )
    mask = dilate_mask((alpha > 24).astype(np.uint8) * 255, dilate)
    mask_path = storage.abs_path(project.project_id, "backgrounds/erase_producto.png")
    save_mask(mask_path, mask)

    target = storage.abs_path(project.project_id, BACKGROUND_REL)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Se rellena en un archivo aparte y solo se mueve si sale bien: si el
    # proveedor falla a medias, la plancha anterior sigue intacta.
    staged = target.with_name("plate_sin_producto.png")
    provider = get_inpainting_provider(provider_name, model)
    used = getattr(provider, "name", "opencv")
    try:
        provider.fill(str(plate), str(mask_path), prompt=prompt, output_path=str(staged))
    except Exception as exc:  # noqa: BLE001 - el fondo no debe bloquear el recorte
        staged.unlink(missing_ok=True)
        warnings.append(
            f"El producto se recortó, pero el fondo no se pudo limpiar con {used} "
            f"({exc}). Queda la foto original detrás."
        )
        return warnings

    staged.replace(target)
    project.background = BackgroundInfo(
        path=BACKGROUND_REL,
        provider=f"{project.background.provider}+{used}" if project.background.path else used,
        generated_at=utcnow(),
        warnings=[f"Se borró de la foto el producto recortado, con {used}."],
    )
    warnings.append(f"Fondo limpiado con {used} tras recortar el producto.")
    return warnings
