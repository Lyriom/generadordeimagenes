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
#: Por debajo de esta confianza el OCR ya no esta leyendo texto: esta leyendo
#: una textura. Borrar lo que cree ver ahi estropea el fondo.
OCR_MIN_CONFIDENCE = .45
#: Una caja de texto minuscula suele ser ruido de compresion; una que ocupa
#: media pieza no es un titular, es que el OCR se comio el arte entero.
TEXT_BOX_MIN_RATIO = .00012
TEXT_BOX_MAX_RATIO = .30


def _blancos(mask: Image.Image) -> int:
    """Píxeles marcados. Con el histograma, no recorriendo la imagen en Python.

    Medido sobre una placa de 1600x2000: 37 ms por recorrido contra 4,7 ms, y
    se llama hasta cuatro veces por placa.
    """

    return sum(mask.histogram()[128:])


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
    blanco = _blancos(mask)
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
    cubierto = _blancos(mask)
    if not area_total * PRODUCT_MIN_RATIO <= cubierto <= area_total * PRODUCT_MAX_RATIO:
        return None, "la silueta no cuadra con un producto; se deja la escena intacta."
    return mask.filter(ImageFilter.MaxFilter(5)), ""


def _normalizar(texto: str) -> str:
    import unicodedata

    plano = unicodedata.normalize("NFKD", texto or "")
    return "".join(
        char for char in plano.casefold()
        if char.isalnum()
    )


def _es_marca(texto: str, marca: str) -> bool:
    """¿Esta lectura del OCR es el wordmark de la marca?

    Un logotipo escrito con letras lo lee el OCR como cualquier otro texto, y
    borrarlo deja la plantilla sin la firma del cliente —justo lo contrario de
    lo que se busca—. Se compara sin tildes ni mayúsculas, y solo con marcas de
    tres letras o más: con "AJ" cualquier palabra daría positivo.
    """

    objetivo = _normalizar(marca)
    if len(objetivo) < 3:
        return False
    leido = _normalizar(texto)
    if not leido:
        return False
    return objetivo in leido or leido in objetivo


def text_boxes(
    artwork: Image.Image, provider=None, *, brand_name: str = ""
) -> tuple[list[tuple[int, int, int, int]], list[dict[str, object]], str]:
    """Cajas del texto impreso en el arte, leídas con OCR.

    Un KV que llega como JPG, PDF o AI no trae capas: la única forma de saber
    qué es contenido variable y qué es identidad de marca es leer dónde hay
    texto. El wordmark de la marca se reconoce y **no** entra en las cajas a
    borrar: es identidad, no contenido de la pieza.

    Devuelve ``(cajas, lecturas, motivo)``; con ``motivo`` lleno no se llegó a
    leer nada y quien llame debe abstenerse, no adivinar.
    """

    import tempfile

    if provider is None:
        from ..providers import get_ocr_provider

        provider = get_ocr_provider()
    if not provider.available():
        return [], [], (
            "el OCR no está disponible: sin saber dónde está el texto, borrarlo "
            "sería adivinar."
        )
    ancho, alto = artwork.size
    area_total = max(1, ancho * alto)
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "arte.png"
        artwork.convert("RGB").save(ruta)
        try:
            lectura = provider.read(str(ruta))
        except Exception as exc:  # noqa: BLE001 - el arte sigue sirviendo sin placa
            return [], [], f"el OCR falló ({type(exc).__name__})."
    cajas: list[tuple[int, int, int, int]] = []
    leidas: list[dict[str, object]] = []
    for region in lectura.regions:
        if float(getattr(region, "confidence", 0.0)) < OCR_MIN_CONFIDENCE:
            continue
        izq, arriba = max(0, int(region.x)), max(0, int(region.y))
        der = min(ancho, izq + max(0, int(region.width)))
        abajo = min(alto, arriba + max(0, int(region.height)))
        if der <= izq or abajo <= arriba:
            continue
        proporcion = ((der - izq) * (abajo - arriba)) / area_total
        if not TEXT_BOX_MIN_RATIO <= proporcion <= TEXT_BOX_MAX_RATIO:
            continue
        texto = str(getattr(region, "text", ""))[:200]
        marca = _es_marca(texto, brand_name)
        if not marca:
            cajas.append((izq, arriba, der, abajo))
        leidas.append({
            "text": texto,
            "bbox": [izq, arriba, der, abajo],
            "confidence": round(float(getattr(region, "confidence", 0.0)), 3),
            "role": "brand" if marca else "content",
            # El color con el que estaba escrito. Sin esto el renderer pinta
            # todo en blanco y un precio sobre una pastilla blanca del arte
            # desaparece: se paga la pieza y no se ve el precio.
            "color": str(getattr(region, "color", "") or "")[:9],
        })
    if not cajas:
        return [], [], ""
    return cajas, leidas, ""


def build_plate_from_artwork(
    artwork: Image.Image,
    target: Path,
    *,
    preferred_provider: str | None = None,
    ocr_provider=None,
    segmentation_provider=None,
    brand_name: str = "",
) -> tuple[Path, str, list[str], list[dict[str, object]]] | None:
    """Placa a partir de un arte plano: JPG, página de PDF o AI aplanado.

    Es el mismo resultado que ``build_plate`` da con un PSD por capas, pero sin
    capas: el texto lo encuentra el OCR y el producto la segmentación. Importa
    porque casi ningún KV llega en PSD, y sin esto la plantilla se quedaba con
    una copia desenfocada del anuncio anterior de fondo —que es exactamente lo
    que no comunica la marca.

    Devuelve también las lecturas del OCR: saber dónde puso el diseñador el
    titular y el precio vale tanto como el fondo limpio.
    """

    warnings: list[str] = []
    cajas, leidas, motivo = text_boxes(artwork, ocr_provider, brand_name=brand_name)
    if motivo:
        # Sin OCR no hay placa. Es deliberado: el mal resultado conocido es
        # hornear el precio del anuncio viejo en todas las piezas nuevas.
        logger.info("Sin placa desde arte plano: %s", motivo)
        return None

    canvas = artwork.size
    mask = _mask(canvas, cajas) if cajas else None
    if mask is None and cajas:
        # El texto ocupa mas de lo que se puede reconstruir: no es un KV con
        # zonas variables, es un cartel de puro texto.
        return None
    if mask is None:
        mask = Image.new("L", canvas, 0)
        warnings.append(
            "El OCR no encontró texto impreso en este arte: la placa conserva "
            "la composición tal cual."
        )

    silueta, motivo_producto = _producto_de_la_escena(
        artwork, segmentation_provider
    )
    if silueta is not None:
        combinada = Image.new("L", canvas, 0)
        combinada.paste(mask, (0, 0))
        combinada.paste(silueta, (0, 0), silueta)
        cubierto = _blancos(combinada)
        if cubierto <= canvas[0] * canvas[1] * MAX_ERASE_RATIO:
            mask.close()
            mask = combinada
        else:
            combinada.close()
            motivo_producto = (
                "quitar el producto obligaría a reconstruir medio arte; se deja la escena."
            )
            silueta = None
        silueta and silueta.close()
    if silueta is None and motivo_producto:
        warnings.append(
            "La plantilla conserva el producto de la fotografía original porque "
            + motivo_producto
        )

    hecho = _compose(artwork, mask, target, warnings, preferred_provider)
    if hecho is None:
        return None
    ruta, motor, avisos = hecho
    return ruta, motor, avisos, leidas


def _producto_de_la_escena(artwork: Image.Image, provider=None):
    """``_product_mask`` con el proveedor resuelto y sin dejar escapar fallos."""

    try:
        if provider is None:
            from ..providers import get_segmentation_provider

            provider = get_segmentation_provider()
        return _product_mask(artwork, provider)
    except Exception as exc:  # noqa: BLE001 - la placa vale igual sin recorte
        return None, f"la segmentación falló ({type(exc).__name__})."


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
        cubierto = _blancos(combinada)
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

    return _compose(artwork, mask, target, warnings, preferred_provider)


def _compose(
    artwork: Image.Image,
    mask: Image.Image,
    target: Path,
    warnings: list[str],
    preferred_provider: str | None,
) -> tuple[Path, str, list[str]] | None:
    """Borra la máscara del arte y reconstruye lo que había debajo."""

    canvas = artwork.size
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


__all__ = ["build_plate", "build_plate_from_artwork", "text_boxes"]
