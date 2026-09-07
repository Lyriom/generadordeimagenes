"""Qué producto es cada recorte, sin pedírselo al usuario.

El tamaño real de un producto es lo que permite escalar bien un combo, y antes
salía únicamente del nombre del archivo. Eso convertía «nombra bien tus PNG» en
un requisito, que es trabajo del motor y no del que usa la herramienta: un
`producto1.png` dejaba la pieza con proporciones neutras y nadie se enteraba.

Aquí se identifica solo, en cascada y de lo barato a lo caro:

1. **El nombre** del archivo o de la capa. Gratis e inmediato; cuando el archivo
   ya viene del catálogo (`cocina-indurama-20p.png`) no hace falta nada más.
2. **La foto**, preguntándole a un modelo con visión por una familia de una lista
   cerrada. Es la vía que hace que el nombre deje de ser obligatorio.
3. **El arte**, cuando el KV nombra un solo producto y hay un solo producto que
   identificar. El titular «COCINA A GAS 4Q 20P CROMA» dice qué es y de paso
   cuántas pulgadas.

Lo que se averigua se guarda en la capa, así que se paga una vez por recorte y la
generación sigue siendo local y determinista. Si las tres vías fallan no se
inventa nada: el motor iguala los altos y lo dice en los avisos.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..models import LayerCategory, LayerType, Project
from ..providers.base import ProviderUnavailableError
from ..providers.openai_vision import OpenAIVisionProvider
from . import product_scale

logger = logging.getLogger(__name__)

#: De dónde salió la identificación. Se guarda para poder explicarlo en la
#: interfaz: no es lo mismo haberlo leído del nombre que haberlo mirado.
SOURCE_LABELS = {
    "nombre": "por el nombre del archivo",
    "imagen": "mirando la foto",
    "arte": "por el texto del KV",
}


#: Lo reconocido por recorte, con la huella del PNG como clave. Un lote pone el
#: mismo producto en todos los KV de la campaña: sin esto se preguntaba una vez
#: por cada KV, y con veinte productos y tres KV son sesenta consultas para
#: veinte imágenes distintas. Vive en memoria del proceso a propósito: es una
#: caché, no un dato, y perderla al reiniciar no rompe nada.
_CACHE: dict[str, dict] = {}
_CACHE_MAX = 512


def _provider() -> OpenAIVisionProvider:
    return OpenAIVisionProvider()


def _recordar(clave: str | None, record: dict) -> None:
    if not clave:
        return
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[clave] = record


def _anotar(diagnostics: list[str] | None, mensaje: str) -> None:
    if diagnostics is not None and mensaje not in diagnostics:
        diagnostics.append(mensaje[:300])


def artwork_text(project: Project) -> str:
    """Todo el copy del KV en una sola cadena.

    Sirve para dos cosas: reconocer el producto cuando el arte lo nombra, y
    sacar las pulgadas («20P», «55"») que el nombre del archivo suele no traer.
    """
    piezas: list[str] = []
    for layer in project.layers:
        if layer.type != LayerType.TEXT:
            continue
        texto = (layer.content or "").strip()
        if texto:
            piezas.append(texto)
    return " · ".join(piezas)


def _from_artwork(project: Project, hint: str) -> str | None:
    """Familia que nombra el arte, solo si nombra una y hay un producto que medir.

    Con dos productos en el KV y dos familias en el copy no hay forma de saber
    cuál es cuál, y repartirlas al azar sería peor que no saberlo.
    """
    productos = [
        layer
        for layer in project.layers
        if layer.category == LayerCategory.PRODUCT and layer.visible
    ]
    if len(productos) != 1:
        return None
    return product_scale.family_key(hint)


def identify(
    project: Project,
    layer,
    image_path: str | Path | None = None,
    *,
    use_vision: bool = True,
    diagnostics: list[str] | None = None,
    cache_key: str | None = None,
) -> dict | None:
    """Reconoce el producto de `layer` y deja el resultado en `layer.meta`.

    Devuelve el registro guardado, o `None` si ninguna vía lo reconoció. Nunca
    lanza: que el reconocimiento falle no puede tumbar la subida de un producto.

    En `diagnostics` deja **por qué** no lo reconoció. No es lo mismo «lo miré y
    no supe qué era» que «no pude preguntarle a nadie»: lo primero se arregla con
    una foto mejor y lo segundo con la configuración del servidor, y colapsar las
    dos en un solo aviso deja al usuario sin saber cuál de las dos le pasó.
    """
    meta = layer.meta
    clave = cache_key or meta.get("asset_fingerprint")
    guardado = _CACHE.get(clave) if clave else None
    if guardado is not None:
        meta["product_size"] = dict(guardado)
        return meta["product_size"]

    nombre_archivo = meta.get("replaced_from")
    texto_arte = artwork_text(project)
    hints = [nombre_archivo, getattr(layer, "name", None), meta.get("psd_name"), texto_arte]

    key = product_scale.family_key(nombre_archivo, getattr(layer, "name", None), meta.get("psd_name"))
    source = "nombre"

    if key is None and use_vision and image_path is not None:
        provider = _provider()
        if not provider.available():
            _anotar(diagnostics, "el reconocimiento por imagen está apagado o sin clave")
        else:
            try:
                key = provider.identify(image_path, list(product_scale.families()))
                source = "imagen"
                if key is None:
                    _anotar(diagnostics, "se miró la foto y no se reconoció el producto")
            except ProviderUnavailableError as exc:
                logger.info("Reconocimiento por imagen no disponible: %s", exc)
                _anotar(diagnostics, f"no se pudo consultar el reconocedor ({exc})")
            except Exception as exc:  # noqa: BLE001 - identificar es opcional
                logger.warning("Falló el reconocimiento por imagen: %s", exc)
                _anotar(diagnostics, f"falló el reconocedor ({type(exc).__name__}: {exc})")
    elif key is None and not use_vision:
        _anotar(diagnostics, "no se consultó el reconocimiento por imagen")

    if key is None:
        key = _from_artwork(project, texto_arte)
        source = "arte"

    if key is None:
        meta.pop("product_size", None)
        return None

    measurement = product_scale.measure_family(key, *hints)
    if measurement is None:
        meta.pop("product_size", None)
        return None

    record = {
        "family_key": key,
        "family": measurement.family,
        "height_cm": measurement.height_cm,
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
    }
    meta["product_size"] = record
    _recordar(clave, record)
    logger.info(
        "producto identificado en %s: capa %s -> %s (%s cm, %s)",
        project.project_id,
        layer.id,
        measurement.family,
        measurement.height_cm,
        source,
    )
    return record


def describe(layers) -> list[str]:
    """Frases para la interfaz: qué reconoció y de dónde lo sacó."""
    lineas: list[str] = []
    for layer in layers:
        record = (layer.meta or {}).get("product_size")
        if not record:
            continue
        lineas.append(
            f"«{layer.name}»: {record['family']} "
            f"({record['height_cm']:.0f} cm, {record.get('source_label', '')})".strip()
        )
    return lineas
