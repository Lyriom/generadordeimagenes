"""Tamaño real de los productos, para que un combo no mienta con las proporciones.

Un KV de combo mete dos o tres productos en el mismo hueco del arte. El motor
ajustaba cada uno por su cuenta a su casilla, así que el más estrecho salía el
más alto: un cilindro de gas más grande que la cocina que lo acompaña. Eso no es
un defecto de estilo, es una pieza que no se puede publicar.

Aquí está lo único que permite decidir cuál va más grande: cuánto mide cada
familia de producto en la vida real. Son altos típicos en centímetros de línea
blanca y electrónica de consumo, no la ficha técnica de un modelo concreto;
sirven para **ordenar tamaños entre sí**, que es lo que el ojo juzga en el arte.

Cuando no se reconoce el producto no se inventa nada: quien pregunta recibe
`None` y el motor iguala los altos, que es la única opción que no afirma que uno
sea más grande que otro.
"""
from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

#: Proporción 16:9: el alto útil es esta fracción de la diagonal.
_DIAGONAL_TO_HEIGHT = 9 / (9**2 + 16**2) ** 0.5
_INCH_TO_CM = 2.54
#: Marco y base de una pantalla sobre su área visible.
_SCREEN_CHROME = 1.08


class Measurement(NamedTuple):
    """Alto real de un producto y la familia por la que se reconoció."""

    height_cm: float
    family: str


#: (clave, patrón, alto típico en cm, nombre para los avisos).
#:
#: **Gana la primera que coincide**, así que el orden es la regla: cuando un
#: nombre menciona dos cosas («refrigeradora con congelador», «horno
#: microondas»), la que manda es la que aparece antes en esta lista. Por eso lo
#: dominante y lo específico va arriba.
#:
#: Solo entran familias cuyo alto es razonablemente estable. Una aspiradora va de
#: 30 cm (trineo) a 110 cm (vertical): meterla aquí sería cambiar un error por
#: otro, y sin datos el motor ya tiene una salida honesta.
_FAMILIES: tuple[tuple[str, str, float, str], ...] = (
    # --- línea blanca -------------------------------------------------
    ("refrigeradora", r"refrigerador\w*|nevera\w*|frigorific\w*|side\s*by\s*side", 170.0, "refrigeradora"),
    ("cocina", r"cocina\w*|estufa\w*", 90.0, "cocina"),
    ("lavadora", r"lavadora\w*|lava\s*seca\w*", 95.0, "lavadora"),
    ("secadora", r"secadora\w*", 95.0, "secadora"),
    ("lavavajillas", r"lavavajillas|lava\s*platos", 85.0, "lavavajillas"),
    ("congelador", r"congelador\w*|freezer\w*", 85.0, "congelador"),
    ("campana", r"campana\w*|extractor\w*\s+de\s+olor\w*", 55.0, "campana extractora"),
    ("calefon", r"calefon\w*|calentador\w*\s+de\s+agua", 60.0, "calefón"),
    ("cilindro_gas", r"cilindro\w*|bombona\w*|tanque\w*\s+de\s+gas", 58.0, "cilindro de gas"),
    # --- electrodomésticos pequeños -----------------------------------
    # El microondas va antes que el horno: «horno microondas» es un microondas.
    ("microondas", r"microonda\w*", 30.0, "microondas"),
    ("horno", r"\bhornos?\b", 60.0, "horno"),
    ("licuadora", r"licuadora\w*", 40.0, "licuadora"),
    ("batidora", r"batidora\w*", 33.0, "batidora"),
    ("arrocera", r"arrocera\w*", 28.0, "olla arrocera"),
    ("freidora", r"freidora\w*|air\s*fry\w*", 35.0, "freidora de aire"),
    ("cafetera", r"cafetera\w*", 33.0, "cafetera"),
    ("hervidor", r"hervidor\w*|jarra\w*\s+electric\w*", 25.0, "hervidor"),
    ("sanduchera", r"sanduchera\w*|wa?fflera\w*|waflera\w*", 12.0, "sanduchera"),
    ("exprimidor", r"exprimidor\w*|extractor\w*\s+de\s+jugo\w*", 40.0, "extractor de jugos"),
    ("ollas", r"juego\w*\s+de\s+ollas|bateria\w*\s+de\s+cocina", 25.0, "juego de ollas"),
    ("plancha", r"\bplanchas?\b(?!\s+de\s+induccion)", 15.0, "plancha"),
    # --- climatización ------------------------------------------------
    ("aire", r"aire\w*\s+acondicionado|\bsplit\b", 30.0, "aire acondicionado"),
    ("ventilador_pedestal", r"ventilador\w*\s+(?:de\s+)?pedestal", 130.0, "ventilador de pedestal"),
    ("dispensador", r"(?:dispensador|purificador)\w*\s+de\s+agua", 100.0, "dispensador de agua"),
    # --- electrónica ---------------------------------------------------
    ("soundbar", r"barra\w*\s+de\s+sonido|sound\s*bar", 9.0, "barra de sonido"),
    ("televisor", r"televisor\w*|smart\s*tv|\btv\b", 70.0, "televisor"),
    ("monitor", r"monitor\w*", 40.0, "monitor"),
    ("parlante", r"parlante\w*|torre\w*\s+de\s+sonido|bafle\w*", 100.0, "parlante"),
    ("consola", r"playstation|\bps[45]\b|xbox|nintendo\s*switch", 30.0, "consola"),
    ("laptop", r"laptop\w*|portatil\w*|notebook\w*", 25.0, "laptop"),
    ("tablet", r"tablet\w*|\bipad\b", 25.0, "tablet"),
    ("celular", r"celular\w*|smartphone\w*|telefono\w*|\biphone\b", 16.0, "celular"),
    ("audifonos", r"audifono\w*|auricular\w*|headphone\w*", 20.0, "audífonos"),
    # --- otros ---------------------------------------------------------
    ("bicicleta", r"bicicleta\w*|\bbici\b", 105.0, "bicicleta"),
)

#: Pantallas: las pulgadas del nombre son la diagonal y dan el alto real.
_SCREEN_FAMILIES = {"televisor", "monitor"}
#: Cocinas: las pulgadas del nombre son el ANCHO («20P» = 20 pulgadas de ancho),
#: nunca una diagonal. Confundirlas dejaría la cocina en 25 cm de alto.
_WIDTH_IN_INCHES_FAMILIES = {"cocina"}

#: Dos dígitos seguidos de la marca de pulgadas. Los códigos de quemadores («4Q»)
#: y de puertas («2P») son de un dígito y quedan fuera por el `\d{2}`.
_INCHES = re.compile(r"(?<!\d)(\d{2})(?!\d)\s*(?:\"|''|pulg\w*|p(?![a-z]))")


def normalise(text: str) -> str:
    """Minúsculas, sin acentos y con los separadores de archivo como espacios."""
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    plain = plain.lower()
    plain = re.sub(r"[_\-.+()\[\]]+", " ", plain)
    return re.sub(r"\s+", " ", plain).strip()


def _inches(haystack: str) -> int | None:
    found = [int(value) for value in _INCHES.findall(haystack)]
    usable = [value for value in found if 10 <= value <= 90]
    return max(usable) if usable else None


def measure(*texts: str | None) -> Measurement | None:
    """Alto real del producto que nombran esos textos, o `None` si no se reconoce."""
    haystack = " ".join(normalise(text) for text in texts if text)
    if not haystack:
        return None

    for key, pattern, height_cm, label in _FAMILIES:
        if not re.search(pattern, haystack):
            continue
        inches = _inches(haystack)
        if inches is not None and key in _SCREEN_FAMILIES:
            height_cm = inches * _DIAGONAL_TO_HEIGHT * _INCH_TO_CM * _SCREEN_CHROME
            label = f'{label} de {inches}"'
        elif inches is not None and key in _WIDTH_IN_INCHES_FAMILIES:
            # Una cocina de 30" es más ancha que una de 20", y algo más alta.
            height_cm = 95.0 if inches >= 26 else 90.0
            label = f'{label} de {inches}"'
        return Measurement(round(height_cm, 1), label)
    return None


def measure_layer(layer) -> Measurement | None:
    """Mide un producto por su origen, de la pista más fiable a la más pobre.

    El nombre del archivo que subió el usuario es el mejor dato
    («cocina-indurama-croma.png»); el de la capa del PSD suele ser «Capa 15». No
    se usa el nombre del grupo: describe al combo entero, así que mediría a todas
    sus piezas como si fueran la primera que menciona.
    """
    meta = getattr(layer, "meta", None) or {}
    for candidate in (
        meta.get("replaced_from"),
        getattr(layer, "name", None),
        meta.get("psd_name"),
    ):
        found = measure(candidate)
        if found is not None:
            return found
    return None


#: Nada baja de esta fracción del producto más alto. Un celular junto a una
#: refrigeradora saldría al 9% de su alto: fiel e ilegible, y en un combo lo que
#: se vende es el conjunto. Un diseñador agranda el pequeño por la misma razón.
MIN_RELATIVE_HEIGHT = 0.30

#: Desvío tolerado entre la proporción esperada y la que sale en el arte.
PROPORTION_TOLERANCE = 0.22


def relative_heights(
    measurements: list[Measurement | None],
) -> tuple[list[float], bool]:
    """Alto de cada producto respecto al más alto del grupo.

    Devuelve también si la proporción se apoya en medidas reales. Con menos de dos
    productos reconocidos no hay proporción que respetar y todos van iguales:
    igualar no afirma nada, y era justo lo contrario de lo que pasaba antes, donde
    el más estrecho salía siempre el más alto.
    """
    known = [item for item in measurements if item is not None]
    if len(known) < 2:
        return [1.0] * len(measurements), False

    tallest = max(item.height_cm for item in known)
    # Un producto sin reconocer va a la media de los reconocidos: no destaca ni
    # desaparece mientras el usuario no diga qué es.
    average = sum(item.height_cm for item in known) / len(known)
    heights = [(item.height_cm if item is not None else average) for item in measurements]
    return [max(MIN_RELATIVE_HEIGHT, value / tallest) for value in heights], True


def expected_ratio(first: Measurement, second: Measurement) -> float:
    """Proporción de altos que el motor puede prometer entre dos productos.

    No es el cociente crudo: el suelo de legibilidad de `MIN_RELATIVE_HEIGHT` es
    parte del diseño, así que comparar contra el cociente real marcaría como
    defecto algo que el motor hace a propósito.
    """
    raw = first.height_cm / second.height_cm
    return min(max(raw, MIN_RELATIVE_HEIGHT), 1 / MIN_RELATIVE_HEIGHT)


def proportion_conflicts(
    entries: list[tuple[str, Measurement | None, int]],
) -> list[str]:
    """Avisos por parejas de productos cuyo tamaño relativo no cuadra.

    `entries` son tripletes (nombre, medida, alto en píxeles). Solo se juzgan las
    parejas de las que se conocen las dos medidas: sin eso no hay nada que
    comparar y callar es lo correcto.
    """
    warnings: list[str] = []
    known = [
        (name, item, height)
        for name, item, height in entries
        if item is not None and height > 0
    ]
    for index, (name_a, measure_a, px_a) in enumerate(known):
        for name_b, measure_b, px_b in known[index + 1 :]:
            expected = expected_ratio(measure_a, measure_b)
            rendered = px_a / px_b
            if abs(rendered - expected) / expected <= PROPORTION_TOLERANCE:
                continue
            if rendered > expected:
                big, small, ratio, should = name_a, name_b, rendered, expected
            else:
                big, small, ratio, should = name_b, name_a, 1 / rendered, 1 / expected
            warnings.append(
                f"Proporción irreal: '{big}' sale {ratio:.1f}× el alto de '{small}' "
                f"y por tamaño real le toca {should:.1f}×."
            )
    return warnings
