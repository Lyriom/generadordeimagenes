"""Deducir la composición de la plantilla a partir del arte real.

El renderer tenía una retícula fija por categoría: titular arriba, producto en
medio, precio abajo a la izquierda. Servía para no dejar la pieza vacía, pero
producía siempre la misma pieza, mirase lo que mirase. El resultado en pantalla
era el arte de la marca de fondo y encima una maqueta que no era suya.

Aquí se hace lo contrario: el OCR ya leyó **dónde** puso el diseñador cada
cosa, y esas cajas se convierten en las posiciones de la plantilla. El titular
va donde iba el titular, el precio donde iba el precio y el legal donde iba el
legal, porque se han medido, no supuesto.

Reglas deliberadamente conservadoras: un hueco solo se fija cuando la señal es
franca. Lo que no se reconoce se deja a la retícula, que sigue siendo un
resultado aceptable. Colocar un precio donde había un legal es peor que
colocarlo en la posición genérica de siempre.
"""
from __future__ import annotations

import re
import unicodedata

from ..models.campaign import NormalizedPlacement

#: Debajo de esto la lectura no es fiable ni para situar una caja.
MIN_CONFIDENCE = .6
#: Una caja que ocupa más de esto no es un campo, es el arte entero mal leído.
MAX_FIELD_RATIO = .28

_MONEDA = re.compile(r"[$€]|\busd\b|\bs/\b", re.IGNORECASE)
_DIGITO = re.compile(r"\d")
#: Una cifra sola, con o sin símbolo: "359", "$9,99". Cuando la fuente no viaja
#: incrustada en el PDF, el "$" se lee como "S" ("S359"); también es un precio.
_SOLO_CIFRA = re.compile(r"^\s*[$S]?\s*\d[\d.,\s]*$")
_PORCENTAJE = re.compile(r"\d\s*%|\bdto\b|\bdescuento\b|\boff\b", re.IGNORECASE)
_ANTES = re.compile(r"\bantes\b|\bregular\b|\bde\s*\$|\bprecio\s+normal\b", re.IGNORECASE)
_CUOTA = re.compile(
    r"\bcuotas?\b|\bal\s*mes\b|\bmensual(?:es)?\b|\bsemanal(?:es)?\b|\bmeses\b|\bdesde\b",
    re.IGNORECASE,
)
_VIGENCIA = re.compile(
    r"\bvalid\w*\b|\bvigen\w*\b|\bhasta\s+el\b|\bdel\s+\d|\b\d{1,2}\s*/\s*\d{1,2}\b",
    re.IGNORECASE,
)
_LEGAL = re.compile(
    r"\bt[eé]rminos\b|\bcondiciones\b|\brestricciones\b|\baplican\b|\bconsulte\b"
    r"|\bstock\b|\bsuperintendencia\b|\bruc\b",
    re.IGNORECASE,
)
_CTA = re.compile(
    r"\bcompra\b|\bcomprar\b|\bvisita\b|\bvisítanos\b|\bllama\b|\bcot[ií]za\b"
    r"|\bpide\b|\bordena\b|\baprovecha\b|\bcl[ií]c\b|\bm[aá]s\s+info\b",
    re.IGNORECASE,
)


def _plano(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "")


def _caja(lectura: dict) -> tuple[int, int, int, int] | None:
    bbox = lectura.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        izq, arriba, der, abajo = (int(valor) for valor in bbox)
    except (TypeError, ValueError, OverflowError):
        return None
    if der <= izq or abajo <= arriba:
        return None
    return izq, arriba, der, abajo


def clasificar(
    lecturas: list[dict], size: tuple[int, int]
) -> dict[str, tuple[int, int, int, int]]:
    """Asigna a cada hueco la caja que ocupaba en el arte original.

    El texto no dice qué campo es; lo dicen su contenido, su tamaño y su sitio.
    Un precio lleva moneda y es de los más grandes; el legal es la línea más
    pequeña y está abajo; el titular es el bloque de mayor altura que no es
    ninguna de las dos cosas.
    """

    ancho, alto = size
    if ancho <= 0 or alto <= 0:
        return {}
    area_total = ancho * alto
    items: list[tuple[dict, tuple[int, int, int, int], int, str]] = []
    for lectura in lecturas:
        if not isinstance(lectura, dict):
            continue
        if float(lectura.get("confidence") or 0) < MIN_CONFIDENCE:
            continue
        caja = _caja(lectura)
        if caja is None:
            continue
        if (caja[2] - caja[0]) * (caja[3] - caja[1]) > area_total * MAX_FIELD_RATIO:
            continue
        items.append((lectura, caja, caja[3] - caja[1], _plano(str(lectura.get("text", "")))))
    if not items:
        return {}

    asignado: dict[str, tuple[int, int, int, int]] = {}
    usados: set[int] = set()

    def reservar(hueco: str, indice: int) -> None:
        if hueco in asignado or indice in usados:
            return
        asignado[hueco] = items[indice][1]
        usados.add(indice)

    # El wordmark ya viene marcado por la placa: es el logo, no contenido.
    for indice, (lectura, _caja_, _altura, _texto) in enumerate(items):
        if lectura.get("role") == "brand":
            reservar("logo", indice)

    # El legal: la línea más pequeña, en la franja inferior, y con su léxico.
    legales = [
        indice for indice, (_l, caja, altura, texto) in enumerate(items)
        if indice not in usados
        and (_LEGAL.search(texto) or (caja[1] / alto > .88 and altura / alto < .035))
    ]
    if legales:
        reservar("legal", min(legales, key=lambda i: items[i][2]))

    # La vigencia: fechas o "válido hasta", siempre pequeña.
    for indice, (_l, _caja_, altura, texto) in enumerate(items):
        if indice in usados or altura / alto > .05:
            continue
        if _VIGENCIA.search(texto):
            reservar("validity", indice)
            break

    # El descuento: un porcentaje suelto.
    for indice, (_l, _caja_, _altura, texto) in enumerate(items):
        if indice not in usados and _PORCENTAJE.search(texto):
            reservar("discount", indice)
            break

    # La cuota: "desde $25 al mes".
    for indice, (_l, _caja_, _altura, texto) in enumerate(items):
        if indice not in usados and _CUOTA.search(texto):
            reservar("installment", indice)
            break

    # Los precios: los que llevan moneda o son solo dígitos grandes. El mayor
    # es el precio actual; uno menor con "antes" es el precio anterior.
    precios = [
        indice for indice, (_l, _caja_, _altura, texto) in enumerate(items)
        if indice not in usados and _DIGITO.search(texto)
        and (_MONEDA.search(texto) or _ANTES.search(texto) or _SOLO_CIFRA.match(texto))
    ]
    anteriores = [indice for indice in precios if _ANTES.search(items[indice][3])]
    if anteriores:
        reservar("previous_price", max(anteriores, key=lambda i: items[i][2]))
    restantes = [indice for indice in precios if indice not in usados]
    if restantes:
        reservar("price", max(restantes, key=lambda i: items[i][2]))

    # El CTA: un verbo de acción, corto.
    for indice, (_l, _caja_, _altura, texto) in enumerate(items):
        if indice not in usados and _CTA.search(texto) and len(texto) <= 40:
            reservar("cta", indice)
            break

    # El titular: de lo que queda, el bloque de más altura. Y justo debajo, si
    # hay otro, el subtítulo: así se conserva la pareja tal como estaba.
    libres = [indice for indice in range(len(items)) if indice not in usados]
    if libres:
        titular = max(libres, key=lambda i: items[i][2])
        reservar("headline", titular)
        caja_titular = items[titular][1]
        debajo = [
            indice for indice in libres
            if indice not in usados
            and items[indice][1][1] >= caja_titular[3]
            and items[indice][1][1] - caja_titular[3] < alto * .10
        ]
        if debajo:
            reservar("subheadline", min(debajo, key=lambda i: items[i][1][1]))
    return asignado


#: Una franja libre menor que esto no es el sitio del producto, es un
#: interlineado: dejarlo a la retícula da mejor resultado que encajarlo ahí.
MIN_PRODUCT_BAND = .18


def hueco_producto(
    ocupadas, size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """La franja que el diseño dejó libre entre sus textos.

    El producto es lo único que no se puede leer con OCR, pero sí se puede
    deducir: ocupa el hueco. Sin esto, la foto usaba la caja de la retícula
    —centrada y enorme— y se comía el titular y el precio que acababan de
    colocarse en su sitio real.

    Cuentan **todas** las cajas de texto del arte, no solo las que se supieron
    clasificar: una que no se reconoció sigue siendo un sitio donde el diseño
    puso letra, y lo que no se reconoce se dibuja con la retícula, que puede
    caer justo ahí.
    """

    ancho, alto = size
    if ancho <= 0 or alto <= 0:
        return None
    cajas = list(ocupadas.values()) if isinstance(ocupadas, dict) else list(ocupadas)
    if not cajas:
        return None
    ocupado = [False] * alto
    for (_x0, y0, _x1, y1) in cajas:
        for y in range(max(0, y0), min(alto, y1)):
            ocupado[y] = True

    mejor = (0, 0)
    inicio = None
    for y in range(alto + 1):
        libre = y < alto and not ocupado[y]
        if libre and inicio is None:
            inicio = y
        elif not libre and inicio is not None:
            if y - inicio > mejor[1] - mejor[0]:
                mejor = (inicio, y)
            inicio = None
    arriba, abajo = mejor
    if abajo - arriba < alto * MIN_PRODUCT_BAND:
        return None
    # Un respiro contra los textos vecinos: pegado al titular se lee como un
    # error de montaje aunque las dos cajas sean correctas.
    margen = int((abajo - arriba) * .06)
    arriba, abajo = arriba + margen, abajo - margen
    if abajo <= arriba:
        return None
    return int(ancho * .06), arriba, int(ancho * .94), abajo


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def colores(lecturas: list[dict], size: tuple[int, int]) -> dict[str, str]:
    """Color con el que estaba escrito cada hueco en el arte original.

    El renderer pintaba todo en blanco. Sobre un fondo oscuro se leía; sobre la
    pastilla blanca que el propio arte trae bajo el precio, no: la pieza salía
    sin precio y nadie se enteraba hasta abrir el PNG. El color que el
    diseñador eligió ya era legible sobre ese fondo, y el fondo es el mismo.
    """

    cajas = clasificar(lecturas, size)
    por_caja = {
        tuple(lectura["bbox"]): str(lectura.get("color") or "")
        for lectura in lecturas
        if isinstance(lectura, dict) and isinstance(lectura.get("bbox"), (list, tuple))
    }
    salida: dict[str, str] = {}
    for hueco, caja in cajas.items():
        color = por_caja.get(tuple(caja), "")
        if _HEX.match(color):
            salida[hueco] = color.upper()
    return salida


def placements(
    lecturas: list[dict],
    size: tuple[int, int],
    safe: dict[str, float],
    *,
    product_box: tuple[int, int, int, int] | None = None,
) -> dict[str, NormalizedPlacement]:
    """Cajas del arte convertidas al sistema que entiende ``_layout``.

    ``_layout`` mide sus posiciones **dentro del área segura**, no del lienzo.
    La conversión se hace aquí una sola vez; recortar contra el área segura no
    es una pérdida: un titular que en el original salía pegado al borde queda
    dentro del margen que la red social no tapa.
    """

    ancho, alto = size
    if ancho <= 0 or alto <= 0:
        return {}
    cajas = clasificar(lecturas, size)
    todas = [
        caja for caja in (_caja(item) for item in lecturas if isinstance(item, dict))
        if caja is not None
    ]
    hueco = product_box if product_box is not None else hueco_producto(todas, size)
    if hueco is not None:
        cajas.setdefault("product", hueco)
    return normalize_boxes(cajas, size, safe)


def normalize_boxes(
    cajas: dict[str, tuple[int, int, int, int]],
    size: tuple[int, int],
    safe: dict[str, float],
) -> dict[str, NormalizedPlacement]:
    """Cajas en píxeles del lienzo convertidas a coordenadas del área segura."""

    ancho, alto = size
    if ancho <= 0 or alto <= 0:
        return {}
    izquierda, arriba = float(safe.get("left", .04)), float(safe.get("top", .04))
    derecha, abajo = float(safe.get("right", .04)), float(safe.get("bottom", .04))
    util_w, util_h = 1 - izquierda - derecha, 1 - arriba - abajo
    if util_w <= 0 or util_h <= 0:
        return {}
    resultado: dict[str, NormalizedPlacement] = {}
    for hueco, (x0, y0, x1, y1) in cajas.items():
        x = (x0 / ancho - izquierda) / util_w
        y = (y0 / alto - arriba) / util_h
        w = (x1 - x0) / ancho / util_w
        h = (y1 - y0) / alto / util_h
        # Recorte al área segura conservando lo que quepa.
        # Se redondea antes de recortar: con el orden inverso, el redondeo de
        # NormalizedPlacement podía devolver un alto una diezmilésima mayor que
        # el hueco disponible y dejar la caja fuera del lienzo.
        x, y = round(max(0.0, min(.99, x)), 4), round(max(0.0, min(.99, y)), 4)
        w, h = min(round(w, 4), round(1 - x, 4)), min(round(h, 4), round(1 - y, 4))
        if w <= 0 or h <= 0:
            continue
        resultado[hueco] = NormalizedPlacement(x=x, y=y, width=w, height=h)
    return resultado


def aspect_key(size: tuple[int, int]) -> str:
    """La misma familia que usa ``_layout`` para buscar las posiciones."""

    ancho, alto = size
    if ancho <= 0 or alto <= 0:
        return "square"
    if alto / ancho >= 1.62:
        return "story"
    if alto / ancho > 1.2:
        return "portrait"
    if ancho / alto > 1.35:
        return "landscape"
    return "square"


__all__ = [
    "aspect_key", "clasificar", "colores", "hueco_producto", "normalize_boxes", "placements",
]
