"""Placa de plantilla: el arte real del PSD con su contenido variable borrado.

La plantilla se dibujaba sobre un degradado y una copia desenfocada del arte
fuente. El desenfoque estaba puesto a propósito —para no reciclar el precio ni
el producto de una pieza anterior— pero el resultado no comunicaba la marca:
tres tarjetas grises con "TITULAR DE CAMPAÑA" encima.

Aquí se hace lo contrario y con el material de verdad: se compone el artboard
tal cual lo dibujó el diseñador, se borran las cajas de lo que la matriz debe
poder cambiar (textos, precios, sellos de promoción, legales) y se reconstruye
el fondo debajo con el motor de inpainting que ya usa el flujo de KV. Lo que
queda es la identidad: fondo, color, marcos, logo y ritmo compositivo.

Lo que NO hace, y conviene saberlo: si el producto viene fundido dentro de una
fotografía a sangre —una mesa fotografiada en su escena, por ejemplo—, no se
puede separar por cajas de capa y se queda en la placa. Recortarlo exige
segmentación sobre píxeles, que es el siguiente paso.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from ..providers import ProviderUnavailableError, get_inpainting_provider

logger = logging.getLogger(__name__)

#: Una capa que cubre casi todo el artboard es la escena, no un elemento
#: suelto. Borrarla dejaría la placa en blanco, así que nunca entra en la
#: máscara aunque su nombre suene a contenido variable.
FULL_BLEED_RATIO = .85
#: Por encima de esto ya no queda plantilla que reconstruir: si hay que borrar
#: más de la mitad del arte, el inpainting inventaría el diseño entero.
MAX_ERASE_RATIO = .45
#: La caja de una capa viene pegada a su contenido; sin margen quedan bordes
#: fantasma del texto viejo asomando bajo el nuevo.
DILATE_PX = 6


def _boxes_to_erase(
    manifest: list[dict],
    frozen_names: set[str],
    canvas: tuple[int, int],
) -> list[tuple[int, int, int, int]]:
    """Cajas del contenido que cada arte debe poder cambiar.

    Se borra lo que la ingesta NO congeló —es decir, lo que no es fondo, logo
    ni decoración de marca— siempre que sea un elemento acotado. El criterio ya
    está decidido aguas arriba por ``_psd_fixed_asset_role``; aquí solo se
    traduce a geometría.
    """

    width, height = canvas
    if width <= 0 or height <= 0:
        return []
    area_total = width * height
    boxes: list[tuple[int, int, int, int]] = []
    for item in manifest:
        if not isinstance(item, dict) or not item.get("visible", True):
            continue
        if str(item.get("name", "")) in frozen_names:
            continue
        raw = item.get("bbox")
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            continue
        try:
            left, top, right, bottom = (int(value) for value in raw)
        except (TypeError, ValueError, OverflowError):
            continue
        left, right = max(0, min(width, left)), max(0, min(width, right))
        top, bottom = max(0, min(height, top)), max(0, min(height, bottom))
        if right <= left or bottom <= top:
            continue
        if (right - left) / width >= FULL_BLEED_RATIO and (bottom - top) / height >= FULL_BLEED_RATIO:
            # La escena de fondo: borrarla no deja plantilla, deja un hueco.
            continue
        if (right - left) * (bottom - top) > area_total * MAX_ERASE_RATIO:
            continue
        boxes.append((left, top, right, bottom))
    return boxes


def _mask(canvas: tuple[int, int], boxes: list[tuple[int, int, int, int]]) -> Image.Image | None:
    if not boxes:
        return None
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for left, top, right, bottom in boxes:
        draw.rectangle(
            (
                max(0, left - DILATE_PX), max(0, top - DILATE_PX),
                min(canvas[0], right + DILATE_PX), min(canvas[1], bottom + DILATE_PX),
            ),
            fill=255,
        )
    blanco = sum(1 for value in mask.getdata() if value > 127)
    if blanco > canvas[0] * canvas[1] * MAX_ERASE_RATIO:
        # Con medio arte marcado, reconstruirlo sería inventarlo.
        mask.close()
        return None
    return mask


def build_plate(
    artwork: Image.Image,
    manifest: list[dict],
    frozen_names: set[str],
    target: Path,
    *,
    preferred_provider: str | None = None,
) -> tuple[Path, str, list[str]] | None:
    """Escribe la placa y devuelve (ruta, motor usado, avisos).

    Devuelve ``None`` cuando no hay nada que borrar o cuando borrar sería
    rehacer el arte: en ese caso la plantilla conserva su fondo determinista y
    nadie se queda con una reconstrucción inventada creyendo que es la marca.
    """

    warnings: list[str] = []
    canvas = artwork.size
    boxes = _boxes_to_erase(manifest, frozen_names, canvas)
    mask = _mask(canvas, boxes)
    if mask is None:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    base_path = target.with_name(target.stem + "-base.png")
    mask_path = target.with_name(target.stem + "-mask.png")
    base = artwork.convert("RGB")
    base.save(base_path, format="PNG")
    mask.save(mask_path, format="PNG")

    motor = "none"
    try:
        provider = get_inpainting_provider(preferred_provider)
        provider.fill(
            str(base_path), str(mask_path),
            prompt=(
                "Extiende el fondo y las texturas de marca existentes sobre la zona "
                "marcada. No añadas texto, precios, logotipos ni productos nuevos."
            ),
            output_path=str(target),
        )
        motor = getattr(provider, "name", "inpainting")
    except (ProviderUnavailableError, Exception) as exc:  # noqa: BLE001
        # Sin motor de relleno la placa sigue sirviendo: se difumina la zona
        # borrada en vez de dejar un rectángulo negro, y se avisa de que la
        # reconstrucción no es real.
        logger.info("Placa de plantilla sin inpainting (%s)", type(exc).__name__)
        suavizado = base.filter(ImageFilter.GaussianBlur(max(6, min(canvas) // 40)))
        compuesta = Image.composite(suavizado, base, mask)
        compuesta.save(target, format="PNG")
        compuesta.close()
        suavizado.close()
        motor = "local"
        warnings.append(
            "La zona variable del arte se cubrió con un difuminado local: no hay "
            "motor de reconstrucción configurado, así que el fondo bajo el texto "
            "borrado es aproximado."
        )
    finally:
        base.close()
        mask.close()
        base_path.unlink(missing_ok=True)
        mask_path.unlink(missing_ok=True)

    if not target.exists():
        return None
    return target, motor, warnings


__all__ = ["build_plate"]
