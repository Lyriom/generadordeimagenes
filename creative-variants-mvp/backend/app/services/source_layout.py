"""Recomponer con la retícula del arte original, no con una plantilla genérica.

Cuando el formato de salida no se parece al de origen, el motor no puede
conservar el diseño y cae en una familia de layout: «producto a la izquierda»,
«titular arriba». El resultado es correcto y anónimo —cada elemento aterriza
donde diga la plantilla, no donde estaba— y en un banner de 1920x325 volcado a
1080x1350 se nota: la pieza queda floja.

Las herramientas del sector no reordenan a ciegas: conservan lo que llaman
*layout intent*. Bannerflow lo anuncia con esas palabras («preserves layout
intent, focal points, safe areas, and copy balance»); SizeIM ancla el CTA a la
misma banda relativa en todos los tamaños; Canva describe Magic Switch como
«reorganize elements rather than just stretching». Las tres hacen lo mismo:
mantener el **orden de lectura** y la agrupación del original, y recolocar esos
grupos en el eje que el lienzo nuevo tenga libre.

Eso es lo que hay aquí, con dos diferencias respecto a la primera versión, que
salieron de medir el resultado:

- **El eje no se adivina, se elige.** Se resuelve el reparto en vertical y en
  horizontal, se mide cuánto lienzo llena cada uno con el contenido de verdad, y
  gana el mejor. Un banner en un lienzo alto se apila; el mismo banner en uno
  panorámico conserva sus columnas, sin regla escrita para cada caso.
- **El recorte no es a prorrata.** Cuando lo que piden los bloques no cabe, se
  quita primero de las imágenes y solo después del texto: un producto algo menor
  sigue siendo el producto, un titular reducido a una rendija deja de leerse. Es
  la misma prioridad que publica SizeIM en su guía de zonas seguras (mínimos de
  cuerpo de letra antes que tamaño de imagen).

Y la agrupación no es sólo «lo que iba junto va junto»: es **cómo** iba junto.
Tres líneas de copy apiladas en el arte se apilan también en la pieza nueva,
cada una con todo el ancho; puestas en columnas —que fue el primer intento—
«ELECTROMENORES» acababa en 23 px, ilegible. Solo cuando las bandas van por el
mismo eje que ya separaba a los bloques se reparten el ancho entre ellos.

Y una tercera, que costó dos mediciones: **el tamaño de cada banda no se
inventa**. Sale de lo que le daría la familia de layout de referencia, que son
proporciones ya afinadas y son las que hacen que una pieza se lea como una
pieza. Pedir para cada texto su cuerpo de letra máximo dejaba titulares de medio
lienzo y el producto en un cuarto de lo que le toca. Lo que aporta este módulo es
el **orden**, no una escala nueva: sobre esa base solo hay dos ajustes, que una
imagen no pida más de lo que sus píxeles dan y que un texto no baje del cuerpo
mínimo con el que se lee.

El módulo es geometría pura: recibe las medidas ya resueltas y devuelve zonas
relativas. Las decisiones de diseño —qué categorías entran, con qué cuerpo de
letra, cuántas líneas— son de `layout_engine`, que es quien las sabe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Zone = tuple[float, float, float, float]

#: Solape mínimo a lo largo del eje de lectura para que dos bloques se
#: consideren del mismo sitio del arte y viajen juntos en la misma banda. Medido
#: sobre el más pequeño de los dos: una franja larga no se anexiona a todo lo
#: que le pase por debajo.
GROUP_OVERLAP = 0.5

#: Ninguna banda baja de esto, por poco que pida su contenido. Una banda de 20 px
#: no es una banda: es una capa aplastada contra la de al lado.
MIN_BAND = 0.05

#: Cuerpo de letra mínimo para que un texto se lea, relativo al alto del lienzo.
#: Son unos 14 px en una pieza de 628 de alto y 30 en una de 1350: el mínimo que
#: recomiendan las guías de display para el texto principal.
MIN_FONT = 0.022

#: Aire entre bandas, relativo al lado por el que fluyen.
BAND_GAP = 0.022

#: Ancho medio de un carácter en proporción al cuerpo de la letra, y alto de
#: línea. Sirven para estimar el bloque que un texto ocupará de verdad, que es
#: lo que decide cuánta banda necesita. El renderer luego ajusta a lo exacto.
CHAR_WIDTH = 0.55
LINE_HEIGHT = 1.32


@dataclass
class Item:
    """Un bloque del arte, con lo justo para saber cuánto sitio necesita."""

    key: str
    #: Caja que ocupaba en el arte original, relativa a su lienzo (0..1).
    box: Zone
    #: Proporción ancho/alto del contenido. `None` en los textos: un texto no
    #: tiene proporción propia, se reparte en líneas según el ancho que le toque.
    aspect: float | None = None
    #: Píxeles reales del recorte. Marcan el techo: ampliar más allá de
    #: `max_upscale` no aporta nitidez, solo hueco desperdiciado en la banda.
    natural: tuple[int, int] | None = None
    max_upscale: float = 2.0
    #: Lo que le daría la familia de layout de referencia: alto de su zona en una
    #: composición apilada, ancho en una panorámica. Es el punto de partida del
    #: tamaño de su banda.
    ref_y: float = 0.12
    ref_x: float = 0.3
    #: Cuerpo máximo de la letra, relativo al alto del lienzo de salida.
    font_cap: float = 0.06
    max_lines: int = 2
    chars: int = 0

    @property
    def is_text(self) -> bool:
        return self.aspect is None


def _centre(box: Zone, axis: str) -> float:
    x, y, w, h = box
    return (x + w / 2) if axis == "x" else (y + h / 2)


def _extent(box: Zone, axis: str) -> tuple[float, float]:
    x, y, w, h = box
    return (x, x + w) if axis == "x" else (y, y + h)


def _overlap(a: Zone, b: Zone, axis: str) -> float:
    """Cuánto se pisan dos cajas en un eje, en proporción a la más corta."""
    a0, a1 = _extent(a, axis)
    b0, b1 = _extent(b, axis)
    solape = min(a1, b1) - max(a0, b0)
    if solape <= 0:
        return 0.0
    menor = min(a1 - a0, b1 - b0)
    return solape / menor if menor > 0 else 0.0


def reading_axis(items: list[Item], source_canvas: tuple[int, int]) -> str:
    """Eje por el que se lee el arte original.

    Un arte ancho se lee en horizontal y uno alto en vertical. Cuando la forma no
    lo dice —un cuadrado—, lo dice el reparto: el eje en el que los bloques están
    más desplegados es el que ordena la lectura.
    """
    source_w, source_h = source_canvas
    aspect = source_w / max(1, source_h)
    if aspect >= 1.15:
        return "x"
    if aspect <= 0.87:
        return "y"
    centros_x = [_centre(item.box, "x") for item in items]
    centros_y = [_centre(item.box, "y") for item in items]
    return (
        "x"
        if (max(centros_x) - min(centros_x)) >= (max(centros_y) - min(centros_y))
        else "y"
    )


def clusters(items: list[Item], axis: str) -> list[list[Item]]:
    """Los bloques repartidos en los tramos que ocupaban del eje de lectura.

    Los tramos salen en el orden en que se leen y, dentro de cada uno, los
    bloques en el orden en que estaban apilados. Ese doble orden es lo que hay
    que conservar: ordenar solo por el centro del eje ya bastaba para colar el
    precio por delante del titular, porque el titular es más ancho y su centro
    cae más a la derecha aunque los dos empiecen en el mismo sitio.
    """
    minor = "y" if axis == "x" else "x"
    ordenados = sorted(
        items, key=lambda item: (_extent(item.box, axis)[0], _centre(item.box, minor))
    )
    tramos: list[list[Item]] = []
    for item in ordenados:
        junto = tramos and any(
            _overlap(item.box, previo.box, axis) >= GROUP_OVERLAP for previo in tramos[-1]
        )
        if junto:
            tramos[-1].append(item)
        else:
            tramos.append([item])
    return [sorted(tramo, key=lambda item: _centre(item.box, minor)) for tramo in tramos]


def _groups(tramos: list[list[Item]], axis: str, flow: str) -> list[list[Item]]:
    """Cómo se convierten los tramos del arte en bandas del lienzo nuevo.

    Compartir banda significa repartirse su ancho, y eso solo tiene sentido si en
    el arte los bloques ya estaban separados por ese mismo eje. Tres líneas de
    copy apiladas se apilan también aquí, cada una con todo el ancho: meterlas en
    columnas dejaba «ELECTROMENORES» en 23 px. En cambio, en un lienzo
    panorámico ese mismo tramo sí es una columna y sus bloques se reparten el
    alto, porque es como estaban.
    """
    minor = "y" if axis == "x" else "x"
    if minor == flow:
        return [[item] for tramo in tramos for item in tramo]
    return [list(tramo) for tramo in tramos]


def _shares(group: list[Item], axis: str) -> list[float]:
    """Reparto del ancho de banda entre los bloques que la comparten.

    Proporcional a lo que ocupaban en el arte, con un suelo: dos bloques en la
    misma banda y uno de ellos reducido a una rendija es peor que repartir.
    """
    minor = "y" if axis == "x" else "x"
    crudos = [max(0.04, item.box[3] if minor == "y" else item.box[2]) for item in group]
    total = sum(crudos)
    return [valor / total for valor in crudos]


def _fitted(item: Item, box_w: float, box_h: float, canvas_h: int) -> tuple[float, float]:
    """Lo que el contenido llega a ocupar de verdad dentro de una caja."""
    if not item.is_text:
        aspect = item.aspect or 1.0
        ancho = min(box_w, box_h * aspect)
        alto = ancho / aspect
        if item.natural:
            tope_w = item.natural[0] * item.max_upscale
            if ancho > tope_w:
                ancho, alto = tope_w, tope_w / aspect
        return ancho, alto
    font_px = max(1.0, item.font_cap * canvas_h)
    una_linea = max(1.0, item.chars * CHAR_WIDTH * font_px)
    lineas = min(item.max_lines, max(1, math.ceil(una_linea / max(1.0, box_w))))
    alto = min(box_h, lineas * font_px * LINE_HEIGHT)
    ancho = min(box_w, una_linea / lineas * 1.12)
    return ancho, alto


def _text_band(
    item: Item, font_rel: float, cross_px: float, flow: str, canvas_w: int, canvas_h: int
) -> float:
    """Banda que ocupa un texto compuesto a un cuerpo de letra dado."""
    font_px = max(1.0, font_rel * canvas_h)
    una_linea = max(1.0, item.chars * CHAR_WIDTH * font_px)
    lineas = min(item.max_lines, max(1, math.ceil(una_linea / max(1.0, cross_px))))
    if flow == "y":
        return (lineas * font_px * LINE_HEIGHT) / max(1, canvas_h)
    return min(cross_px, una_linea / lineas * 1.12) / max(1, canvas_w)


def _demand(
    item: Item, cross_px: float, flow: str, canvas_w: int, canvas_h: int
) -> tuple[float, float, float]:
    """Lo que pide un bloque, lo mínimo que admite y su techo.

    El punto de partida es lo que le daría la familia de referencia. Un texto no
    baja del cuerpo con el que se lee ni sube de su cuerpo máximo; una imagen no
    pasa de los píxeles que tiene: pedir banda para un tamaño al que no va a
    llegar solo deja hueco vacío, y es lo que convertía un logo de 210 px en
    media pieza.
    """
    base = item.ref_y if flow == "y" else item.ref_x
    if item.is_text:
        piso = _text_band(item, MIN_FONT, cross_px, flow, canvas_w, canvas_h)
        techo = _text_band(item, item.font_cap, cross_px, flow, canvas_w, canvas_h)
        piso = max(MIN_BAND, min(piso, techo))
        return max(piso, min(base, techo)), piso, max(techo, piso)

    canvas_flow = canvas_h if flow == "y" else canvas_w
    techo = 1.0
    if item.natural:
        natural_flow = item.natural[1] if flow == "y" else item.natural[0]
        techo = (natural_flow * item.max_upscale) / max(1, canvas_flow)
    techo = max(MIN_BAND, techo)
    return max(MIN_BAND, min(base, techo)), MIN_BAND, techo


def _trim(
    demandas: list[float], pisos: list[float], texto: list[bool], hueco: float
) -> list[float]:
    """Ajusta las bandas al hueco quitando primero de donde menos duele.

    Dos rondas: la primera solo recorta imágenes, la segunda ya toca el texto.
    Dentro de cada ronda cada banda cede en proporción a la holgura que tenga
    sobre su piso, de modo que la que pidió de más es la que más devuelve.
    """
    ajustadas = list(demandas)
    for solo_imagenes in (True, False):
        exceso = sum(ajustadas) - hueco
        if exceso <= 1e-9:
            return ajustadas
        indices = [
            i for i in range(len(ajustadas)) if not (solo_imagenes and texto[i])
        ]
        holguras = {i: max(0.0, ajustadas[i] - pisos[i]) for i in indices}
        total = sum(holguras.values())
        if total <= 0:
            continue
        recorte = min(exceso, total)
        for indice, holgura in holguras.items():
            ajustadas[indice] -= recorte * (holgura / total)
    total = sum(ajustadas)
    if total > hueco and total > 0:
        # Ni con los pisos cabe: no hay nada más que dar y se reparte a prorrata.
        ajustadas = [valor * (hueco / total) for valor in ajustadas]
    return ajustadas


def _grow(
    demandas: list[float], techos: list[float], texto: list[bool], hueco: float
) -> list[float]:
    """Reparte el aire que sobra, primero a las imágenes y sin pasar del techo.

    Repartirlo entre todos a partes iguales es lo que dejaba la pieza floja:
    mucho blanco y un producto pequeño en medio. Y repartirlo sin techo la deja
    borrosa, que es peor.
    """
    ajustadas = list(demandas)
    for solo_imagenes in (True, False):
        for _ in range(4):
            sobrante = hueco - sum(ajustadas)
            if sobrante <= 1e-9:
                return ajustadas
            indices = [
                i
                for i in range(len(ajustadas))
                if ajustadas[i] < techos[i] - 1e-9 and not (solo_imagenes and texto[i])
            ]
            if not indices:
                break
            base = sum(ajustadas[i] for i in indices) or 1.0
            for indice in indices:
                cuota = sobrante * (ajustadas[indice] / base)
                ajustadas[indice] = min(techos[indice], ajustadas[indice] + cuota)
    return ajustadas


def _solve(
    tramos: list[list[Item]],
    axis: str,
    flow: str,
    canvas_w: int,
    canvas_h: int,
    margin: tuple[float, float],
    reserve: float,
) -> tuple[dict[str, Zone], float]:
    """Reparte las bandas por un eje y devuelve las zonas y lo que llenan.

    La puntuación es el área que el contenido llega a ocupar sobre el lienzo: la
    misma medida con la que después se juzga la pieza. Así el eje no se elige por
    una regla escrita, sino por el resultado.
    """
    grupos = _groups(tramos, axis, flow)
    repartos = [_shares(grupo, axis) for grupo in grupos]
    margin_x, margin_y = margin
    margen_flow = margin_y if flow == "y" else margin_x
    margen_cross = margin_x if flow == "y" else margin_y
    cross = max(0.1, 1.0 - 2 * margen_cross - (reserve if flow == "x" else 0.0))
    cross_px = cross * (canvas_w if flow == "y" else canvas_h)
    canvas_flow = canvas_h if flow == "y" else canvas_w

    hueco = (
        1.0
        - 2 * margen_flow
        - BAND_GAP * (len(grupos) - 1)
        - (reserve if flow == "y" else 0.0)
    )
    if hueco <= 0:
        return {}, 0.0
    if not grupos:
        return {}, 0.0

    demandas: list[float] = []
    pisos: list[float] = []
    techos: list[float] = []
    texto: list[bool] = []
    for grupo, shares in zip(grupos, repartos):
        # Los miembros de una banda van uno al lado del otro, así que la banda
        # necesita lo del que más pida, no la suma.
        medidas = [
            _demand(item, cross_px * share, flow, canvas_w, canvas_h)
            for item, share in zip(grupo, shares)
        ]
        demandas.append(max(max(pedida for pedida, _, _ in medidas), MIN_BAND))
        pisos.append(max(MIN_BAND, max(piso for _, piso, _ in medidas)))
        techos.append(max(techo for _, _, techo in medidas))
        texto.append(all(item.is_text for item in grupo))

    if sum(demandas) > hueco:
        demandas = _trim(demandas, pisos, texto, hueco)
    else:
        demandas = _grow(demandas, techos, texto, hueco)

    # El aire que ni las imágenes ni el texto pueden usar no se apila al final:
    # se reparte entre las bandas. Si no, la pieza sale con un claro enorme en
    # medio y todo lo demás pegado.
    gap = BAND_GAP
    holgura = hueco - sum(demandas)
    if holgura > 1e-9 and len(grupos) > 1:
        gap += holgura / (len(grupos) - 1)

    zones: dict[str, Zone] = {}
    llenado = 0.0
    posicion = margen_flow
    for grupo, shares, banda in zip(grupos, repartos, demandas):
        avance = margen_cross
        for item, share in zip(grupo, shares):
            ancho = cross * share
            if flow == "y":
                zones[item.key] = (avance, posicion, ancho, banda)
                caja = (ancho * canvas_w, banda * canvas_h)
            else:
                zones[item.key] = (posicion, avance, banda, ancho)
                caja = (banda * canvas_w, ancho * canvas_h)
            avance += ancho
            fit_w, fit_h = _fitted(item, caja[0], caja[1], canvas_h)
            llenado += (fit_w * fit_h) / max(1, canvas_w * canvas_h)
        posicion += banda + gap
    return zones, llenado


def derive_zones(
    items: list[Item],
    source_canvas: tuple[int, int],
    canvas_w: int,
    canvas_h: int,
    *,
    margin: tuple[float, float] = (0.035, 0.035),
    reserve: float = 0.0,
) -> tuple[dict[str, Zone], list[str]]:
    """Zonas para el lienzo de salida siguiendo la retícula del original.

    `reserve` es el trozo que hay que dejar libre para lo que no entra en el
    reflujo y tiene sitio propio —el texto legal, anclado al pie—. Sin eso la
    última banda cae encima del legal y el motor la manda a otro lado, que es
    justo perder el orden que se estaba conservando.

    Devuelve `({}, [])` cuando no hay nada que reordenar: con un solo bloque no
    existe orden de lectura que conservar y la plantilla genérica sirve igual.
    """
    if len(items) < 2:
        return {}, []

    axis = reading_axis(items, source_canvas)
    tramos = clusters(items, axis)

    candidatos = [
        (flow, *_solve(tramos, axis, flow, canvas_w, canvas_h, margin, reserve))
        for flow in ("y", "x")
    ]
    flow, zones, llenado = max(candidatos, key=lambda candidato: candidato[2])
    if not zones:
        return {}, []
    bloques = len(_groups(tramos, axis, flow))

    sentido = {
        ("x", "y"): "de izquierda a derecha pasó a ser de arriba a abajo",
        ("y", "x"): "de arriba a abajo pasó a ser de izquierda a derecha",
        ("x", "x"): "se conservó de izquierda a derecha",
        ("y", "y"): "se conservó de arriba a abajo",
    }[(axis, flow)]
    notes = [
        f"Recompuesto con la retícula del arte: {bloques} bloque(s), el orden de "
        f"lectura {sentido} y el contenido llena el {int(round(llenado * 100))}% del lienzo."
    ]
    return zones, notes
