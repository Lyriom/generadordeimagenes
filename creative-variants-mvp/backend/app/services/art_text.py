"""Editar el copy del arte: reescribir un texto rasterizado o quitar un elemento.

Un KV real llega con el copy y el logo como **píxeles**. El PSD los trae como
objetos inteligentes y el importador los conserva tal cual para no perder la
tipografía de marca: es lo correcto mientras el texto no cambie, y es justo lo
que impedía producir la fila de artes de una promoción, donde cada producto
lleva su nombre, su modelo y su precio.

Este servicio hace ese cambio sin tocar el resto del diseño:

- **Medir** el recorte original: color de la tinta, alto de línea, interlínea,
  alineación y peso. Son los datos que hacen que el texto nuevo se lea como el
  viejo en vez de como un parche.
- **Reescribir**: la capa pasa a ser texto con el cuerpo calculado para que su
  tinta ocupe el mismo alto que la original, anclada al mismo borde. Nada más
  se recoloca.
- **Quitar**: ocultar el elemento y, solo si sus píxeles siguen dentro de la
  plancha de fondo, borrarlos de ahí. Sin eso, ocultar un logo aplanado no
  quita nada de la salida.

Todo es reversible. El PNG original se conserva y la plancha se rehace siempre
desde una copia anterior a cualquier edición, así que deshacer no deja restos ni
acumula reconstrucciones sobre reconstrucciones.
"""
from __future__ import annotations

import io
import logging
import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from ..models import Layer, LayerCategory, LayerType, Project
from . import layer_extraction, renderer, storage
from .imaging import dilate_mask, rgb_to_hex
from .layout_engine import Placement

logger = logging.getLogger(__name__)

#: Alfa a partir del cual un píxel cuenta como tinta y no como borde suavizado.
INK_ALPHA = 96

#: Un recorte casi sin transparencia no viene de un PSD: es un rectángulo del
#: arte aplanado. Ahí la tinta se separa por color, no por alfa.
OPAQUE_PATCH_RATIO = 0.92

#: Distancia de color mínima para considerar que un píxel es tinta sobre el
#: fondo del propio parche.
INK_COLOR_DISTANCE = 60.0

#: Margen lateral mínimo entre un texto que crece y su vecino, en fracción del
#: ancho del lienzo. Sin él, un precio más largo se mete debajo del titular.
NEIGHBOUR_GAP = 0.012

#: Un tramo de tinta más bajo que esta fracción de una línea completa no es una
#: línea: es una tilde, un punto o una cedilla suelta de la línea de al lado.
FRAGMENT_HEIGHT = 0.5

#: …y solo se une si está a menos de esta fracción de su línea.
FRAGMENT_REACH = 0.5

#: Copia de la plancha anterior a cualquier edición de arte. Rehacer siempre
#: desde aquí es lo que permite deshacer sin restos.
PLATE_BASELINE_REL = "backgrounds/plancha_sin_ediciones.png"
PLATE_REL = "backgrounds/background.png"

#: Diferencia media por debajo de la cual la plancha y el arte original son el
#: mismo píxel: el elemento sigue aplanado en el fondo y hay que borrarlo.
PLATE_SAME_PIXELS = 8.0

#: Categorías que este editor gestiona. Producto y persona tienen su propio
#: flujo (reemplazo y recorte), y el fondo no es un elemento editable.
EXCLUDED_CATEGORIES = {
    LayerCategory.BACKGROUND,
    LayerCategory.PRODUCT,
    LayerCategory.PERSON,
}


@dataclass
class TextStyle:
    """Cómo estaba escrito el texto original, medido sobre sus píxeles."""

    color: str
    align: str
    lines: int
    ink_height: int
    line_height: float
    #: Grosor de trazo relativo al alto de tinta. Es lo que separa una redonda de
    #: una negrita mejor que la densidad, que depende de qué letras hay escritas.
    stroke: float = 0.0
    #: Distancia entre el inicio de dos líneas, en píxeles del arte original.
    line_pitch: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class ArtTextError(ValueError):
    """El elemento no admite la operación pedida."""


# ------------------------------------------------------------------- medición
def _layer_png(project: Project, layer: Layer) -> Path | None:
    """PNG con los píxeles originales del elemento, aunque ya se haya reescrito."""
    origin = layer.meta.get("art_text") or {}
    relative = origin.get("src") or layer.src
    if not relative:
        return None
    path = storage.abs_path(project.project_id, relative)
    return path if path.exists() else None


def _ink_mask(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Qué píxeles son tinta.

    Un recorte de PSD lo dice con su alfa. Un rectángulo tomado de un arte
    aplanado llega opaco entero, así que ahí la tinta es lo que se separa del
    color de fondo del propio parche.
    """
    solid = alpha >= INK_ALPHA
    if not solid.any():
        return solid
    if solid.mean() < OPAQUE_PATCH_RATIO:
        return solid

    flat = rgb[solid].reshape(-1, 3).astype(np.float32)
    backdrop = _dominant_rgb(flat)
    distance = np.linalg.norm(rgb.astype(np.float32) - np.asarray(backdrop, np.float32), axis=2)
    ink = solid & (distance > INK_COLOR_DISTANCE)
    # Un parche liso sin nada escrito no debe declararse "todo tinta".
    return ink if ink.any() else np.zeros_like(solid)


def _dominant_rgb(pixels: np.ndarray) -> tuple[int, int, int]:
    """Color más repetido, promediado dentro de su propio cubo de color.

    Cuantizar antes de contar agrupa el antialias con el trazo al que pertenece;
    la mediana por canal, en cambio, inventa un color que no está en la imagen.
    """
    if pixels.size == 0:
        return (0, 0, 0)
    buckets = (pixels // 16).astype(np.int32)
    keys = buckets[:, 0] * 4096 + buckets[:, 1] * 64 + buckets[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    winner = values[int(np.argmax(counts))]
    chosen = pixels[keys == winner]
    red, green, blue = (int(round(value)) for value in chosen.mean(axis=0))
    return red, green, blue


def _ink_rows(ink: np.ndarray) -> np.ndarray:
    """Qué filas del recorte tienen tinta.

    `ndarray.any(axis=1)` está declarado como "escalar o array" porque con un
    array de cero dimensiones devuelve un escalar. Aquí siempre es 2D, pero el
    analizador no lo sabe y marcaba en rojo cada llamada.
    """
    return np.atleast_1d(np.asarray(ink.any(axis=1)))


def _ink_stroke(ink: np.ndarray) -> float:
    """Grosor de trazo de un bloque de tinta, normalizado por su alto de línea.

    Se mide sobre **todo** el texto, no sobre la primera línea: el original y el
    candidato tienen que pasar por el mismo procedimiento o la comparación de
    pesos compara dos mezclas de letras distintas.
    """
    runs = _line_runs(_ink_rows(ink))
    if not runs:
        return 0.0
    heights = [bottom - top + 1 for top, bottom in runs]
    return _stroke_ratio(ink, max(1, int(np.median(heights))))


def _stroke_ratio(ink: np.ndarray, ink_height: int) -> float:
    """Grosor medio de trazo dividido por el alto de tinta.

    La densidad —tinta partido por caja— no sirve para decidir el peso: una línea
    de dígitos con espacios es menos densa en negrita que una de mayúsculas en
    redonda. El grosor del palo, en cambio, es de la tipografía y no del texto.
    """
    runs: list[int] = []
    for row in ink:
        length = 0
        for filled in row:
            if filled:
                length += 1
            elif length:
                runs.append(length)
                length = 0
        if length:
            runs.append(length)
    if not runs:
        return 0.0
    return float(np.median(runs)) / max(1, ink_height)


def _rendered_stroke(font, text: str, line_height: float) -> float:
    """Grosor relativo del texto nuevo escrito con una tipografía concreta."""
    lines = text.split("\n")
    ascent, descent = font.getmetrics()
    line_px = max(1, int((ascent + descent) * line_height))
    width = int(max((_SCRATCH.textlength(line, font=font) for line in lines), default=1))
    canvas = Image.new("L", (width + 16, line_px * len(lines) + 16), 0)
    draw = ImageDraw.Draw(canvas)
    for index, line in enumerate(lines):
        draw.text((8, 8 + index * line_px), line, font=font, fill=255)
    return _ink_stroke(np.asarray(canvas) > 96)


def _ink_runs(rows: np.ndarray) -> list[tuple[int, int]]:
    """Tramos verticales con tinta, tal cual, sin unir nada."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, filled in enumerate(rows):
        if filled and start is None:
            start = index
        elif not filled and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(rows) - 1))
    return runs


def _line_runs(rows: np.ndarray) -> list[tuple[int, int]]:
    """Tramos verticales con tinta: una entrada por línea de texto.

    El único tramo que hay que unir al de al lado es un **fragmento**: la tilde
    de una Á, el punto de una i, la cedilla. Se reconocen por ser mucho más
    bajos que una línea completa, no por estar cerca.

    Unir por cercanía fue el primer intento y estaba mal: dos líneas de copy con
    la interlínea normal de un KV están a menos de un tercio de su alto, así que
    se fusionaban en una sola y el alto de tinta salía del doble. El texto nuevo
    se escribía entonces a más del doble de cuerpo que el original.
    """
    runs = [list(run) for run in _ink_runs(rows)]
    if len(runs) < 2:
        return [(run[0], run[1]) for run in runs]

    # Referencia de "línea completa": el tramo más alto. La mediana no sirve
    # cuando solo hay una línea y su tilde, porque promedia las dos.
    reference = max(run[1] - run[0] + 1 for run in runs)
    fragment = reference * FRAGMENT_HEIGHT
    reach = reference * FRAGMENT_REACH

    merged = [runs[0]]
    for run in runs[1:]:
        previous = merged[-1]
        gap = run[0] - previous[1]
        smaller = min(run[1] - run[0] + 1, previous[1] - previous[0] + 1)
        if smaller < fragment and gap <= reach:
            previous[1] = max(previous[1], run[1])
        else:
            merged.append(run)
    return [(run[0], run[1]) for run in merged]


def _alignment(ink: np.ndarray, runs: list[tuple[int, int]], center_ratio: float) -> str:
    """Alineación real del bloque; con una sola línea, la que deja crecer al texto."""
    if len(runs) >= 2:
        lefts, rights = [], []
        for top, bottom in runs:
            columns = np.nonzero(ink[top : bottom + 1].any(axis=0))[0]
            if columns.size:
                lefts.append(int(columns[0]))
                rights.append(int(columns[-1]))
        if len(lefts) >= 2:
            spread = {
                "left": max(lefts) - min(lefts),
                "right": max(rights) - min(rights),
                # El centro se mide sobre la suma de bordes: se divide entre dos
                # para compararlo en la misma escala que los otros dos repartos.
                "center": (
                    max(l + r for l, r in zip(lefts, rights))
                    - min(l + r for l, r in zip(lefts, rights))
                )
                // 2,
            }
            return min(spread, key=lambda key: spread[key])

    # Una línea suelta no revela su alineación. Se elige por dónde está en el
    # arte, que es hacia dónde puede crecer sin pisar a su vecino.
    if center_ratio < 0.4:
        return "left"
    if center_ratio > 0.6:
        return "right"
    return "center"


def measure(project: Project, layer: Layer) -> TextStyle | None:
    """Estilo del texto original. `None` si el elemento no tiene tinta legible."""
    # El fondo de un bloque separado es la caja, el sello y el marco sin su
    # texto: un dibujo. Medirlo daba un renglón del alto del bloque entero.
    if layer.meta.get("art_piece") == PIECE_PLATE:
        return None
    path = _layer_png(project, layer)
    if path is None:
        return None
    with Image.open(path) as opened:
        art = opened.convert("RGBA")
        rgb = np.asarray(art.convert("RGB"), dtype=np.uint8)
        alpha = np.asarray(art.getchannel("A"), dtype=np.uint8)

    ink, _ = _ink_and_plates(rgb, alpha)
    if not ink.any():
        return None

    # Sin el filete: una barra de color pegada al texto soldaba sus renglones
    # y el alto de tinta salía el de todos juntos, así que el texto nuevo se
    # escribía a ese cuerpo.
    ink, _ = _split_ink(_denoise(ink))
    if not ink.any():
        return None
    runs = _line_runs(_ink_rows(ink))
    if not runs:
        return None
    heights = [bottom - top + 1 for top, bottom in runs]
    ink_height = max(1, int(np.median(heights)))

    # Interlínea real: distancia entre los inicios de línea consecutivos. Se
    # guarda en píxeles porque el factor depende de las métricas de la
    # tipografía con la que se acabe escribiendo, que aquí todavía no se sabe.
    if len(runs) >= 2:
        line_pitch = int(round(float(np.median(np.diff([top for top, _ in runs])))))
        line_height = max(0.62, min(2.4, line_pitch / max(1.0, ink_height * 1.32)))
    else:
        line_pitch = 0
        line_height = 1.15

    box = layer.meta.get("art_text", {}).get("box") or [
        layer.x, layer.y, layer.width, layer.height
    ]
    canvas_w = max(1, project.canvas.width)
    center_ratio = (box[0] + box[2] / 2) / canvas_w

    solid = alpha >= 200
    ink_pixels = rgb[ink & solid] if (ink & solid).any() else rgb[ink]
    color = rgb_to_hex(_dominant_rgb(ink_pixels.reshape(-1, 3).astype(np.float32)))

    return TextStyle(
        color=color,
        align=_alignment(ink, runs, center_ratio),
        lines=len(runs),
        ink_height=ink_height,
        line_height=round(line_height, 3),
        stroke=round(_stroke_ratio(ink, ink_height), 4),  # ya normalizado por línea
        line_pitch=line_pitch,
    )


# ------------------------------------------------------------------ escritura
_SCRATCH = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _first_ink_line(content: str) -> str:
    for line in content.split("\n"):
        if line.strip():
            return line
    return content.strip() or "0"


def _size_for_ink_height(font_path: str | None, sample: str, target: int) -> int:
    """Cuerpo cuya tinta mide lo mismo que la del texto original.

    Se busca sobre el texto nuevo, no sobre el viejo: así el reemplazo ocupa el
    mismo alto visual aunque cambie de mayúsculas a números o pierda las tildes.
    """
    small = renderer.load_font(font_path, 10).getbbox(sample)
    large = renderer.load_font(font_path, 100).getbbox(sample)
    if (large[3] - large[1]) <= (small[3] - small[1]):
        # Tipografía de mapa de bits (PIL sin TrueType): no escala, así que el
        # cuerpo se estima por la proporción habitual entre alto de caja y tinta.
        return max(6, int(round(target / 0.72)))

    low, high, best = 6, 800, 12
    while low <= high:
        middle = (low + high) // 2
        font = renderer.load_font(font_path, middle)
        box = font.getbbox(sample)
        height = max(1, box[3] - box[1])
        if height <= target:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return max(6, best)


def _block_metrics(font, content: str, line_height: float) -> tuple[int, int, int, int]:
    """(ancho, alto del bloque, desplazamiento de la tinta, alto de la tinta).

    El bloque es lo que el renderer dibuja; la tinta, lo que se ve. La caja de
    la capa mide la tinta —igual que la medía el recorte original—, así que las
    dos medidas hacen falta: una para dibujar y otra para ocupar sitio.
    """
    lines = content.split("\n")
    ascent, descent = font.getmetrics()
    line_px = int((ascent + descent) * line_height)
    widths = [_SCRATCH.textlength(line, font=font) for line in lines]
    block_w = int(math.ceil(max(widths))) if widths else 1
    block_h = int(line_px * len(lines))
    ink_top, ink_h = renderer.ink_offsets(lines, font, line_height)
    return max(1, block_w), max(1, block_h), ink_top, max(1, ink_h)


def _choose_face(
    project: Project,
    style: TextStyle,
    text: str,
    *,
    weight: str | None,
    font_size: int | None,
) -> tuple[str, str | None, int, object]:
    """Tipografía, peso y cuerpo con los que escribir.

    El peso no se adivina por la densidad del original: se escribe el texto
    **nuevo** en cada peso disponible y se conserva el que da el mismo grosor de
    palo que tenía el arte. Así la comparación no depende de qué letras había.
    """
    options = [weight] if weight else ["normal", "bold"]
    sample = _first_ink_line(text)
    candidates = []
    for option in options:
        path = renderer.resolve_font_path(project, option)
        size = int(font_size) if font_size else _size_for_ink_height(
            path, sample, style.ink_height
        )
        font = renderer.load_font(path, size)
        candidates.append(
            (option, path, size, font, _rendered_stroke(font, text, _line_height(font, style)))
        )
    if len(candidates) == 1 or style.stroke <= 0:
        option, path, size, font, _ = candidates[0]
        return option, path, size, font
    option, path, size, font, _ = min(
        candidates, key=lambda item: abs(item[4] - style.stroke)
    )
    return option, path, size, font


def _line_height(font, style: TextStyle) -> float:
    """Interlínea que reproduce el paso entre líneas medido en el arte."""
    if not style.line_pitch:
        return style.line_height
    ascent, descent = font.getmetrics()
    # El suelo era 0.85 y recortaba las interlíneas apretadas de verdad: un copy
    # de retail va a 0.8 o menos y salía un 7 % más suelto que el original. Con
    # la medida de líneas ya fiable, el paso medido merece más crédito.
    return max(0.62, min(2.4, style.line_pitch / max(1, ascent + descent)))


def _free_span(
    project: Project, layer: Layer, box: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Bordes entre los que el texto puede crecer sin invadir a su vecino.

    Devuelve los dos bordes y no solo el ancho: un precio alineado a la derecha
    crece hacia la izquierda, así que saber cuánto cabe no basta para colocarlo.
    El hueco que ya ocupaba el original siempre entra: nunca se le quita sitio.
    """
    box_x, box_y, box_w, box_h = box
    gap = max(4, int(project.canvas.width * NEIGHBOUR_GAP))
    left = 0
    right = project.canvas.width
    for other in project.layers:
        if other.id == layer.id or not other.visible:
            continue
        if other.category == LayerCategory.BACKGROUND:
            continue
        if other.y >= box_y + box_h or other.y + other.height <= box_y:
            continue  # no comparte banda vertical: no estorba
        if other.x + other.width <= box_x:
            left = max(left, other.x + other.width + gap)
        elif other.x >= box_x + box_w:
            right = min(right, other.x - gap)
    return min(left, box_x), max(right, box_x + box_w)


def apply(
    project: Project,
    layer: Layer,
    content: str,
    *,
    color: str | None = None,
    align: str | None = None,
    weight: str | None = None,
    font_size: int | None = None,
    erase: bool | None = None,
    rebuild: bool = True,
) -> list[str]:
    """Reescribe el texto de un elemento del arte conservando su sitio y su peso."""
    text = (content or "").strip()
    if not text:
        raise ArtTextError("El texto nuevo está vacío.")

    origin = layer.meta.get("art_text")
    if origin is None:
        # Una capa con varias piezas apiladas no se puede reescribir de una vez.
        # Medirla devuelve una sola línea del alto de las tres juntas, así que
        # el rótulo, el precio y el pie salían reemplazados por un único renglón
        # gigante: el bloque de 520x300 se convertía en uno de 880x264 a 288 px.
        # Antes esto pasaba sin avisar y parecía que el arte se borraba.
        piezas = blocks(project, layer)
        if len(piezas) > 1:
            raise ArtTextError(
                f"'{layer.name}' trae {len(piezas)} piezas apiladas (rótulo, precio, "
                "sello…). Reescribirlas de una vez las reemplaza por un solo renglón y "
                "se pierde el diseño. Pulse «Separar en "
                f"{len(piezas)} partes» y cambie solo la que necesita."
            )
        # La máscara describe dónde estaba el copy viejo y es lo que hay que
        # borrar del fondo: se fija antes de mover la caja al texto nuevo.
        layer_extraction.ensure_mask(project, layer, persist=True)
        style = measure(project, layer)
        if style is None:
            raise ArtTextError(
                f"'{layer.name}' no tiene texto reconocible en sus píxeles: no se puede "
                "reescribir. Cree una capa de texto encima en Revisar capas."
            )
        origin = {
            "src": layer.src,
            "content": layer.content or layer.meta.get("editable_content") or "",
            "box": [layer.x, layer.y, layer.width, layer.height],
            "type": layer.type.value,
            "style": style.as_dict(),
            "auto_contrast": layer.auto_contrast,
            "export_as_text": layer.export_as_text,
            "text_verified": layer.text_verified,
            "font_family": layer.font_family,
        }
    if not origin.get("style"):
        # Una capa que viene de juntar partes puede no traer estilo: el recorte
        # original no tenía texto medible aunque sus piezas sí. Se mide ahora,
        # sobre los píxeles que haya, sin perder el resto del original.
        medido = measure(project, layer)
        if medido is None:
            raise ArtTextError(
                f"'{layer.name}' no tiene texto reconocible en sus píxeles: no se puede "
                "reescribir de una vez. Sepárela en partes y reescriba cada una."
            )
        origin = {**origin, "style": medido.as_dict()}
    style = TextStyle(**origin["style"])

    align = align or style.align
    color = color or style.color
    # El texto completo, no su primera línea: el grosor del original se midió
    # sobre todas sus líneas y comparar una contra todas elegía mal la cara.
    weight, font_path, size, font = _choose_face(
        project, style, text, weight=weight, font_size=font_size
    )
    line_height = _line_height(font, style)
    block_w, block_h, ink_top, ink_h = _block_metrics(font, text, line_height)

    box_x, box_y, box_w, box_h = (int(value) for value in origin["box"])
    warnings: list[str] = []

    # El texto nuevo puede crecer, pero no dentro de su vecino: un precio más
    # largo se metía debajo del titular y el arte salía ilegible. Crece solo
    # hacia donde su alineación se lo permite; el borde anclado no se mueve.
    limit_left, limit_right = _free_span(project, layer, (box_x, box_y, box_w, box_h))
    if align == "left":
        room = limit_right - box_x
    elif align == "right":
        room = (box_x + box_w) - limit_left
    else:
        centre = box_x + box_w / 2
        room = int(2 * min(centre - limit_left, limit_right - centre))
    room = max(1, room)
    if block_w > room and not font_size:
        original_size = size
        while block_w > room and size > 8:
            size = max(8, int(size * 0.96))
            font = renderer.load_font(font_path, size)
            line_height = _line_height(font, style)
            block_w, block_h, ink_top, ink_h = _block_metrics(font, text, line_height)
        warnings.append(
            f"'{text[:28]}' no cabía en el hueco de '{layer.name}': se redujo de "
            f"{original_size} a {size} px para no pisar el elemento de al lado."
        )

    if align == "center":
        new_x = box_x + (box_w - block_w) // 2
    elif align == "right":
        new_x = box_x + box_w - block_w
    else:
        new_x = box_x
    # La tinta nueva empieza donde empezaba la vieja. La caja de la capa mide
    # esa tinta —no el bloque tipográfico—: un bloque mide ascendente más
    # descendente, bastante más alto que los trazos, y usarlo como caja hacía
    # que un precio reescrito invadiera al rótulo de arriba y al sello de abajo
    # sin dibujar nada sobre ellos. Cajas solapadas descuadran el reparto del
    # diseño anclado y el control de calidad. El renderer sube el bloque por su
    # `ink_top` para dejar los trazos en su sitio.

    if new_x < 0 or new_x + block_w > project.canvas.width:
        warnings.append(
            f"El texto nuevo de '{layer.name}' es más ancho que el hueco del original: "
            "se sale del arte. Acorte el texto o reduzca el cuerpo."
        )

    layer.meta["art_text"] = {
        **origin,
        "applied": text,
        "font_size": size,
        # Distancia del borde del bloque a la primera fila de tinta. Es lo que
        # traduce la caja de la capa a la caja de tinta que sirve de ancla.
        "ink_top": ink_top,
        # El bloque completo, para quien necesite saber cuánto se dibuja de
        # verdad por encima y por debajo de la tinta.
        "block_height": block_h,
    }
    layer.meta["mask_edited"] = True
    layer.type = LayerType.TEXT
    layer.content = text
    layer.color = color
    # Cerrados a propósito: la capa solo admite estos valores y hasta ahora se
    # le asignaba un `str` cualquiera, así que una alineación inventada llegaba
    # al renderer en vez de rebotar aquí.
    layer.text_align = align if align in ("left", "center", "right") else "center"
    layer.font_weight = "bold" if weight == "bold" else "normal"
    # La familia real del archivo, no la del valor por defecto: es el nombre que
    # el SVG le pide a Illustrator al abrirlo.
    layer.font_family = renderer.font_family_name(font_path) or layer.font_family
    layer.font_size = size
    layer.line_height = round(line_height, 3)
    # El color es el que eligió el diseñador sobre este mismo fondo: recalcularlo
    # por contraste cambiaría un precio de marca a blanco o negro sin motivo.
    layer.auto_contrast = False
    layer.export_as_text = True
    layer.text_verified = True
    # Escribir no devuelve al arte lo que se quitó a propósito: si no, un texto
    # por producto resucitaba el logo que el usuario había retirado.
    if not layer.meta.get("removed_from_art"):
        layer.visible = True
    layer.x = max(0, new_x)
    layer.y = max(0, box_y)
    layer.width = max(1, block_w)
    layer.height = max(1, ink_h)

    # Sin la tipografía de marca el texto nuevo sale con la del sistema, y eso
    # no es publicable. El aviso va aquí, en el momento del cambio, y no solo en
    # el recuadro del principio de la pantalla: ahí se pasa por alto.
    if not project.references.font:
        warnings.append(
            f"'{layer.name}' se reescribió con la tipografía del sistema: falta la de "
            "marca. Suba el .ttf/.otf en «Textos y logos del arte» y el texto se "
            "recalcula solo; hasta entonces el arte no está listo para producción."
        )

    warnings.extend(_sync_plate(project, layer, erase=erase, rebuild=rebuild))
    return warnings


def restore(project: Project, layer: Layer, *, rebuild: bool = True) -> list[str]:
    """Devuelve el elemento a sus píxeles originales."""
    origin = layer.meta.pop("art_text", None)
    if origin is None:
        return []
    box = [int(value) for value in origin["box"]]
    layer.type = LayerType(origin.get("type", LayerType.IMAGE.value))
    layer.src = origin.get("src") or layer.src
    layer.content = origin.get("content") or None
    layer.auto_contrast = bool(origin.get("auto_contrast", True))
    layer.export_as_text = bool(origin.get("export_as_text", False))
    layer.text_verified = bool(origin.get("text_verified", False))
    layer.font_family = origin.get("font_family") or layer.font_family
    layer.x, layer.y, layer.width, layer.height = box
    layer.meta.pop("erased_from_plate", None)
    # Lo que dejó el haber juntado partes: la máscara de la suma cubría más que
    # sus propios píxeles, y borraría de más si luego se quita del arte.
    if layer.meta.pop("merged_parts", None):
        layer.mask = origin.get("mask") or layer.mask
        previo = origin.get("editable_content")
        if previo:
            layer.meta["editable_content"] = previo
        else:
            layer.meta.pop("editable_content", None)
    return rebuild_plate(project) if rebuild else []


def set_removed(
    project: Project,
    layer: Layer,
    removed: bool,
    *,
    erase: bool | None = None,
    rebuild: bool = True,
) -> list[str]:
    """Quita el elemento del arte (o lo devuelve), borrándolo también del fondo."""
    layer.visible = not removed
    if removed:
        layer.meta["removed_from_art"] = True
    else:
        layer.meta.pop("removed_from_art", None)
    return _sync_plate(project, layer, erase=erase, rebuild=rebuild)


# --------------------------------------------------------------------- fondo
def _plate_path(project: Project) -> Path:
    return storage.abs_path(project.project_id, PLATE_REL)


def _source_path(project: Project) -> Path:
    return storage.abs_path(project.project_id, project.source.path)


def _plate_signature(project: Project) -> str:
    background = project.background
    return f"{background.provider or ''}|{background.generated_at or ''}"


def _baseline(project: Project) -> Path:
    """Plancha anterior a cualquier edición de arte, creándola si hace falta.

    Si otro servicio rehizo el fondo (detectar producto, reconstruir con IA) la
    copia se renueva: las ediciones se vuelven a aplicar sobre el fondo nuevo en
    vez de resucitar el fondo viejo.
    """
    path = storage.abs_path(project.project_id, PLATE_BASELINE_REL)
    signature = _plate_signature(project)
    if path.exists() and project.meta.get("art_plate_signature") == signature:
        return path

    origin = _plate_path(project) if project.background.path else _source_path(project)
    if not origin.exists():
        origin = _source_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, path)
    project.meta["art_plate_signature"] = signature
    return path


#: Última diferencia calculada, con la firma de los archivos que la produjeron.
#: Una tanda de catálogo pregunta lo mismo por cada producto y por cada texto, y
#: cada pregunta abría el arte y la plancha enteros.
_DIFFERENCE_CACHE: tuple[tuple, np.ndarray | None] | None = None


def _file_stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path), 0, 0)


def _plate_difference(project: Project) -> np.ndarray | None:
    """Diferencia por píxel entre el arte original y la plancha anterior a editar.

    Se calcula una vez y se reutiliza para todas las capas: el inventario de
    copy pregunta por cada elemento, y abrir las dos imágenes en cada pregunta
    eran 32 lecturas de un lienzo de 27 megapíxeles por pantalla.

    El resultado se guarda con la firma (ruta, fecha, tamaño) de los dos
    archivos: si alguno cambia —rehacer la plancha, volver a detectar el
    producto— la firma no coincide y se vuelve a medir.
    """
    global _DIFFERENCE_CACHE
    source = _source_path(project)
    # Contra la copia limpia: el fondo actual puede tener ya borrado este mismo
    # elemento, y entonces la comparación diría que nunca estuvo aplanado.
    baseline = storage.abs_path(project.project_id, PLATE_BASELINE_REL)
    plate = baseline if baseline.exists() else _plate_path(project)
    if not plate.exists() or not source.exists():
        return None  # sin plancha propia el fondo es el arte: los píxeles están

    stamp = (
        project.project_id,
        project.canvas.width,
        project.canvas.height,
        _file_stamp(source),
        _file_stamp(plate),
    )
    if _DIFFERENCE_CACHE is not None and _DIFFERENCE_CACHE[0] == stamp:
        return _DIFFERENCE_CACHE[1]

    with Image.open(source) as opened:
        original = np.asarray(opened.convert("RGB"), dtype=np.int16)
    with Image.open(plate) as opened:
        current = opened.convert("RGB")
        if current.size != (project.canvas.width, project.canvas.height):
            current = current.resize(
                (project.canvas.width, project.canvas.height), Image.Resampling.LANCZOS
            )
        background = np.asarray(current, dtype=np.int16)
    difference = (
        None
        if original.shape != background.shape
        else np.abs(original - background).mean(axis=2)
    )
    _DIFFERENCE_CACHE = (stamp, difference)
    return difference


def _in_plate(project: Project, layer: Layer, difference: np.ndarray | None) -> bool:
    if difference is None:
        return True
    mask = layer_extraction.ensure_mask(project, layer, persist=False) > 127
    if not mask.any() or mask.shape != difference.shape:
        return False
    return float(difference[mask].mean()) < PLATE_SAME_PIXELS


def pixels_in_plate(project: Project, layer: Layer) -> bool:
    """¿Los píxeles del elemento siguen dentro de la plancha de fondo?

    Cuando el KV llegó aplanado, el logo y el copy forman parte del fondo: se
    ocultan y siguen ahí. Cuando vienen del PSD como capas, el fondo no los
    contiene y no hay nada que borrar.
    """
    return _in_plate(project, layer, _plate_difference(project))


def plate_map(project: Project) -> dict[str, bool]:
    """Qué elementos siguen aplanados en el fondo, con una sola lectura del disco."""
    difference = _plate_difference(project)
    return {
        layer.id: _in_plate(project, layer, difference)
        for layer in project.layers
        if layer.category not in EXCLUDED_CATEGORIES
    }


def _sync_plate(
    project: Project, layer: Layer, *, erase: bool | None, rebuild: bool = True
) -> list[str]:
    """Marca o desmarca el elemento como borrado del fondo y rehace la plancha."""
    hidden = bool(layer.meta.get("removed_from_art")) or not layer.visible
    rewritten = bool(layer.meta.get("art_text"))
    if not (hidden or rewritten):
        if layer.meta.pop("erased_from_plate", None) and rebuild:
            return rebuild_plate(project)
        return []

    if layer.meta.get("erased_from_plate") and erase is None:
        needed = True  # ya se decidió antes; volver a medir leería el fondo ya limpio
    else:
        needed = pixels_in_plate(project, layer) if erase is None else bool(erase)
    if needed:
        layer.meta["erased_from_plate"] = True
    else:
        layer.meta.pop("erased_from_plate", None)
    return rebuild_plate(project) if rebuild else []


def rebuild_plate(project: Project) -> list[str]:
    """Rehace la plancha desde la copia limpia aplicando todos los borrados."""
    erased = [layer for layer in project.layers if layer.meta.get("erased_from_plate")]
    stored = storage.abs_path(project.project_id, PLATE_BASELINE_REL)
    if not erased and not stored.exists():
        # Nunca se borró nada de este KV: no hay plancha que rehacer ni copia
        # que guardar. Duplicar el fondo de un PSD de 94 MB "por si acaso" son
        # megas de disco por proyecto a cambio de nada.
        return []

    baseline = _baseline(project)
    target = _plate_path(project)
    target.parent.mkdir(parents=True, exist_ok=True)

    if not erased:
        shutil.copyfile(baseline, target)
        _register_plate(project)
        return []

    shape = (project.canvas.height, project.canvas.width)
    mask = np.zeros(shape, np.uint8)
    for layer in erased:
        layer_mask = layer_extraction.ensure_mask(project, layer, persist=False)
        mask = np.maximum(mask, (layer_mask > 127).astype(np.uint8) * 255)
    mask = dilate_mask(mask, 4)

    with Image.open(baseline) as opened:
        plate = opened.convert("RGB")
        if plate.size != (shape[1], shape[0]):
            plate = plate.resize((shape[1], shape[0]), Image.Resampling.LANCZOS)
        pixels = np.asarray(plate, dtype=np.uint8)

    radius = max(3, int(min(shape) * 0.01))
    bgr = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    filled = cv2.inpaint(bgr, mask, radius, cv2.INPAINT_TELEA)
    Image.fromarray(cv2.cvtColor(filled, cv2.COLOR_BGR2RGB)).save(target, format="PNG")
    _register_plate(project)

    names = ", ".join(f"'{layer.name}'" for layer in erased[:3])
    return [
        f"{len(erased)} elemento(s) estaban aplanados en el fondo ({names}): se borraron "
        "de la plancha para que no queden como fantasma."
    ]


def _register_plate(project: Project) -> None:
    """Deja la plancha editada como fondo del proyecto y refresca su firma.

    La firma se guarda **después** de registrar el fondo. Guardarla antes hacía
    que el propio registro la invalidara y que la copia limpia se renovara desde
    la plancha ya editada: el logo borrado reaparecía al siguiente cambio.
    """
    if project.background.path != PLATE_REL:
        project.background.path = PLATE_REL
        if not project.background.provider:
            project.background.provider = "art-edit"
    project.meta["art_plate_signature"] = _plate_signature(project)


# --------------------------------------------------------------------- partes
#: Una capa de copy de agencia trae varias piezas juntas: el rótulo "PRECIO
#: OFERTA", el precio, el precio anterior y un sello "EXCLUSIVO ONLINE", todo
#: en el mismo PNG aplanado. Reescribir eso como un solo texto no sirve de
#: nada: para cambiar el precio hay que poder tocar el precio y nada más.
#:
#: Se separa por lo que separa un elemento de otro a ojo: un hueco claro, un
#: cambio de cuerpo o un cambio de color. Y lo que sobrevive a los tres se mira
#: una vez más: solo sigue junto si es un párrafo —prosa que da la vuelta—, no
#: una lista de datos con el mismo cuerpo.
#: Un hueco no distingue una pieza de otra: en el arte real el rótulo, el
#: precio y el precio anterior van a 4-14 px unos de otros, más pegados que la
#: tilde de una Á. Lo que sí las distingue es la forma de la mancha de tinta.
#:
#: Una tilde, el punto de una i o el signo de una ñ ocupan poco ancho (una marca
#: sobre una letra), o lo reparten en marcas sueltas con mucho aire en medio
#: (una línea entera de mayúsculas acentuadas). Un rótulo, por corto que sea,
#: ocupa una parte seria del ancho del bloque y lo llena.
BLOCK_SPAN = 0.25
BLOCK_COVERAGE = 0.45
#: Y entre dos piezas de verdad, lo que las separa: color, cuerpo o un hueco
#: grande. Basta con una de las tres.
BLOCK_GAP = 0.9
BLOCK_SIZE_RATIO = 1.3
BLOCK_COLOR_DISTANCE = 70.0

#: Un filete —la barra de color de la marca, un subrayado— es tinta que no es
#: letra, y cruza las filas de varios renglones a la vez. La proyección de tinta
#: los ve entonces como una sola línea: el nombre del producto, su código y su
#: primera viñeta salían como **una** pieza de 112 px de alto, y reescribirla
#: los reemplazaba por un renglón único. También hacía saltar el corte de
#: centavos, que arrancaba la «L» de «476L».
#:
#: No se reconoce por su forma sola —una «1» grande también es alta y estrecha—
#: sino por lo que hace: se aparta y se vuelve a proyectar. Si su banda se abre
#: en dos renglones o más, era un filete; si no, era una letra.
RULE_HEIGHT = 2.0
RULE_ASPECT = 0.6
COMPONENT_MIN_AREA = 4

#: Dos renglones siguen juntos solo si son un párrafo de verdad: prosa que da la
#: vuelta al llegar al borde. Ahí todas las líneas menos la última llegan casi al
#: mismo sitio —lo que las corta es el ancho de la caja— y todas tienen el mismo
#: cuerpo. En una lista de datos el largo lo pone el contenido y va y viene, así
#: que cada renglón se edita por separado.
PARAGRAPH_FILL = 0.85
PARAGRAPH_HEIGHT = 1.15

#: Un bloque de precio llega del PSD como una mancha maciza: la caja crema, el
#: sello morado, el marco cian y el texto encima, todo junto. Ahí no hay papel
#: entre las letras —el papel es parte del dibujo— así que el bloque entero
#: contaba como una sola pieza, y reescribirlo borraba la caja y escribía el
#: precio nuevo del color de la caja: invisible.
#:
#: Lo que separa una plancha de una línea de texto es cómo se reparte su color:
#: la plancha es **una** mancha grande; el texto, muchas pequeñas. Solo se mira
#: en capas macizas, donde la tinta llena casi toda su propia caja; un texto
#: suelto sobre transparencia no pasa por aquí.
SOLID_FILL = 0.75
PLATE_SHARE = 0.05
PLATE_WHOLENESS = 0.6
PLATE_BUCKET = 32
#: Una plancha vale como pieza si es de verdad una: el hueco de un «9» y el hilo
#: de borde entre dos colores también son manchas suyas, y no son piezas.
PLATE_PIECE_SHARE = 0.01
#: Motas de uno o dos píxeles del borde suavizado. Cuentan como tinta y podían
#: abrir una pieza para nada.
INK_NOISE_AREA = 4
#: Un filete de un píxel de ancho es el borde entre dos colores, no una pieza.
RULE_MIN_SIDE = 3
RULE_MIN_AREA_PX = 40

#: Marca de las partes que son fondo —plancha o filete— y no texto.
PIECE_PLATE = "fondo"

#: Corte dentro de una misma línea. Un precio de retail lleva los centavos en
#: volado —más pequeños y más arriba— y el corte por bandas no los separa nunca,
#: porque van en la misma fila que los enteros. Reescribir el precio entero los
#: rebajaba al mismo cuerpo y el arte perdía su forma.
#:
#: La firma que se busca es específica a propósito: la pieza de la derecha tiene
#: que ser **más baja y acabar más arriba**. Una letra con rasgo descendente —la
#: p de «Compra»— hace justo lo contrario, crece hacia abajo, así que no dispara
#: el corte. Sin esa asimetría, cualquier palabra con p, q o g se partiría.
#: Alto del volado respecto al de los enteros: por debajo de esto es otra pieza.
CENTS_HEIGHT_RATIO = 0.80
#: Cuánto más arriba tiene que acabar, en proporción al alto de los enteros.
CENTS_RISE = 0.12
#: Columnas seguidas con la firma nueva para creérselo. Un corte de una columna
#: es ruido del antialias, no un cambio de cuerpo.
CENTS_MIN_RUN = 3
#: Ninguna de las dos partes puede quedar por debajo de esto (del ancho total).
CENTS_MIN_SHARE = 0.08
#: Ni el remate puede pasar de esto: más ancho que esto no es un volado.
CENTS_MAX_SHARE = 0.45


def _components(ink: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Etiquetas de las manchas de tinta y sus rectángulos."""
    _, labels, stats, _ = cv2.connectedComponentsWithStats(
        ink.astype(np.uint8), connectivity=8
    )
    return labels, stats


def _split_ink(ink: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Separa la tinta en letras y filetes.

    Un filete que cruza tres renglones los suelda en un solo tramo de tinta, y
    a partir de ahí todo sale mal: el bloque se mide como una línea del alto de
    las tres, reescribirlo las reemplaza por un renglón único y el corte de
    centavos dispara contra el final de la primera.

    La prueba no es la forma del trazo sino su efecto: se aparta el candidato,
    se vuelve a proyectar su banda y solo cuenta como filete si la banda se
    abre en dos renglones o más. Así una «1» de titular, que también es alta y
    estrecha, se queda donde está.
    """
    if not ink.any():
        return ink, np.zeros_like(ink)

    labels, stats = _components(ink)
    total = len(stats)
    if total <= 1:
        return ink, np.zeros_like(ink)

    cajas = {
        indice: (
            int(stats[indice, cv2.CC_STAT_LEFT]),
            int(stats[indice, cv2.CC_STAT_TOP]),
            int(stats[indice, cv2.CC_STAT_WIDTH]),
            int(stats[indice, cv2.CC_STAT_HEIGHT]),
        )
        for indice in range(1, total)
        if int(stats[indice, cv2.CC_STAT_AREA]) >= COMPONENT_MIN_AREA
    }
    if not cajas:
        return ink, np.zeros_like(ink)

    filetes = np.zeros(total, dtype=bool)
    for top, bottom in _ink_runs(_ink_rows(ink)):
        dentro = [
            indice
            for indice, (_, y, _, alto) in cajas.items()
            if y <= bottom and y + alto - 1 >= top
        ]
        if len(dentro) < 2:
            continue
        referencia = float(np.median([cajas[indice][3] for indice in dentro]))
        candidatos = [
            indice
            for indice in dentro
            if cajas[indice][3] >= referencia * RULE_HEIGHT
            and cajas[indice][2] <= cajas[indice][3] * RULE_ASPECT
        ]
        if not candidatos:
            continue
        # ¿Sueldan o no? Se quitan y se mira si la banda se abre en renglones.
        limpio = ink[top : bottom + 1].copy()
        for indice in candidatos:
            limpio &= ~(labels[top : bottom + 1] == indice)
        if len(_ink_runs(_ink_rows(limpio))) > 1:
            filetes[candidatos] = True

    if not filetes.any():
        return ink, np.zeros_like(ink)

    mancha = filetes[labels]
    letras = ink & ~mancha
    if not letras.any():
        return ink, np.zeros_like(ink)
    return letras, ink & mancha


def _plate_mask(rgb: np.ndarray, ink: np.ndarray) -> np.ndarray:
    """Las planchas de color de un bloque macizo. Vacío si la capa no lo es."""
    if float(ink.mean()) < SOLID_FILL:
        return np.zeros_like(ink)  # hay papel entre las letras: no es un bloque

    cubos = (rgb.astype(np.int32) // PLATE_BUCKET)
    llaves = cubos[:, :, 0] * 4096 + cubos[:, :, 1] * 64 + cubos[:, :, 2]
    total = int(ink.sum())
    colores: list[tuple[int, int, int]] = []
    valores, cuentas = np.unique(llaves[ink], return_counts=True)
    for valor, cuenta in zip(valores, cuentas):
        if cuenta < total * PLATE_SHARE:
            continue
        clase = ink & (llaves == valor)
        _, stats = _components(clase)
        if len(stats) <= 1:
            continue
        # Una mancha sola y grande es plancha; muchas pequeñas son letras.
        mayor = int(stats[1:, cv2.CC_STAT_AREA].max())
        if mayor < cuenta * PLATE_WHOLENESS:
            continue
        colores.append(_dominant_rgb(rgb[clase].reshape(-1, 3).astype(np.float32)))

    if not colores:
        return np.zeros_like(ink)

    # El color por sí solo no basta: el blanco de «12 CUOTAS» está a menos de
    # 60 del crema de la caja, así que por color entraba en la plancha y el
    # rótulo se quedaba pegado al fondo. De cada color se queda solo su mancha
    # grande; sus letras sueltas, del color que sean, siguen siendo texto.
    minimo = ink.size * PLATE_PIECE_SHARE
    plancha = np.zeros_like(ink)
    for color in colores:
        distancia = np.linalg.norm(
            rgb.astype(np.float32) - np.asarray(color, np.float32), axis=2
        )
        cerca = ink & (distancia <= INK_COLOR_DISTANCE)
        labels, stats = _components(cerca)
        grandes = stats[:, cv2.CC_STAT_AREA] >= minimo
        grandes[0] = False  # la etiqueta 0 es el fondo, no una mancha
        plancha |= cerca & grandes[labels]
    return _absorb_edges(plancha, ink)


def _absorb_edges(plancha: np.ndarray, ink: np.ndarray) -> np.ndarray:
    """Devuelve la plancha con sus bordes suavizados dentro.

    Entre el marco y la caja queda un hilo de un píxel que no es ninguno de los
    dos colores. Suelto, abría una pieza de 329x1 en la lista de textos. Es
    borde de plancha: va con ella.
    """
    if not plancha.any():
        return plancha
    resto = ink & ~plancha
    if not resto.any():
        return plancha
    vecino = dilate_mask(plancha.astype(np.uint8) * 255, 1) > 0
    labels, stats = _components(resto)
    lados = np.minimum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT])
    finas = lados < RULE_MIN_SIDE
    finas[0] = False
    borde = np.zeros(len(stats), dtype=bool)
    for indice in np.nonzero(finas)[0]:
        borde[indice] = bool((vecino & (labels == indice)).any())
    return plancha | (resto & borde[labels])


def _ink_and_plates(rgb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(tinta, planchas) de un recorte.

    En un parche macizo el fondo no es un color suelto: es la caja, su marco y
    su sello. `_ink_mask` toma por fondo el color más repetido, y en un recorte
    ajustado de un precio en negrita el color más repetido es **el número**: la
    máscara salía al revés y el precio nuevo se escribía del color de la caja,
    invisible. Aquí el fondo se reconoce por mancha —una grande y compacta— y
    lo que queda encima es el texto.

    Un recorte normal de PSD no pasa por aquí: su fondo es la transparencia.
    """
    solid = alpha >= INK_ALPHA
    plancha = _plate_mask(rgb, solid)
    if plancha.any():
        tinta = solid & ~plancha
        if tinta.any():
            return tinta, plancha
    return _ink_mask(rgb, alpha), np.zeros_like(solid)


def _plate_boxes(plancha: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Un rectángulo por plancha. Van primeras: son el fondo de lo demás."""
    minimo = plancha.size * PLATE_PIECE_SHARE
    return [
        (left, top, width, height)
        for left, top, width, height in _blob_boxes(plancha)
        if width * height >= minimo
    ]


def _denoise(ink: np.ndarray) -> np.ndarray:
    """La tinta sin las motas del borde suavizado."""
    if not ink.any():
        return ink
    labels, stats = _components(ink)
    grandes = stats[:, cv2.CC_STAT_AREA] > INK_NOISE_AREA
    grandes[0] = False  # la etiqueta 0 es el fondo, no una mancha
    return ink & grandes[labels]


def _blob_boxes(mancha: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Un rectángulo por mancha."""
    if not mancha.any():
        return []
    _, stats = _components(mancha)
    return [
        (
            int(stats[indice, cv2.CC_STAT_LEFT]),
            int(stats[indice, cv2.CC_STAT_TOP]),
            int(stats[indice, cv2.CC_STAT_WIDTH]),
            int(stats[indice, cv2.CC_STAT_HEIGHT]),
        )
        for indice in range(1, len(stats))
    ]


def _rule_boxes(filetes: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Un rectángulo por filete: cada uno es una pieza suya, intacta.

    Se descarta el hilo de un píxel que queda entre dos colores: es el borde
    suavizado de la plancha, no un filete del diseño.
    """
    return [
        (left, top, width, height)
        for left, top, width, height in _blob_boxes(filetes)
        if min(width, height) >= RULE_MIN_SIDE and width * height >= RULE_MIN_AREA_PX
    ]


@dataclass(frozen=True)
class _Run:
    """Un tramo de tinta con lo que hace falta para saber qué es."""

    top: int
    bottom: int
    left: int
    right: int
    coverage: float
    color: tuple[int, int, int]

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    @property
    def span(self) -> int:
        return self.right - self.left + 1


def _profile(rgb: np.ndarray, ink: np.ndarray) -> list[_Run]:
    """Cada tramo con su ancho, su relleno y su color.

    Los tramos van crudos a propósito. Unirlos aquí con `_line_runs` toma como
    referencia el renglón más alto de toda la capa, y en una ficha de producto
    —nombre grande, código pequeño, viñetas— eso hace pasar por tildes del
    grande a los renglones pequeños y los funde todos en uno. Quien junta la
    tilde con su letra es `_group_runs`, que mide el ancho y no el alto.
    """
    profiled: list[_Run] = []
    for top, bottom in _ink_runs(_ink_rows(ink)):
        band = ink[top : bottom + 1]
        columns = np.nonzero(band.any(axis=0))[0]
        if not columns.size:
            continue
        left, right = int(columns[0]), int(columns[-1])
        pixels = rgb[top : bottom + 1][band]
        profiled.append(
            _Run(
                top=top,
                bottom=bottom,
                left=left,
                right=right,
                coverage=columns.size / max(1, right - left + 1),
                color=_dominant_rgb(pixels.reshape(-1, 3).astype(np.float32)),
            )
        )
    return profiled


def _is_fragment(run: _Run, width: int) -> bool:
    """Marca suelta que pertenece a la línea de al lado, no una pieza propia."""
    return run.span < width * BLOCK_SPAN or run.coverage < BLOCK_COVERAGE


def _apart(previous: _Run, run: _Run) -> bool:
    """¿Son dos piezas distintas? Basta con que cambie una de las tres cosas."""
    tall = max(previous.height, run.height)
    short = max(1, min(previous.height, run.height))
    gap = run.top - previous.bottom - 1
    distance = float(
        np.linalg.norm(
            np.asarray(previous.color, np.float32) - np.asarray(run.color, np.float32)
        )
    )
    return (
        gap > tall * BLOCK_GAP
        or tall / short > BLOCK_SIZE_RATIO
        or distance > BLOCK_COLOR_DISTANCE
    )


def _group_runs(rgb: np.ndarray, ink: np.ndarray) -> list[list[_Run]]:
    """Agrupa tramos en piezas. Cada grupo es un elemento editable por separado."""
    runs = _profile(rgb, ink)
    if not runs:
        return []
    width = ink.shape[1]
    groups: list[list[_Run]] = []
    waiting: list[_Run] = []  # fragmentos esperando a la línea de abajo

    for index, run in enumerate(runs):
        if _is_fragment(run, width):
            # La tilde va encima de su letra, así que lo normal es que espere a
            # la línea siguiente; solo se queda con la anterior si está más
            # pegada a ella que a la que viene.
            following = runs[index + 1] if index + 1 < len(runs) else None
            with_previous = bool(groups) and (
                following is None
                or (run.top - groups[-1][-1].bottom) <= (following.top - run.bottom)
            )
            if with_previous:
                groups[-1].append(run)
            else:
                waiting.append(run)
            continue

        block = waiting + [run]
        waiting = []
        if groups and not _apart(groups[-1][-1], run):
            groups[-1].extend(block)
        else:
            groups.append(block)

    if waiting:
        if groups:
            groups[-1].extend(waiting)
        else:
            groups.append(waiting)
    return groups


def _raised_cut(ink: np.ndarray, box: tuple[int, int, int, int]) -> int | None:
    """Columna donde una línea remata en un cuerpo más pequeño y más alto.

    Devuelve la coordenada absoluta del corte, o ``None`` si la línea es
    homogénea. Solo mira el perfil de tinta: no hace falta saber que son
    centavos, basta con que el **remate** sea más bajo y acabe más arriba.

    Se busca desde la derecha y contra la firma dominante de la línea, no
    contra su primer tercio. Un «$» es más alto que sus dígitos, así que
    tomarlo como referencia partía el precio delante del primer número.
    """
    left, top, width, height = box
    if width < 24 or height < 16:
        return None  # demasiado pequeña: cualquier medida aquí es ruido
    banda = ink[top : top + height, left : left + width]
    # Un volado es cosa de **una** línea. En un párrafo de dos, la primera suele
    # ser más larga, y su cola —columnas con tinta solo arriba— es más baja y
    # acaba más arriba que la mediana de las dos: clavada la firma del volado.
    # Así se partía en dos un legal de dos renglones.
    if len(_line_runs(_ink_rows(banda))) != 1:
        return None

    columnas = np.nonzero(banda.any(axis=0))[0]
    if columnas.size < CENTS_MIN_RUN * 2:
        return None

    filas = [np.nonzero(banda[:, x])[0] for x in columnas]
    bottoms = np.array([int(f[-1]) for f in filas], dtype=np.int32)
    alturas = np.array([int(f[-1] - f[0] + 1) for f in filas], dtype=np.int32)

    # La referencia es el trazo **alto** de la línea, no su mediana: en un
    # precio los centavos ocupan casi tantas columnas como los enteros, así que
    # la mediana cae entre los dos cuerpos y ninguno parece desviarse. El
    # percentil alto se queda con los enteros midan lo que midan.
    base_alto = float(np.percentile(alturas, 85))
    if base_alto <= 0:
        return None
    grandes = alturas >= base_alto * 0.85
    if not grandes.any():
        return None
    base_bottom = float(np.median(bottoms[grandes]))

    def volado(indice: int) -> bool:
        return (
            base_bottom - bottoms[indice] >= base_alto * CENTS_RISE
            and alturas[indice] <= base_alto * CENTS_HEIGHT_RATIO
        )

    # Cuántas columnas del final cumplen la firma del volado, sin interrupción.
    cola = 0
    for indice in range(columnas.size - 1, -1, -1):
        if not volado(indice):
            break
        cola += 1
    if cola < CENTS_MIN_RUN:
        return None

    minimo = max(CENTS_MIN_RUN, int(columnas.size * CENTS_MIN_SHARE))
    # Ni un remate ínfimo ni medio renglón: un volado son los centavos, no la
    # mitad del precio.
    if cola < minimo or columnas.size - cola < minimo:
        return None
    if cola > columnas.size * CENTS_MAX_SHARE:
        return None

    # El corte no cae donde empieza a cumplirse la firma: el borde suavizado del
    # último trazo grande ya la cumple —mide poco— y cortar ahí le arrancaba una
    # esquina al dígito, que reaparecía pegada a los centavos. Se lleva al hueco
    # en blanco que separa los dos trazos.
    inicio = columnas.size - cola
    ultimo_grande = 0
    for indice in range(inicio - 1, -1, -1):
        if alturas[indice] >= base_alto * 0.85:
            ultimo_grande = indice
            break

    # El hueco de verdad está a la **derecha** de donde empieza a cumplirse la
    # firma: el último trazo grande remata en una cola corta —la curva de un 3,
    # el pie de un 4— que ya la cumple. Buscar el corte solo hasta ahí lo dejaba
    # dentro del dígito y le arrancaba una esquina, que reaparecía pegada a los
    # centavos. Se busca el primer blanco real pasado ese trazo.
    minimo_hueco = max(2, int(base_alto * 0.04))
    for indice in range(ultimo_grande + 1, columnas.size):
        salto = int(columnas[indice]) - int(columnas[indice - 1])
        if salto >= minimo_hueco:
            return left + int(columnas[indice - 1]) + salto // 2 + 1
    return left + int(columnas[inicio])


def _split_raised(
    ink: np.ndarray, box: tuple[int, int, int, int]
) -> list[tuple[int, int, int, int]]:
    """La caja, partida en el cambio de cuerpo si lo hay."""
    corte = _raised_cut(ink, box)
    if corte is None:
        return [box]
    left, top, width, height = box
    izquierda = (left, top, corte - left, height)
    derecha = (corte, top, left + width - corte, height)
    # Cada mitad se recorta a su tinta: la caja de la banda es la de las dos.
    return [caja for caja in (_tight(ink, izquierda), _tight(ink, derecha)) if caja]


def _tight(
    ink: np.ndarray, box: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    """La caja ajustada a la tinta que contiene."""
    left, top, width, height = box
    recorte = ink[top : top + height, left : left + width]
    filas = np.nonzero(recorte.any(axis=1))[0]
    columnas = np.nonzero(recorte.any(axis=0))[0]
    if not filas.size or not columnas.size:
        return None
    return (
        left + int(columnas[0]),
        top + int(filas[0]),
        int(columnas[-1] - columnas[0] + 1),
        int(filas[-1] - filas[0] + 1),
    )


def _is_paragraph(lines: list[_Run]) -> bool:
    """¿Estos renglones son prosa que da la vuelta, o datos sueltos?

    En un párrafo lo que corta cada línea es el ancho de la caja, así que todas
    menos la última llegan casi al mismo borde y todas van al mismo cuerpo. En
    una ficha —nombre, código, viñetas— el largo lo pone el contenido: una
    línea corta en medio de dos largas delata que no es un párrafo.
    """
    if len(lines) < 2:
        return True
    altos = [run.height for run in lines]
    if max(altos) > min(altos) * PARAGRAPH_HEIGHT:
        return False
    anchos = [run.span for run in lines]
    ancho = max(anchos)
    return all(span >= ancho * PARAGRAPH_FILL for span in anchos[:-1])


def _as_lines(group: list[_Run], width: int) -> list[list[_Run]]:
    """El grupo repartido en renglones; cada marca suelta con la que le toca.

    Una tilde va encima de su letra y un punto de exclamación invertido debajo,
    así que la marca no se queda siempre con el renglón anterior: se mide a cuál
    de los dos está más pegada.
    """
    completos = [
        indice for indice, run in enumerate(group) if not _is_fragment(run, width)
    ]
    if not completos:
        return [group]
    renglones: list[list[_Run]] = [[] for _ in completos]
    for indice, run in enumerate(group):
        if indice in completos:
            renglones[completos.index(indice)].append(run)
            continue
        cercano = min(
            range(len(completos)),
            key=lambda cual: _distance(run, group[completos[cual]]),
        )
        renglones[cercano].append(run)
    return [sorted(renglon, key=lambda run: run.top) for renglon in renglones]


def _distance(run: _Run, other: _Run) -> int:
    """Filas que separan dos tramos; 0 si se solapan."""
    return max(0, other.top - run.bottom, run.top - other.bottom)


def _pieces(rgb: np.ndarray, ink: np.ndarray) -> list[list[_Run]]:
    """Las piezas editables: un párrafo entero, o un renglón cada una."""
    width = ink.shape[1]
    pieces: list[list[_Run]] = []
    for group in _group_runs(rgb, ink):
        completos = [run for run in group if not _is_fragment(run, width)]
        if _is_paragraph(completos):
            pieces.append(group)
        else:
            pieces.extend(_as_lines(group, width))
    return pieces


def _inside(box: tuple[int, int, int, int], other: tuple[int, int, int, int]) -> bool:
    """¿`box` cabe entera dentro de `other`?"""
    left, top, width, height = box
    o_left, o_top, o_width, o_height = other
    return (
        left >= o_left
        and top >= o_top
        and left + width <= o_left + o_width
        and top + height <= o_top + o_height
    )


def _layer_pieces(
    rgb: np.ndarray, alpha: np.ndarray
) -> tuple[list[tuple[int, int, int, int]], list[tuple[int, int, int, int]], np.ndarray]:
    """(fondos, textos, máscara de plancha) del PNG de una capa."""
    tinta, plancha = _ink_and_plates(rgb, alpha)
    letras, filetes = _split_ink(_denoise(tinta))
    fondos = [*_plate_boxes(plancha), *_rule_boxes(filetes)]
    boxes: list[tuple[int, int, int, int]] = []
    for group in _pieces(rgb, letras):
        top = min(run.top for run in group)
        bottom = max(run.bottom for run in group)
        left = min(run.left for run in group)
        right = max(run.right for run in group)
        boxes.extend(
            _split_raised(letras, (left, top, right - left + 1, bottom - top + 1))
        )
    # Un fondo que cabe dentro de una pieza de texto no es una pieza: el recorte
    # del texto es un rectángulo del arte y ya se lo lleva puesto. En un recorte
    # ajustado de «$43» el crema queda partido en cuatro trozos por los dígitos,
    # y sin esto el precio salía separado en cinco piezas que no existen.
    fondos = [
        caja for caja in fondos if not any(_inside(caja, texto) for texto in boxes)
    ]
    # Las planchas primero: las partes se dibujan en el orden de esta lista y el
    # fondo tiene que quedar debajo de su propio texto.
    return (
        sorted(fondos, key=lambda box: (box[1], box[0])),
        sorted(boxes, key=lambda box: (box[1], box[0])),
        plancha,
    )


def _block_boxes(rgb: np.ndarray, alpha: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Rectángulos de cada pieza, en coordenadas del PNG de la capa."""
    fondos, textos, _ = _layer_pieces(rgb, alpha)
    return [*fondos, *textos]


def _plate_only(art: Image.Image, rgb: np.ndarray, texto: np.ndarray) -> Image.Image:
    """El bloque con su texto borrado, para que sirva de fondo de sus partes.

    El recorte de una plancha es un rectángulo del arte, así que se lleva el
    texto dentro. Sin borrarlo, al cambiar el precio el viejo seguía asomando
    por debajo del nuevo. Se rellena con el color que lo rodea, que en una
    plancha lisa es su propio color.
    """
    hueco = dilate_mask(texto.astype(np.uint8) * 255, 2)
    plano = cv2.inpaint(
        np.ascontiguousarray(rgb), (hueco > 0).astype(np.uint8), 3, cv2.INPAINT_TELEA
    )
    limpio = Image.fromarray(plano, mode="RGB").convert("RGBA")
    limpio.putalpha(art.getchannel("A"))
    return limpio


def blocks(project: Project, layer: Layer) -> list[tuple[int, int, int, int]]:
    """Piezas que contiene el elemento. Una sola significa que no hay que separar."""
    path = _layer_png(project, layer)
    if path is None:
        return []
    with Image.open(path) as opened:
        art = opened.convert("RGBA")
        rgb = np.asarray(art.convert("RGB"), dtype=np.uint8)
        alpha = np.asarray(art.getchannel("A"), dtype=np.uint8)
    if not (alpha >= INK_ALPHA).any():
        return []
    return _block_boxes(rgb, alpha)


def split(project: Project, layer: Layer) -> tuple[list[Layer], list[str]]:
    """Convierte una capa con varias piezas en una capa por pieza.

    Cada parte pasa a ser un elemento normal del arte: se mide, se reescribe,
    se quita y se exporta como cualquier otro. Así "cambiar solo el precio" no
    necesita nada nuevo, porque el precio ya es un elemento.
    """
    if layer.meta.get("art_text", {}).get("applied"):
        raise ArtTextError(
            f"'{layer.name}' ya está reescrita. Devuélvala al original antes de separarla."
        )
    if layer.meta.get("split_into"):
        raise ArtTextError(f"'{layer.name}' ya está separada en partes.")

    path = _layer_png(project, layer)
    if path is None:
        raise ArtTextError(f"'{layer.name}' no tiene píxeles que separar.")
    boxes = blocks(project, layer)
    if len(boxes) < 2:
        raise ArtTextError(
            f"'{layer.name}' es una sola pieza: no hay nada que separar. "
            "Reescríbala entera desde su casilla de texto."
        )

    with Image.open(path) as opened:
        art = opened.convert("RGBA")
        rgb = np.asarray(art.convert("RGB"), dtype=np.uint8)
        alpha = np.asarray(art.getchannel("A"), dtype=np.uint8)
        fondos, _, plancha = _layer_pieces(rgb, alpha)
        # Un fondo se recorta del arte sin su texto; el texto ya viaja en su
        # propia parte y quedaba repetido debajo al reescribirlo.
        limpio = (
            _plate_only(art, rgb, (alpha >= INK_ALPHA) & ~plancha)
            if plancha.any()
            else art
        )
        cuantos_fondos = len(fondos)
        parts: list[Layer] = []
        for index, (left, top, width, height) in enumerate(boxes):
            fondo = index < cuantos_fondos
            fuente = limpio if fondo else art
            crop = fuente.crop((left, top, left + width, top + height))
            relative = f"layers/{layer.id[:8]}_parte{index + 1}.png"
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG", optimize=True)
            storage.write_bytes(project.project_id, relative, buffer.getvalue())
            parts.append(
                Layer(
                    name=(
                        f"{layer.name} · fondo"
                        if fondo
                        else f"{layer.name} · parte {index + 1 - cuantos_fondos}"
                    ),
                    type=LayerType.IMAGE,
                    category=layer.category,
                    src=relative,
                    x=layer.x + left,
                    y=layer.y + top,
                    width=width,
                    height=height,
                    z_index=layer.z_index,
                    visible=layer.visible,
                    locked=layer.locked,
                    movable=layer.movable,
                    resizable=layer.resizable,
                    reorderable=layer.reorderable,
                    replaceable=layer.replaceable,
                    preserve_aspect_ratio=layer.preserve_aspect_ratio,
                    source=layer.source,
                    meta={
                        "split_from": layer.id,
                        "split_index": index,
                        **({"art_piece": PIECE_PLATE} if fondo else {}),
                    },
                )
            )

    # La capa madre sale del arte pero no se pierde: se guarda entera para poder
    # deshacer. Guardarla fuera de `layers` evita que el resto del programa
    # tenga que aprender a ignorarla en cada recorrido.
    stash = dict(project.meta.get("split_layers") or {})
    stash[layer.id] = layer.model_dump(mode="json")
    project.meta["split_layers"] = stash

    position = project.layers.index(layer)
    project.layers[position : position + 1] = parts
    for part in parts:
        part.meta["split_into"] = [item.id for item in parts]

    return parts, [
        f"'{layer.name}' se separó en {len(parts)} partes: ahora cada una se "
        "reescribe o se quita por su cuenta."
    ]


def _part_touched(part: Layer) -> bool:
    """¿Se le hizo algo a esta parte que no se pueda perder al juntarlas?"""
    if (part.meta.get("art_text") or {}).get("applied"):
        return True
    return bool(part.meta.get("removed_from_art")) or not part.visible


def _merge_box(parent: Layer, visible: list[Layer]) -> tuple[int, int, int, int]:
    """La caja de la madre más lo que las partes hayan crecido al reescribirse.

    Se parte de la caja original y no solo de las partes: si una se quitó, el
    hueco que ocupaba sigue formando parte del elemento, y encogerse hasta las
    que quedan movería el resto del arte.
    """
    left = min([parent.x] + [item.x for item in visible])
    top = min([parent.y] + [item.y for item in visible])
    right = max([parent.x + parent.width] + [item.x + item.width for item in visible])
    bottom = max([parent.y + parent.height] + [item.y + item.height for item in visible])
    return left, top, max(1, right - left), max(1, bottom - top)


def _draw_part(
    canvas: Image.Image, project: Project, part: Layer, corner: tuple[int, int]
) -> list[str]:
    """Pinta una parte en el lienzo del conjunto, con lo que dice hoy."""
    offset_x, offset_y = corner
    if part.type is LayerType.TEXT:
        # Reescrita: sus píxeles ya no son el recorte, son el texto nuevo. Lo
        # dibuja el mismo motor que compone las variantes, así que lo que se
        # aplana aquí es exactamente lo que se iba a exportar.
        placement = Placement(
            layer=part,
            x=part.x - offset_x,
            y=part.y - offset_y,
            width=max(1, part.width),
            height=max(1, part.height),
            z_index=part.z_index,
            align=part.text_align,
            valign="top",
            pinned=True,
            font_size=part.font_size,
        )
        _, warnings = renderer.draw_text_layer(canvas, placement, project)
        return warnings

    path = _layer_png(project, part)
    if path is None:
        return [f"'{part.name}' no tenía PNG que juntar: su hueco quedó vacío."]
    with Image.open(path) as opened:
        crop = opened.convert("RGBA")
        canvas.paste(crop, (part.x - offset_x, part.y - offset_y), crop)
    return []


def _merge_parts(project: Project, parent: Layer, children: list[Layer]) -> list[str]:
    """Aplana el estado actual de las partes sobre la capa madre.

    Volver a unir no puede deshacer lo que se escribió en las partes: quien
    separa una capa para cambiar solo el precio y luego la junta espera
    encontrarse el precio nuevo, no el viejo. Así que la madre no vuelve tal
    como estaba, vuelve con lo que las partes dicen hoy —y el original se
    guarda en `art_text` para que «Volver al original» siga funcionando—.
    """
    visible = [
        item for item in children
        if item.visible and not item.meta.get("removed_from_art")
    ]
    # El estilo se mide sobre el recorte original, que aún está en disco: es lo
    # que necesita `apply` si después se reescribe la capa entera de una vez.
    style = measure(project, parent)
    origin = {
        "src": parent.src,
        "content": parent.content or parent.meta.get("editable_content") or "",
        "box": [parent.x, parent.y, parent.width, parent.height],
        "type": parent.type.value,
        "auto_contrast": parent.auto_contrast,
        "export_as_text": parent.export_as_text,
        "text_verified": parent.text_verified,
        "font_family": parent.font_family,
        "mask": parent.mask,
        "editable_content": parent.meta.get("editable_content"),
    }
    if style is not None:
        origin["style"] = style.as_dict()

    if not visible:
        # No quedó nada que pintar: el elemento entero se había quitado del arte.
        parent.meta["art_text"] = {**origin, "applied": ""}
        parent.meta["merged_parts"] = True
        parent.meta["removed_from_art"] = True
        parent.visible = False
        return _sync_plate(project, parent, erase=None)

    left, top, width, height = _merge_box(parent, visible)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    warnings: list[str] = []
    for part in sorted(visible, key=lambda item: int(item.meta.get("split_index", 0))):
        warnings.extend(_draw_part(canvas, project, part, (left, top)))

    relative = f"layers/{parent.id[:8]}_unido.png"
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    storage.write_bytes(project.project_id, relative, buffer.getvalue())

    # La madre queda como una imagen con los píxeles ya compuestos: el texto
    # nuevo está dentro, no se vuelve a dibujar.
    parent.type = LayerType.IMAGE
    parent.src = relative
    parent.x, parent.y, parent.width, parent.height = left, top, width, height
    parent.content = None
    parent.export_as_text = False
    parent.auto_contrast = False
    textos = [current_text(part) for part in visible]
    parent.meta["art_text"] = {**origin, "applied": " ".join(t for t in textos if t)}
    parent.meta["editable_content"] = " ".join(t for t in textos if t)
    parent.meta["merged_parts"] = True
    parent.meta["mask_edited"] = True

    # La plancha tiene que seguir sin la tinta vieja de las partes reescritas, y
    # la máscara de la madre sola no la cubre: la nueva es la suma de todas.
    shape = (project.canvas.height, project.canvas.width)
    union = np.zeros(shape, np.uint8)
    for capa in [parent, *children]:
        capa_mask = layer_extraction.ensure_mask(project, capa, persist=False)
        if capa_mask.shape[:2] == shape:
            union = np.maximum(union, capa_mask)
    if union.any():
        layer_extraction.write_mask(project, parent, union)

    erased = any(part.meta.get("erased_from_plate") for part in children)
    if erased:
        parent.meta["erased_from_plate"] = True
    else:
        parent.meta.pop("erased_from_plate", None)
    warnings.extend(rebuild_plate(project))
    return warnings


def unsplit(project: Project, parent_id: str) -> list[str]:
    """Vuelve a juntar las partes en la capa original."""
    stash = dict(project.meta.get("split_layers") or {})
    stored = stash.get(parent_id)
    if stored is None:
        raise ArtTextError("Esa capa no está separada en partes.")

    parent = Layer(**stored)
    children = [item for item in project.layers if item.meta.get("split_from") == parent_id]
    if not children:
        raise ArtTextError(f"No quedan partes de '{parent.name}' que juntar.")

    position = min(project.layers.index(item) for item in children)
    project.layers = [item for item in project.layers if item.meta.get("split_from") != parent_id]
    project.layers.insert(position, parent)
    del stash[parent_id]
    project.meta["split_layers"] = stash

    # Sin nada escrito ni quitado, juntarlas es deshacer la separación: la madre
    # vuelve tal cual y no se aplana ni se rehace nada.
    touched = [item for item in children if _part_touched(item)]
    if not touched:
        return [f"Las partes volvieron a ser '{parent.name}'."]

    warnings = _merge_parts(project, parent, children)
    return [
        f"Las partes volvieron a ser '{parent.name}', conservando lo que se cambió "
        f"en {len(touched)} de ellas.",
        *warnings,
    ]


# ------------------------------------------------------------------ inventario
def editable_layers(project: Project) -> list[Layer]:
    """Copy, logos y decoraciones: todo lo que el usuario puede reescribir o quitar."""
    return sorted(
        (layer for layer in project.layers if layer.category not in EXCLUDED_CATEGORIES),
        key=lambda layer: layer.z_index,
    )


def current_text(layer: Layer) -> str:
    """Lo que dice hoy el elemento, venga de una capa de texto o de un PSD."""
    origin = layer.meta.get("art_text") or {}
    if origin.get("applied"):
        return str(origin["applied"])
    return str(layer.content or layer.meta.get("editable_content") or "").strip()


def remeasure_all(project: Project) -> list[str]:
    """Vuelve a escribir el copy ya reescrito con la tipografía activa.

    El cuerpo y la caja de un texto se calculan contra las métricas de la cara
    con la que se escribió. Al subir la tipografía de marca después, esos
    números dejan de valer: hay que rehacerlos desde el estilo del original, que
    es lo que se guardó y no cambia.
    """
    warnings: list[str] = []
    for layer in project.layers:
        applied = (layer.meta.get("art_text") or {}).get("applied")
        if not applied:
            continue
        try:
            warnings.extend(apply(project, layer, applied, rebuild=False))
        except ArtTextError as exc:
            warnings.append(str(exc))
    return warnings


def apply_batch(project: Project, overrides: dict[str, str]) -> list[str]:
    """Aplica el juego completo de textos de una tanda.

    Es un reemplazo total, no un parche: lo que se reescribió para el producto
    anterior y no viene en esta tanda vuelve a su original. Sin eso, el precio
    de la primera lavadora se quedaría pegado en todos los artes siguientes.
    """
    warnings: list[str] = []
    wanted = {key: value for key, value in overrides.items() if (value or "").strip()}
    touched = False
    for layer in project.layers:
        if layer.meta.get("art_text") and layer.id not in wanted:
            restore(project, layer, rebuild=False)
            touched = True
    for layer_id, content in wanted.items():
        layer = project.layer_by_id(layer_id)
        if layer is None:
            warnings.append(f"Se ignoró un texto para una capa inexistente: {layer_id}.")
            continue
        if current_text(layer) == content.strip():
            continue
        try:
            warnings.extend(apply(project, layer, content, rebuild=False))
            touched = True
        except ArtTextError as exc:
            warnings.append(str(exc))
    if touched:
        # Una sola reconstrucción por tanda: rehacerla capa a capa repetía un
        # inpainting de lienzo completo por cada precio cambiado.
        warnings.extend(rebuild_plate(project))
    return warnings
