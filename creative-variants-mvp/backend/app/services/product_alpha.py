"""Recortar el producto cuando el archivo llega con su fondo.

Un catálogo no entrega recortes: entrega el producto sobre blanco. `A06656.png`,
`AWHAFT6001-01.webp`, `AWHMM20C01-01.webp` son exactamente eso. Al pegarlos sobre
un KV lo que se ve es un rectángulo blanco con el producto dentro, tapando el
arte alrededor.

Hasta ahora el motor lo **avisaba** y seguía: «la imagen no tiene transparencia,
use un PNG recortado». Pedirle al usuario que recorte a mano veinte archivos de
catálogo es devolverle el trabajo que venía a hacer aquí, y encima el aviso se
perdía entre los demás. El recortador ya estaba en la casa —se usaba para
rescatar un producto aplanado dentro del KV— pero no en el camino por el que
entran los productos.

Se intenta en dos pasos, del gratis al que cuesta:

1. **El fondo plano.** Si el borde de la imagen es de un color uniforme —el
   blanco del catálogo, un gris de estudio—, el recorte es geometría: todo lo que
   se aparta de ese color es el producto. Sin red, sin coste y sin esperar, que es
   lo que hace falta cuando se suben veinte de golpe.
2. **`remove-background` de Magnific**, para lo que el paso anterior no puede: el
   producto fotografiado en una cocina real, con sombra y encimera. Ahí no hay un
   color de fondo que quitar y hace falta un modelo.

Si ninguno de los dos da un recorte creíble se deja la imagen como vino y se
dice con claridad qué pasó. Inventar un recorte malo es peor que no recortar:
al menos el rectángulo blanco se ve y se corrige.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..providers.base import ProviderUnavailableError
from ..providers.magnific import MagnificCutoutProvider

logger = logging.getLogger(__name__)

#: Alpha por encima del cual un píxel cuenta como opaco.
OPAQUE = 8

#: Ancho del anillo de borde con el que se mide el fondo, en proporción al lado
#: menor. Suficiente para leer el color sin morder el producto.
BORDER_RING = 0.04

#: Cuánto del anillo tiene que ser del mismo color para llamarlo fondo plano.
FLAT_BORDER_SHARE = 0.88

#: Distancia de color mínima para separar el producto del fondo. Se sube si el
#: propio fondo tiene ruido: un blanco de catálogo comprimido en JPG no es un
#: blanco, es un blanco con granos.
MIN_TOLERANCE = 20.0

#: Y un techo, porque la tolerancia sale de la dispersión del propio borde: una
#: foto de ambiente con la pared arriba y la encimera abajo tiene un borde tan
#: variado que pedía una tolerancia enorme, y con ella todo entraba como fondo.
#: Un fondo de catálogo tiene ruido de compresión, no dos colores distintos.
MAX_TOLERANCE = 42.0

#: Un recorte fuera de esta franja no es un producto: o se quedó con nada, o se
#: trajo la imagen entera y no recortó nada.
MIN_SUBJECT = 0.02
MAX_SUBJECT = 0.90

#: Cuánto de la imagen tiene que ser de un mismo color para que no haya nada que
#: recortar. Una imagen de un solo color no tiene producto dentro, así que no se
#: gasta una consulta al recortador en preguntárselo.
UNIFORM_SHARE = 0.97


def has_alpha(image: Image.Image) -> bool:
    """¿La imagen trae transparencia de verdad, no un canal alfa lleno de 255?"""
    if image.mode != "RGBA":
        return False
    return bool(np.asarray(image.getchannel("A")).min() < 255)


def _subject_ratio(alpha: np.ndarray) -> float:
    return float((alpha > OPAQUE).mean())


def is_uniform(image: Image.Image) -> bool:
    """¿La imagen es de un solo color? Entonces no hay producto que recortar."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32).reshape(-1, 3)
    if rgb.size == 0:
        return True
    color = np.median(rgb, axis=0)
    cerca = np.linalg.norm(rgb - color, axis=1) <= MIN_TOLERANCE
    return float(cerca.mean()) >= UNIFORM_SHARE


def _edges(rgb: np.ndarray) -> list[np.ndarray]:
    """Los cuatro bordes por separado, no un anillo revuelto.

    Medirlos juntos daba falsos positivos: una foto de ambiente con la pared
    arriba y la encimera abajo tiene el anillo mezclado, pero la mediana y el
    porcentaje salían bien y se recortaba por color un producto que necesitaba
    el modelo. Un fondo de catálogo es del mismo color por los cuatro lados.
    """
    alto, ancho = rgb.shape[:2]
    grosor = max(2, int(round(min(alto, ancho) * BORDER_RING)))
    return [
        rgb[:grosor, :].reshape(-1, 3),
        rgb[-grosor:, :].reshape(-1, 3),
        rgb[:, :grosor].reshape(-1, 3),
        rgb[:, -grosor:].reshape(-1, 3),
    ]


def flat_backdrop_alpha(image: Image.Image) -> tuple[np.ndarray | None, str | None]:
    """Alpha deducido de un fondo de color uniforme. `None` si no lo hay.

    Devuelve también el motivo por el que no se pudo, para poder decirlo.
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    bordes = _edges(rgb)
    if any(borde.size == 0 for borde in bordes):
        return None, "la imagen es demasiado pequeña"

    muestras = np.concatenate(bordes)
    fondo = np.median(muestras, axis=0)
    distancias = np.linalg.norm(muestras - fondo, axis=1)
    # Se mide con la propia dispersión del borde: un fondo comprimido tiene
    # ruido y exigirle un color exacto lo descartaría siempre.
    tolerancia = max(MIN_TOLERANCE, float(np.percentile(distancias, 90)) * 1.6)
    if tolerancia > MAX_TOLERANCE:
        return None, "el fondo de la imagen no es de un color, son varios"
    for borde in bordes:
        cerca = np.linalg.norm(borde - fondo, axis=1) <= tolerancia
        if float(cerca.mean()) < FLAT_BORDER_SHARE:
            return None, "el borde de la imagen no es de un color uniforme"

    lejos = np.linalg.norm(rgb - fondo, axis=2) > tolerancia
    mascara = (lejos.astype(np.uint8)) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN, kernel, iterations=1)
    mascara = cv2.morphologyEx(mascara, cv2.MORPH_CLOSE, kernel, iterations=2)

    # Los huecos interiores se rellenan: la puerta blanca de un microondas es del
    # color del fondo, pero es producto. Se inunda el fondo desde el borde y lo
    # que no se moja por dentro se devuelve al producto.
    fuera = np.zeros((mascara.shape[0] + 2, mascara.shape[1] + 2), dtype=np.uint8)
    relleno = mascara.copy()
    cv2.floodFill(relleno, fuera, (0, 0), 255)
    mascara = cv2.bitwise_or(mascara, cv2.bitwise_not(relleno))

    # Solo el sujeto principal: un catálogo a veces trae una marca de agua o una
    # etiqueta en una esquina, y eso no es el producto.
    total, etiquetas, stats, _ = cv2.connectedComponentsWithStats(
        (mascara > 0).astype(np.uint8), connectivity=8
    )
    if total <= 1:
        return None, "no se encontró ningún sujeto sobre el fondo"
    mayor = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mascara = np.where(etiquetas == mayor, 255, 0).astype(np.uint8)

    alpha = cv2.GaussianBlur(mascara, (0, 0), 0.8)
    ratio = _subject_ratio(alpha)
    if ratio < MIN_SUBJECT:
        return None, f"el sujeto recortado ocupaba solo el {ratio * 100:.0f}% de la imagen"
    if ratio > MAX_SUBJECT:
        return None, "el fondo y el producto son del mismo color"
    return alpha, None


def _with_alpha(image: Image.Image, alpha: np.ndarray) -> Image.Image:
    salida = image.convert("RGBA")
    salida.putalpha(Image.fromarray(alpha, mode="L"))
    return salida


def cutout(
    image: Image.Image,
    source: Path,
    *,
    use_provider: bool = True,
) -> tuple[Image.Image, list[str], str]:
    """Devuelve el producto recortado, los avisos y por dónde se consiguió.

    Nunca lanza: que el recorte falle no puede tumbar la subida de un producto.
    El método es `ya-transparente`, `fondo-plano`, `magnific` o `sin-recorte`.
    """
    warnings: list[str] = []
    if has_alpha(image):
        return image, warnings, "ya-transparente"

    if is_uniform(image):
        warnings.append(
            "La imagen es de un solo color: no hay ningún producto que recortar, "
            "así que sobre el KV se verá como un rectángulo."
        )
        return image, warnings, "sin-recorte"

    alpha, motivo = flat_backdrop_alpha(image)
    if alpha is not None:
        return _with_alpha(image, alpha), warnings, "fondo-plano"

    provider = MagnificCutoutProvider()
    if not use_provider or not provider.available():
        warnings.append(
            "La imagen llegó con fondo y no se pudo recortar aquí "
            f"({motivo}): se verá su fondo rectangular sobre el KV. "
            "Suba el PNG recortado o active el recortador en el servidor."
        )
        return image, warnings, "sin-recorte"

    salida = source.with_name(f"{source.stem}_cutout.png")
    try:
        provider.cutout(str(source), output_path=str(salida))
        with Image.open(salida) as abierta:
            recortada = abierta.convert("RGBA")
        ratio = _subject_ratio(np.asarray(recortada.getchannel("A")))
        if not MIN_SUBJECT <= ratio <= MAX_SUBJECT:
            raise ValueError(
                f"el recorte devuelto ocupaba el {ratio * 100:.0f}% de la imagen"
            )
        return recortada, warnings, "magnific"
    except ProviderUnavailableError as exc:
        logger.info("Recortador no disponible: %s", exc)
        warnings.append(
            "La imagen llegó con fondo y el recortador no está disponible "
            f"({exc}): se verá su fondo rectangular sobre el KV."
        )
    except Exception as exc:  # noqa: BLE001 - recortar es un intento, no un requisito
        logger.warning("Recorte fallido para %s: %s", source.name, exc)
        warnings.append(
            f"La imagen llegó con fondo y no se pudo recortar ({exc}): "
            "se verá su fondo rectangular sobre el KV."
        )
    finally:
        salida.unlink(missing_ok=True)
    return image, warnings, "sin-recorte"
