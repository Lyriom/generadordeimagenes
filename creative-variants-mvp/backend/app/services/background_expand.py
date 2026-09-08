"""Extender el fondo del arte hasta el lienzo que pide el formato.

Cuando una pieza se recompone a una proporción que el arte no cubre, el fondo se
resolvía estirando la plancha y difuminándola. Es honesto —difuminada se lee
como textura y no como un banner roto— pero sigue siendo el mismo banner
ampliado cuatro veces, y en un 1080x1350 salido de un 1920x325 eso es casi toda
la pieza.

Los modelos de imagen que ya están en la cuenta saben continuar un fondo hacia
afuera: Gemini 2.5 Flash Image («Nano Banana») y los de la familia Nano Banana
Pro están en el catálogo, y el de máscara real —Ideogram— es el que se usa aquí
por una razón concreta: **con máscara los píxeles del arte no se tocan**. El
modelo solo pinta lo que quedaba fuera. Un KV aprobado sigue siendo el KV
aprobado; sin máscara el modelo regenera también el arte, y eso no se le puede
hacer a una marca.

Se guarda en disco por lienzo, así que una tanda de veinte productos sobre el
mismo KV paga una vez por formato y no una por pieza. Si el modelo no está o
falla, no pasa nada: se devuelve `None` y el renderer sigue con la plancha
difuminada de siempre.
"""
from __future__ import annotations

import logging

import numpy as np
from PIL import Image

from ..config import settings
from ..models import Project
from ..providers import ProviderUnavailableError, get_inpainting_provider
from ..providers.magnific import MODELS, MagnificSceneProvider
from . import storage
from .imaging import dilate_mask, fit_contain, hex_to_rgb, load_alpha, load_flat_rgb, save_mask, style_palette

logger = logging.getLogger(__name__)

#: Qué se le pide al modelo. Solo fondo: el arte final no se delega nunca.
EXPAND_INSTRUCTION = (
    "Continúa hacia afuera el fondo de esta pieza publicitaria en la zona "
    "marcada, siguiendo sus colores, su iluminación, sus degradados y sus "
    "texturas. No dibujes productos, personas, logotipos, letras, números ni "
    "precios. No añadas marcos, bordes ni viñetas. El resultado debe parecer "
    "la misma escena fotografiada con un encuadre más amplio."
)

#: Cuánto se puede ampliar la plancha antes de que se le vean los píxeles. Es
#: el mismo número con el que el renderer decide difuminarla, y a propósito: se
#: extiende exactamente donde antes había que difuminar. Por debajo no hace
#: falta —la plancha recortada llena el lienzo con píxeles de verdad— y pagar
#: por extenderla sería pagar por nada.
SHARP_UPSCALE = 1.6

#: Cuánto se mete la máscara dentro de la plancha para que la costura no se vea.
SEAM = 10


def relative_path(width: int, height: int) -> str:
    return f"backgrounds/expanded_{width}x{height}.png"


def _scene_provider(model: str | None, plancha_limpia: bool) -> MagnificSceneProvider | None:
    """El modelo sin máscara, solo si se pidió y solo si es seguro usarlo.

    Gemini 2.5 Flash Image y los Nano Banana Pro continúan una escena mejor que
    un modelo de máscara, pero **regeneran la imagen entera**. Sobre el arte
    original eso redibujaría el copy, los precios y los logos, así que se exige
    plancha limpia: el fondo ya reconstruido, sin nada que se pueda estropear.
    """
    elegido = (model or settings.magnific_expand_model or "").strip()
    if not elegido or elegido == settings.magnific_model:
        return None
    definicion = MODELS.get(elegido)
    if definicion is None or definicion.supports_mask or not plancha_limpia:
        return None
    escena = MagnificSceneProvider(model=elegido)
    return escena if escena.available() else None


def _plate(project: Project):
    """La plancha de la que se parte: el fondo reconstruido o el arte original."""
    if project.background.path:
        path = storage.abs_path(project.project_id, project.background.path)
        if path.exists():
            return path
    return storage.abs_path(project.project_id, project.source.path)


def cover_upscale(source: tuple[int, int], width: int, height: int) -> float:
    """Cuánto habría que ampliar la plancha para llenar el lienzo recortándola.

    Es la cuenta que decide si merece la pena extender: con 0.38 la plancha
    llena el lienzo de sobra y solo hay que reducirla; con 4.15 —un banner de
    325 px de alto en un lienzo de 1350— no hay píxeles y lo que sale es una
    mancha ampliada cuatro veces.
    """
    source_w, source_h = source
    if min(source_w, source_h, width, height) <= 0:
        return 0.0
    return max(width / source_w, height / source_h)


def cached(project: Project, width: int, height: int) -> str | None:
    """La ruta relativa del fondo ya extendido para este lienzo, si existe."""
    rel = relative_path(width, height)
    return rel if storage.abs_path(project.project_id, rel).exists() else None


def expand(
    project: Project,
    width: int,
    height: int,
    *,
    preferred_provider: str | None = None,
    model: str | None = None,
) -> tuple[str | None, list[str]]:
    """Extiende el fondo hasta (width, height). Devuelve (ruta_rel, avisos).

    Nunca lanza: que no se pueda extender el fondo no puede tumbar una tanda.
    Devuelve `None` cuando no hace falta, no se puede, o no salió.
    """
    warnings: list[str] = []
    ya = cached(project, width, height)
    if ya is not None:
        return ya, warnings

    plate = _plate(project)
    if not plate.exists():
        return None, warnings

    base = load_flat_rgb(plate)
    if cover_upscale(base.size, width, height) <= SHARP_UPSCALE:
        # La plancha llena el lienzo con píxeles de verdad: no hay nada que
        # inventar, y recortarla es mejor que reducir el arte para hacer sitio.
        return None, warnings

    limpia = bool(project.background.path) and plate != storage.abs_path(
        project.project_id, project.source.path
    )
    escena = _scene_provider(model, limpia)
    provider = None if escena is not None else get_inpainting_provider(preferred_provider, model)
    if escena is None and getattr(provider, "name", "opencv") == "opencv":
        # OpenCV rellena por difusión de los píxeles vecinos: en una franja tan
        # grande da una mancha. Para eso ya está la plancha difuminada.
        return None, warnings

    fitted_w, fitted_h = fit_contain(base.size[0], base.size[1], width, height)
    offset = ((width - fitted_w) // 2, (height - fitted_h) // 2)
    primary, _ = style_palette(base, alpha=load_alpha(plate))
    lienzo = Image.new("RGB", (width, height), hex_to_rgb(primary))
    lienzo.paste(base.resize((fitted_w, fitted_h), Image.Resampling.LANCZOS), offset)

    mask = np.full((height, width), 255, dtype=np.uint8)
    mask[offset[1] : offset[1] + fitted_h, offset[0] : offset[0] + fitted_w] = 0
    # La costura se repinta: si la máscara termina justo en el borde de la
    # plancha, se ve la línea donde acaba el arte y empieza lo generado.
    mask = dilate_mask(mask, SEAM)

    rel = relative_path(width, height)
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    base_rel = f"backgrounds/expand_base_{width}x{height}.png"
    base_path = storage.abs_path(project.project_id, base_rel)
    mask_path = storage.abs_path(project.project_id, f"backgrounds/expand_mask_{width}x{height}.png")
    lienzo.save(base_path, format="PNG")
    save_mask(mask_path, mask)

    try:
        if escena is not None:
            # Sin máscara: el modelo devuelve la escena entera continuada. Solo
            # se llega aquí con plancha limpia, donde no hay copy ni logos que
            # pueda redibujar.
            escena.empty(str(base_path), output_path=str(target), prompt=EXPAND_INSTRUCTION)
        else:
            assert provider is not None
            provider.fill(
                str(base_path), str(mask_path), prompt=EXPAND_INSTRUCTION, output_path=str(target)
            )
    except (ProviderUnavailableError, Exception) as exc:  # noqa: BLE001
        logger.warning("fondo no extendido a %sx%s en %s: %s", width, height, project.project_id, exc)
        warnings.append(
            f"No se pudo extender el fondo a {width}x{height} ({exc}): la pieza usa "
            "la plancha del arte difuminada."
        )
        target.unlink(missing_ok=True)
        return None, warnings
    finally:
        base_path.unlink(missing_ok=True)
        mask_path.unlink(missing_ok=True)

    # El modelo elige su propio tamaño: se devuelve al del lienzo para que el
    # renderer no tenga que adivinar nada.
    with Image.open(target) as salida:
        if salida.size != (width, height):
            salida.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).save(
                target, format="PNG"
            )
    usado = escena if escena is not None else provider
    logger.info(
        "fondo extendido a %sx%s en %s con %s",
        width, height, project.project_id,
        getattr(usado, "model_id", getattr(usado, "name", "?")),
    )
    return rel, warnings
