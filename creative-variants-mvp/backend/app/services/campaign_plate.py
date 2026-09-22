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

Cuando el producto viene fundido dentro de una fotografía a sangre —una mesa
fotografiada en su escena— no hay caja de capa que lo separe, y ahí entra la
segmentación por píxeles. Solo se intenta con un modelo de verdad (SAM): el
proveedor local de respaldo propone la imagen casi entera como "sujeto" —medido:
el 83,9 % del arte— y borrar eso no deja plantilla, deja un borrón.
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
#: Un producto ocupa una parte franca del arte, pero no el arte entero. Por
#: debajo del mínimo es un adorno; por encima del máximo, lo que se ha
#: detectado es la escena, y quitarla no deja nada que reutilizar.
PRODUCT_MIN_RATIO = .04
PRODUCT_MAX_RATIO = .40


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


def _product_mask(
    artwork: Image.Image, provider
) -> tuple[Image.Image | None, str]:
    """Silueta del producto dentro de una escena fotográfica, si se distingue.

    Devuelve ``(None, motivo)`` siempre que no haya una certeza razonable. Es
    deliberado: dejar el producto visible es un defecto conocido y acotado;
    borrar media escena y rellenarla a ojo estropea la plantilla entera.
    """

    import tempfile

    nombre = getattr(provider, "name", "")
    if "sam" not in nombre.casefold():
        return None, (
            f"segmentación '{nombre}': sin un modelo de verdad no se intenta "
            "separar el producto de la escena."
        )
    ancho, alto = artwork.size
    area_total = ancho * alto
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "plate.png"
        artwork.convert("RGB").save(ruta)
        try:
            detecciones = provider.detect(str(ruta))
        except Exception as exc:  # noqa: BLE001 - la placa sigue sin recorte
            return None, f"la segmentación fallo ({type(exc).__name__})."
        candidatas = [
            item for item in detecciones
            if area_total * PRODUCT_MIN_RATIO <= item.area <= area_total * PRODUCT_MAX_RATIO
        ]
        if not candidatas:
            return None, "no se distinguió un producto acotado dentro de la escena."
        elegida = max(candidatas, key=lambda item: item.area)
        try:
            bruta = provider.segment(str(ruta), box=elegida.box)
        except Exception as exc:  # noqa: BLE001
            return None, f"la segmentación fallo ({type(exc).__name__})."
    if bruta is None or getattr(bruta, "size", 0) == 0:
        return None, "la segmentación no devolvió silueta."
    mask = Image.fromarray(bruta).convert("L")
    if mask.size != artwork.size:
        mask = mask.resize(artwork.size, Image.Resampling.NEAREST)
    cubierto = sum(1 for value in mask.getdata() if value > 127)
    if not area_total * PRODUCT_MIN_RATIO <= cubierto <= area_total * PRODUCT_MAX_RATIO:
        return None, "la silueta no cuadra con un producto; se deja la escena intacta."
    return mask.filter(ImageFilter.MaxFilter(5)), ""


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

    # El producto fundido en la escena no tiene caja de capa que lo delate; si
    # hay un modelo capaz, se recorta su silueta y se suma a lo que se borra.
    from ..providers import get_segmentation_provider

    try:
        silueta, motivo = _product_mask(artwork, get_segmentation_provider())
    except Exception as exc:  # noqa: BLE001 - la placa vale igual sin recorte
        silueta, motivo = None, f"la segmentación fallo ({type(exc).__name__})."
    if silueta is not None:
        combinada = Image.new("L", canvas, 0)
        combinada.paste(mask, (0, 0))
        combinada.paste(silueta, (0, 0), silueta)
        cubierto = sum(1 for value in combinada.getdata() if value > 127)
        if cubierto <= canvas[0] * canvas[1] * MAX_ERASE_RATIO:
            mask.close()
            mask = combinada
        else:
            combinada.close()
            motivo = (
                "quitar el producto obligaría a reconstruir medio arte; se deja la escena."
            )
            silueta = None
        silueta and silueta.close()
    if silueta is None and motivo:
        # Que quede dicho: la plantilla lleva un producto de muestra dentro, y
        # eso se ve en cada pieza que se produzca con ella.
        warnings.append(
            "La plantilla conserva el producto de la fotografía original porque "
            + motivo
        )

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
