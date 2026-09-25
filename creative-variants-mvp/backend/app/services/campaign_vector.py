"""Plantillas a partir del editable vectorial: el `.ai` o el PDF con capas.

El `.ai` se trataba como una foto: se rasterizaba la página, el OCR adivinaba
dónde había texto, el inpainting rellenaba y encima se dibujaba la retícula.
Con un desplegable de doce productos el resultado era el catálogo entero con
"TITULAR DE CAMPAÑA" pegado encima: no era una plantilla.

Pero el editable ya dice lo que el OCR adivina. Un PDF de Illustrator conserva
cada imagen colocada con su caja y su canal alfa, y cada texto con su fuente,
su cuerpo, su color y su posición exacta. Con eso:

* **Piezas.** Una página de arte es una pieza. En un toolkit —una presentación
  que enseña los KV dentro de diapositivas— cada KV es una imagen opaca
  enmarcada dentro de la lámina, y la pieza es ese marco.
* **Fijo frente a variable.** Lo que se repite entre piezas es identidad: el
  logo de campaña, el sello, la tipografía de "CRÉDITO DIRECTO". Lo que solo
  está en una pieza, o lleva cifras, es lo que cada fila de la matriz cambia:
  el producto recortado, su nombre, el precio y la cuota.
* **Placa.** Se renderiza la pieza desde el propio PDF con lo variable quitado
  —el producto sustituido por transparente y el texto redactado sin tocar
  vectores ni imágenes—. No hay inpainting: el fondo que queda es el que dibujó
  el diseñador, con la pastilla del precio vacía esperando su cifra.
* **Posiciones.** Las cajas de lo quitado son los huecos, con su color real.

Una página con muchos productos es un catálogo, no una pieza: de ella no sale
placa, porque una placa con doce huecos de producto no es una plantilla para
redes y la alternativa —dejar el catálogo de fondo— es justo lo que se veía mal.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Un KV dentro de una lámina ocupa una parte franca de ella; por encima de
#: esto es el fondo de la diapositiva, por debajo un icono.
FRAME_MIN_RATIO = .10
FRAME_MAX_RATIO = .85
#: Proporciones en las que vive una pieza para redes o un arte impreso.
MIN_ASPECT, MAX_ASPECT = .45, 2.3
#: Un producto recortado ocupa una parte visible de la pieza, no un icono.
PRODUCT_MIN_RATIO = .035
PRODUCT_MAX_RATIO = .60
#: Más productos que esto en una pieza es un catálogo.
CATALOG_PRODUCTS = 4
#: Lado largo de la placa, en píxeles.
PLATE_MAX_SIDE = 1600
#: Un documento enorme no se recorre entero: bastan las primeras láminas.
MAX_PAGES = 40

_ESPACIADO = re.compile(r"^(?:\S\s){4,}")


@dataclass
class Imagen:
    xref: int
    bbox: tuple[float, float, float, float]
    alpha: bool


@dataclass
class Texto:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    color: str
    font: str
    #: Texto girado: casi siempre la trama del borde. Nunca se une a otro.
    rotated: bool = False


@dataclass
class Pieza:
    page: int
    rect: tuple[float, float, float, float]
    frame_xref: int = 0
    images: list[Imagen] = field(default_factory=list)
    texts: list[Texto] = field(default_factory=list)
    products: list[Imagen] = field(default_factory=list)
    variable_texts: list[Texto] = field(default_factory=list)
    decorative_texts: list[Texto] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> float:
        return self.rect[3] - self.rect[1]

    @property
    def prices(self) -> int:
        return sum(1 for texto in self.variable_texts if _es_precio(texto.text))

    @property
    def catalog(self) -> bool:
        # Un desplegable trae decenas de precios; un KV, uno o dos.
        return len(self.products) > CATALOG_PRODUCTS or self.prices > CATALOG_PRODUCTS


def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:  # PyMuPDF < 1.24 conserva solo el alias histórico.
        import fitz
    return fitz


def _area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _inter(a, b) -> tuple[float, float, float, float]:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _dentro(box, rect, tolerancia: float = .5) -> bool:
    """El centro de la caja cae en el rect y al menos la mitad de ella también."""

    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if not (rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]):
        return False
    area = _area(box)
    return area <= 0 or _area(_inter(box, rect)) / area >= tolerancia


def _normal(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", texto or "")
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"\s+", "", plano).casefold()


def _hex(color: int) -> str:
    return f"#{(color >> 16) & 255:02X}{(color >> 8) & 255:02X}{color & 255:02X}"


def _textos(page) -> list[Texto]:
    """Los campos de texto de la página, tal como los lee una persona.

    Los bloques del PDF no sirven: Illustrator mete en uno solo la cuota y el
    precio de la misma pastilla. Se parte de las líneas y se reagrupa: primero
    lo que está en la misma altura ("$359" y el "00" volado), después las
    líneas seguidas del mismo cuerpo y color (un nombre de producto a dos
    renglones).
    """

    try:
        datos = page.get_text("dict")
    except Exception:  # noqa: BLE001 - una página ilegible no tumba el documento
        return []
    lineas: list[Texto] = []
    for bloque in datos.get("blocks", []):
        if bloque.get("type") != 0:
            continue
        for linea in bloque.get("lines", []):
            tramos = [t for t in linea.get("spans", []) if str(t.get("text", "")).strip()]
            if not tramos:
                continue
            direccion = linea.get("dir") or (1, 0)
            girada = abs(float(direccion[0]) - 1) > .01
            mayor = max(tramos, key=lambda t: float(t.get("size") or 0))
            lineas.append(Texto(
                text="".join(str(t.get("text", "")) for t in linea.get("spans", [])).strip()[:300],
                bbox=(
                    min(t["bbox"][0] for t in tramos), min(t["bbox"][1] for t in tramos),
                    max(t["bbox"][2] for t in tramos), max(t["bbox"][3] for t in tramos),
                ),
                size=float(mayor.get("size") or 0),
                color=_hex(int(mayor.get("color") or 0)),
                font=str(mayor.get("font") or "")[:80],
                rotated=girada,
            ))

    def unir(a: Texto, b: Texto, separador: str) -> Texto:
        mayor = a if a.size >= b.size else b
        return Texto(
            text=(a.text + separador + b.text).strip()[:300],
            bbox=(
                min(a.bbox[0], b.bbox[0]), min(a.bbox[1], b.bbox[1]),
                max(a.bbox[2], b.bbox[2]), max(a.bbox[3], b.bbox[3]),
            ),
            size=mayor.size, color=mayor.color, font=mayor.font,
        )

    # Misma altura y pegadas: una sola línea.
    lineas.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    hecho = True
    while hecho:
        hecho = False
        for i in range(len(lineas)):
            for j in range(i + 1, len(lineas)):
                a, b = lineas[i], lineas[j]
                if a.rotated or b.rotated:
                    continue
                alto = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
                solape = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
                hueco = max(a.bbox[0], b.bbox[0]) - min(a.bbox[2], b.bbox[2])
                cuerpo = max(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
                if alto > 0 and solape >= alto * .5 and hueco < cuerpo * .6:
                    izquierda, derecha = (a, b) if a.bbox[0] <= b.bbox[0] else (b, a)
                    lineas[i] = unir(izquierda, derecha, "")
                    del lineas[j]
                    hecho = True
                    break
            if hecho:
                break
    # Renglones seguidos del mismo cuerpo y color: un solo campo.
    lineas.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    salida: list[Texto] = []
    for linea in lineas:
        # No basta con mirar el renglón anterior de la lista: en una lámina
        # con dos KV lado a lado, ordenar por altura intercala los textos de
        # uno y otro.
        destino = None
        for indice in range(len(salida) - 1, -1, -1):
            previa = salida[indice]
            if (
                not previa.rotated and not linea.rotated
                and previa.color == linea.color
                and previa.size > 0
                and abs(previa.size - linea.size) <= previa.size * .15
                and min(previa.bbox[2], linea.bbox[2]) > max(previa.bbox[0], linea.bbox[0])
                and 0 <= linea.bbox[1] - previa.bbox[3] < linea.size * .6
                and not _ESPACIADO.match(linea.text)
            ):
                destino = indice
                break
        if destino is None:
            salida.append(linea)
        else:
            salida[destino] = unir(salida[destino], linea, " ")
    return salida


def _alpha_de(doc, xref: int, cache: dict[int, bool]) -> bool:
    if xref <= 0:
        return False
    if xref not in cache:
        try:
            cache[xref] = bool(doc.xref_get_key(xref, "SMask")[1] not in ("null", ""))
        except Exception:  # noqa: BLE001
            cache[xref] = False
    return cache[xref]


def find_pieces(doc) -> list[Pieza]:
    """Las piezas del documento: marcos dentro de láminas, o páginas de arte."""

    alfa: dict[int, bool] = {}
    por_pagina: list[tuple[int, tuple, list[Imagen], list[Texto], list[tuple]]] = []
    hay_marcos = False
    for numero in range(min(len(doc), MAX_PAGES)):
        page = doc[numero]
        rect = tuple(page.rect)
        area_pagina = _area(rect)
        if area_pagina <= 0:
            continue
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:  # noqa: BLE001
            infos = []
        imagenes = [
            Imagen(
                xref=int(info.get("xref") or 0),
                bbox=tuple(float(v) for v in info["bbox"]),
                alpha=_alpha_de(doc, int(info.get("xref") or 0), alfa),
            )
            for info in infos
            if info.get("bbox")
        ]
        marcos: list[tuple] = []
        for imagen in imagenes:
            x0, y0, x1, y1 = imagen.bbox
            if imagen.alpha or imagen.xref <= 0:
                continue
            # Un KV enmarcado en la lámina cabe entero en ella; una foto que
            # sangra por fuera del borde es el fondo recortado de un arte.
            if x0 < rect[0] or y0 < rect[1] or x1 > rect[2] or y1 > rect[3]:
                continue
            ratio = _area(imagen.bbox) / area_pagina
            if not FRAME_MIN_RATIO <= ratio <= FRAME_MAX_RATIO:
                continue
            ancho, alto = x1 - x0, y1 - y0
            if alto <= 0 or not MIN_ASPECT <= ancho / alto <= MAX_ASPECT:
                continue
            if any(_area(_inter(imagen.bbox, otro[1])) > .5 * _area(imagen.bbox) for otro in marcos):
                continue
            marcos.append((imagen.xref, imagen.bbox))
        hay_marcos = hay_marcos or bool(marcos)
        por_pagina.append((numero, rect, imagenes, _textos(page), marcos))

    piezas: list[Pieza] = []
    for numero, rect, imagenes, textos, marcos in por_pagina:
        if hay_marcos:
            # Un toolkit: solo los KV enmarcados son piezas. Las láminas de
            # texto —justificación, insight— no son arte.
            zonas = [(xref, caja) for xref, caja in marcos]
        else:
            ancho, alto = rect[2] - rect[0], rect[3] - rect[1]
            if alto <= 0 or not MIN_ASPECT <= ancho / alto <= MAX_ASPECT:
                continue
            if not imagenes and not textos:
                continue
            zonas = [(0, rect)]
        for xref, caja in zonas:
            pieza = Pieza(page=numero, rect=caja, frame_xref=xref)
            pieza.images = [
                imagen for imagen in imagenes
                if imagen.xref != xref and _dentro(imagen.bbox, caja)
            ]
            pieza.texts = [texto for texto in textos if _dentro(texto.bbox, caja)]
            piezas.append(pieza)
    _clasificar(piezas, doc)
    return piezas


def _firma(doc, xref: int, cache: dict[int, tuple | None]) -> tuple | None:
    """Huella visual de una imagen, para reconocerla aunque cambie de xref.

    Illustrator incrusta el mismo logo una vez por mesa de trabajo: en el
    toolkit de CrediFest el sello "Crédito Directo" tiene un xref distinto en
    cada KV. Comparar xrefs lo tomaba por producto y lo borraba de la placa.

    La huella junta forma (un dHash de 16x16), color medio y proporción: dos
    refrigeradoras distintas tienen casi la misma silueta, y confundirlas
    haría pasar el producto por un logo repetido.
    """

    if xref in cache:
        return cache[xref]
    firma = None
    try:
        import io

        from PIL import Image, ImageStat

        datos = doc.extract_image(xref)
        with Image.open(io.BytesIO(datos["image"])) as imagen:
            imagen.draft("RGB", (128, 128))
            proporcion = round(imagen.width / max(1, imagen.height), 2)
            color = imagen.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
            media = tuple(int(v) for v in ImageStat.Stat(color).mean)
            gris = color.convert("L").resize((17, 16), Image.Resampling.BILINEAR)
            pixeles = list(gris.getdata())
        forma = 0
        for fila in range(16):
            for columna in range(16):
                izquierda = pixeles[fila * 17 + columna]
                derecha = pixeles[fila * 17 + columna + 1]
                forma = (forma << 1) | (1 if izquierda > derecha else 0)
        firma = (forma, media, proporcion)
    except Exception:  # noqa: BLE001 - sin huella se compara por xref
        firma = None
    cache[xref] = firma
    return firma


def _misma(a: tuple | None, b: tuple | None) -> bool:
    if a is None or b is None:
        return False
    forma_a, media_a, proporcion_a = a
    forma_b, media_b, proporcion_b = b
    return (
        bin(forma_a ^ forma_b).count("1") <= 24
        and max(abs(x - y) for x, y in zip(media_a, media_b)) <= 18
        and abs(proporcion_a - proporcion_b) <= .06
    )


def _es_precio(texto: str) -> bool:
    return bool(re.search(r"\d", texto)) and bool(
        re.search(r"[$€]|\bcuotas?\b|\bsemanal|\bmensual|\bentrada\b", texto, re.IGNORECASE)
    )


def _clasificar(piezas: list[Pieza], doc=None) -> None:
    """Separa en cada pieza lo que es identidad de lo que cambia por fila."""

    varias = len(piezas) > 1

    # Candidatas a producto: recortes con alfa de tamaño franco.
    candidatas: dict[int, list[Imagen]] = {}
    for indice, pieza in enumerate(piezas):
        area = pieza.width * pieza.height
        if area <= 0:
            continue
        for imagen in pieza.images:
            if not imagen.alpha or imagen.xref <= 0:
                continue
            ratio = _area(_inter(imagen.bbox, pieza.rect)) / area
            if PRODUCT_MIN_RATIO <= ratio <= PRODUCT_MAX_RATIO:
                candidatas.setdefault(indice, []).append(imagen)
    huellas: dict[int, tuple | None] = {}
    firmas_por_pieza: dict[int, list[tuple[int, tuple | None]]] = {}
    for indice, imagenes in candidatas.items():
        firmas_por_pieza[indice] = [
            (imagen.xref, _firma(doc, imagen.xref, huellas) if doc is not None else None)
            for imagen in imagenes
        ]
    # Todas las imágenes alfa del documento cuentan para saber si algo se
    # repite, no solo las de tamaño de producto: el logo pequeño de una lámina
    # es el mismo que el grande de otra.
    todas: list[tuple[int, int, tuple | None]] = []
    if varias and doc is not None:
        for indice, pieza in enumerate(piezas):
            for imagen in pieza.images:
                if imagen.alpha and imagen.xref > 0:
                    todas.append((indice, imagen.xref, _firma(doc, imagen.xref, huellas)))

    def repetida(indice: int, xref: int, firma: tuple | None) -> bool:
        for otra, otro_xref, otra_firma in todas:
            if otra == indice:
                continue
            if otro_xref == xref or _misma(firma, otra_firma):
                return True
        return False

    for indice, pieza in enumerate(piezas):
        productos: list[Imagen] = []
        for imagen, (_xref, firma) in zip(
            candidatas.get(indice, []), firmas_por_pieza.get(indice, [])
        ):
            # Lo que aparece en varias piezas es un logo o un sello de campaña.
            if varias and repetida(indice, imagen.xref, firma):
                continue
            productos.append(imagen)
        pieza.products = productos

    # Un texto repetido es identidad solo si también sale en una pieza sin
    # producto —la "layout base", un cierre— o en muchas. Dos KV de producto
    # que comparten "REFRIGERADORA TOP MOUNT" comparten el ejemplo, no un
    # rótulo fijo: ese nombre es lo primero que cambia la matriz.
    en_piezas: dict[str, list[int]] = {}
    for indice, pieza in enumerate(piezas):
        for clave in {_normal(texto.text) for texto in pieza.texts}:
            en_piezas.setdefault(clave, []).append(indice)

    def rotulo_fijo(clave: str) -> bool:
        donde = en_piezas.get(clave, [])
        if len(donde) >= 3:
            return True
        return len(donde) >= 2 and any(not piezas[i].products for i in donde)

    for pieza in piezas:
        repetidos: dict[str, int] = {}
        for texto in pieza.texts:
            clave = _normal(texto.text)
            repetidos[clave] = repetidos.get(clave, 0) + 1
        variables: list[Texto] = []
        decorativos: list[Texto] = []
        for texto in pieza.texts:
            clave = _normal(texto.text)
            if not clave:
                continue
            # Tipografía usada como trama —"C R E D I F E S T • F E S T I V A L"
            # repetido por el borde— es decoración, no un campo.
            tiene_cifra = bool(re.search(r"\d", texto.text))
            if texto.rotated or _ESPACIADO.match(texto.text) or (
                repetidos.get(clave, 0) >= 3 and not tiene_cifra
            ):
                decorativos.append(texto)
                continue
            if varias and not tiene_cifra and rotulo_fijo(clave):
                continue
            variables.append(texto)
        pieza.variable_texts = variables
        pieza.decorative_texts = decorativos


def best_pieces(piezas: list[Pieza]) -> list[Pieza]:
    """Una pieza por familia de formato, la que mejor sirve de plantilla.

    Se prefiere la que tiene producto y campos: es la que enseña dónde va cada
    cosa. Una "layout base" vacía da la misma placa, pero ninguna posición.
    """

    from .campaign_layout_from_art import aspect_key

    # Hace falta algo que la matriz rellene: un precio, o un producto con su
    # nombre. Un KV de awareness o un rompetráfico troquelado sin campos no
    # enseña dónde va nada, y su placa recortada engaña más que ayuda.
    elegibles = [
        pieza for pieza in piezas
        if not pieza.catalog
        and (pieza.prices or (pieza.products and pieza.variable_texts))
    ]

    def orden(pieza: Pieza):
        campos = len(pieza.variable_texts)
        return (
            0 if pieza.products else 1,
            # Un precio dice que es un KV de producto, que es lo que produce
            # la matriz; un KV de awareness sin precio enseña menos huecos.
            0 if pieza.prices else 1,
            0 if 2 <= campos <= 12 else 1,
            pieza.page,
            pieza.rect[0],
        )

    mejores: dict[str, Pieza] = {}
    for pieza in sorted(elegibles, key=orden):
        familia = aspect_key((int(pieza.width), int(pieza.height)))
        mejores.setdefault(familia, pieza)
    return list(mejores.values())


def render_plate(pdf_path: Path, pieza: Pieza, target: Path) -> tuple[int, int, float]:
    """Renderiza la pieza sin su contenido variable. Devuelve (ancho, alto, escala).

    Se abre una copia del documento en memoria para cada placa: quitar una
    imagen la cambia en todo el documento y la siguiente placa la necesitaría.
    """

    fitz = _fitz()
    doc = fitz.open(pdf_path)
    try:
        page = doc[pieza.page]
        for imagen in pieza.products:
            try:
                page.delete_image(imagen.xref)
            except Exception:  # noqa: BLE001 - una imagen protegida se queda
                logger.info("No se pudo quitar la imagen %s de la placa", imagen.xref)
        if pieza.variable_texts:
            for texto in pieza.variable_texts:
                x0, y0, x1, y1 = texto.bbox
                # Un pelo hacia dentro: la caja de un tramo toca la del vecino
                # fijo y la redacción se llevaría también a ese.
                margen = min(.6, (y1 - y0) * .08)
                page.add_redact_annot(
                    fitz.Rect(x0 + margen, y0 + margen, x1 - margen, y1 - margen),
                    fill=False, cross_out=False,
                )
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        escala = PLATE_MAX_SIDE / max(pieza.width, pieza.height)
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(escala, escala), clip=fitz.Rect(*pieza.rect), alpha=False
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(target))
        return pixmap.width, pixmap.height, escala
    finally:
        doc.close()


def reads_for(pieza: Pieza, escala: float) -> list[dict]:
    """Los campos variables como lecturas, en píxeles de la placa."""

    x0, y0 = pieza.rect[0], pieza.rect[1]
    lecturas = []
    for texto in pieza.variable_texts:
        bx0, by0, bx1, by1 = texto.bbox
        lecturas.append({
            "text": texto.text[:200],
            "bbox": [
                int(round((bx0 - x0) * escala)), int(round((by0 - y0) * escala)),
                int(round((bx1 - x0) * escala)), int(round((by1 - y0) * escala)),
            ],
            "confidence": 1.0,
            "role": "content",
            "color": texto.color,
            "font": texto.font,
            "size": round(texto.size * escala, 1),
        })
    return lecturas


def product_box(pieza: Pieza, escala: float) -> tuple[int, int, int, int] | None:
    if not pieza.products:
        return None
    x0, y0 = pieza.rect[0], pieza.rect[1]
    cajas = [_inter(imagen.bbox, pieza.rect) for imagen in pieza.products]
    return (
        int(round((min(c[0] for c in cajas) - x0) * escala)),
        int(round((min(c[1] for c in cajas) - y0) * escala)),
        int(round((max(c[2] for c in cajas) - x0) * escala)),
        int(round((max(c[3] for c in cajas) - y0) * escala)),
    )


def _fondos(pieza: Pieza, doc) -> list[int]:
    """Imágenes que hacen de fondo: el marco del KV y la lámina que lo rodea."""

    area = pieza.width * pieza.height
    fondos = {pieza.frame_xref} if pieza.frame_xref > 0 else set()
    page = doc[pieza.page]
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001
        infos = []
    for info in infos:
        xref = int(info.get("xref") or 0)
        if xref <= 0 or not info.get("bbox"):
            continue
        cubre = _area(_inter(tuple(info["bbox"]), pieza.rect))
        if area > 0 and cubre / area >= .80:
            fondos.add(xref)
    return sorted(fondos)


def _fondo_imagen(doc, pieza: Pieza, xrefs: list[int], escala: float, size: tuple[int, int]):
    """El fondo colocado donde estaba, al tamaño de la placa."""

    import io

    from PIL import Image

    lienzo = Image.new("RGB", size, (0, 0, 0))
    puesto = False
    page = doc[pieza.page]
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001
        infos = []
    # Se pintan en el orden del documento: la lámina primero, el marco encima.
    for info in infos:
        xref = int(info.get("xref") or 0)
        if xref not in xrefs or not info.get("bbox"):
            continue
        try:
            datos = doc.extract_image(xref)
            with Image.open(io.BytesIO(datos["image"])) as crudo:
                imagen = crudo.convert("RGB")
        except Exception:  # noqa: BLE001
            continue
        x0, y0, x1, y1 = info["bbox"]
        destino = (
            int(round((x1 - x0) * escala)), int(round((y1 - y0) * escala))
        )
        if destino[0] <= 0 or destino[1] <= 0:
            continue
        colocada = imagen.resize(destino, Image.Resampling.LANCZOS)
        lienzo.paste(
            colocada,
            (int(round((x0 - pieza.rect[0]) * escala)), int(round((y0 - pieza.rect[1]) * escala))),
        )
        puesto = True
    return lienzo if puesto else None


def _sin(page, textos: list[Texto]) -> None:
    fitz = _fitz()
    if not textos:
        return
    for texto in textos:
        x0, y0, x1, y1 = texto.bbox
        margen = min(.6, (y1 - y0) * .08)
        page.add_redact_annot(
            fitz.Rect(x0 + margen, y0 + margen, x1 - margen, y1 - margen),
            fill=False, cross_out=False,
        )
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )


def decompose(pdf_path: Path, pieza: Pieza, escala: float):
    """Fondo y capa de identidad por separado, para recomponer otro formato.

    La capa de identidad es la pieza sin fondo, sin producto y sin campos:
    logos, sellos, pastillas de precio vacías. Sobre transparente, para poder
    mover cada elemento por su cuenta.
    """

    import io

    from PIL import Image

    fitz = _fitz()
    doc = fitz.open(pdf_path)
    try:
        fondos = _fondos(pieza, doc)
        ancho = int(round(pieza.width * escala))
        alto = int(round(pieza.height * escala))
        fondo = _fondo_imagen(doc, pieza, fondos, escala, (ancho, alto))
        page = doc[pieza.page]
        for imagen in pieza.products:
            try:
                page.delete_image(imagen.xref)
            except Exception:  # noqa: BLE001
                continue
        # La trama tipográfica del borde se dibuja para un marco concreto: en
        # otra proporción quedaría cortada o estirada, así que no viaja.
        _sin(page, [*pieza.variable_texts, *pieza.decorative_texts])
        matriz, recorte = fitz.Matrix(escala, escala), fitz.Rect(*pieza.rect)
        # Con fondo: se renderiza la pieza entera y la identidad se separa por
        # diferencia contra el fondo solo. Renderizar sin el fondo sobre
        # transparente no basta: una lámina de presentación trae debajo un
        # rectángulo blanco a sangre, y todo salía opaco.
        con_fondo = page.get_pixmap(matrix=matriz, clip=recorte, alpha=False)
        entera = Image.open(io.BytesIO(con_fondo.tobytes("png"))).convert("RGB")
        if entera.size != (ancho, alto):
            entera = entera.resize((ancho, alto), Image.Resampling.LANCZOS)
        if fondo is not None:
            return fondo, _separar(entera, fondo)
        for xref in fondos:
            try:
                page.delete_image(xref)
            except Exception:  # noqa: BLE001
                continue
        pixmap = page.get_pixmap(matrix=matriz, clip=recorte, alpha=True)
        capa = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGBA")
        if capa.size != (ancho, alto):
            capa = capa.resize((ancho, alto), Image.Resampling.LANCZOS)
        return None, capa
    finally:
        doc.close()


def _separar(entera, fondo):
    """Lo que la pieza pinta encima de su fondo, con alfa suave en el borde."""

    import cv2
    import numpy as np
    from PIL import Image

    a = np.asarray(entera, dtype=np.int16)
    b = np.asarray(fondo.resize(entera.size), dtype=np.int16)
    diferencia = np.abs(a - b).max(axis=2).astype(np.uint8)
    mascara = (diferencia > 28).astype(np.uint8) * 255
    # Se cierra lo que el umbral deja a medias —el interior liso de una
    # pastilla parecida al fondo— y se suaviza el borde para no recortar a
    # sierra al moverlo.
    mascara = cv2.morphologyEx(mascara, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    mascara = cv2.GaussianBlur(mascara, (0, 0), 1.2)
    capa = entera.convert("RGBA")
    capa.putalpha(Image.fromarray(mascara))
    return capa


def components(capa) -> list[tuple[int, int, int, int]]:
    """Cajas de los elementos sueltos de la capa de identidad."""

    import cv2
    import numpy as np

    alfa = np.asarray(capa.getchannel("A"))
    alto, ancho = alfa.shape
    factor = min(1.0, 320 / max(ancho, alto))
    pequeño = cv2.resize(
        alfa, (max(1, int(ancho * factor)), max(1, int(alto * factor))),
        interpolation=cv2.INTER_AREA,
    )
    mascara = (pequeño > 20).astype(np.uint8)
    # Las letras de un logo y su sombra son un mismo elemento: se unen antes
    # de contarlos.
    mascara = cv2.dilate(mascara, np.ones((5, 5), np.uint8))
    total, _etiquetas, stats, _centros = cv2.connectedComponentsWithStats(mascara, connectivity=8)
    cajas = []
    minimo = mascara.size * .0006
    for indice in range(1, total):
        x, y, w, h, area = stats[indice]
        if area < minimo:
            continue
        # Una franja fina a lo largo de un borde no es un elemento: es el
        # filo del marco o un píxel de diferencia entre el fondo renderizado y
        # el extraído. Contarla impedía agrandar nada en un horizontal.
        fw, fh = mascara.shape[1], mascara.shape[0]
        if (w < fw * .03 and h > fh * .6) or (h < fh * .03 and w > fw * .6):
            continue
        cajas.append((
            int(x / factor), int(y / factor),
            min(ancho, int((x + w) / factor) + 1), min(alto, int((y + h) / factor) + 1),
        ))
    return cajas


def _ancla(inicio: float, fin: float, total: float) -> str:
    centro = (inicio + fin) / 2 / total
    return "start" if centro < 1 / 3 else "end" if centro > 2 / 3 else "center"


def _mover(
    caja: tuple[float, float, float, float],
    origen: tuple[int, int],
    zona: tuple[float, float, float, float],
    escala: float,
) -> tuple[float, float, float, float]:
    """Lleva una caja de la pieza al formato nuevo, anclada a su borde cercano.

    Un logo arriba a la derecha sigue arriba a la derecha y a la misma
    distancia proporcional del borde; lo centrado sigue centrado.
    """

    ancho0, alto0 = origen
    zx0, zy0, zx1, zy1 = zona
    x0, y0, x1, y1 = caja
    w, h = (x1 - x0) * escala, (y1 - y0) * escala

    def eje(a0, a1, total0, z0, z1):
        ancla = _ancla(a0, a1, total0)
        if ancla == "start":
            return z0 + a0 * escala
        if ancla == "end":
            return z1 - (total0 - a1) * escala - (a1 - a0) * escala
        return (z0 + z1) / 2 + (a0 - total0 / 2) * escala

    nx = eje(x0, x1, ancho0, zx0, zx1)
    ny = eje(y0, y1, alto0, zy0, zy1)
    return nx, ny, nx + w, ny + h


def _contenedor(caja, elementos, umbral: float = .5):
    """El elemento de identidad que contiene la caja de un campo, si lo hay."""

    area = _area(caja)
    if area <= 0:
        return None
    mejor, indice = 0.0, None
    for posicion, original in enumerate(elementos):
        parte = _area(_inter(caja, original)) / area
        if parte > mejor:
            mejor, indice = parte, posicion
    return indice if mejor >= umbral else None


def _plan(elementos, cajas_campo, dentro, origen, zona, escala):
    """Dónde cae cada elemento y cada campo con una escala dada."""

    destinos = [_mover(caja, origen, zona, escala) for caja in elementos]
    campos: dict[str, tuple[float, float, float, float]] = {}
    for hueco, caja in cajas_campo.items():
        indice = dentro.get(hueco)
        if indice is not None:
            original, nueva = elementos[indice], destinos[indice]
            x0 = nueva[0] + (caja[0] - original[0]) * escala
            y0 = nueva[1] + (caja[1] - original[1]) * escala
            campos[hueco] = (
                x0, y0, x0 + (caja[2] - caja[0]) * escala, y0 + (caja[3] - caja[1]) * escala
            )
        else:
            campos[hueco] = _mover(caja, origen, zona, escala)
    return destinos, campos


def _choca(a, b, margen: float = 4.0) -> bool:
    return (
        min(a[2], b[2]) - max(a[0], b[0]) > margen
        and min(a[3], b[3]) - max(a[1], b[1]) > margen
    )


def adapt(
    fondo, capa, cajas_campo: dict[str, tuple[int, int, int, int]],
    size: tuple[int, int], safe: dict[str, float],
    *,
    omit: set[str] | None = None,
    extras: tuple[str, ...] | list[str] = (),
    aspecto: float | None = None,
):
    """Recompone la pieza en otra proporción. Devuelve (placa, cajas de campo).

    ``extras`` son los campos que la fila trae y el arte no tiene (titular,
    CTA…): la composición les reserva su bloque y devuelve sus cajas.
    ``aspecto`` es el ancho/alto de lo que irá en el hueco del producto.

    El fondo cubre el lienzo; cada elemento de identidad se ancla a su borde.
    La escala se busca: se empieza por la que conserva el área de cada
    elemento y se baja hasta que nada se pise y todo quepa en el área segura.
    Con la escala mínima —la que mete la pieza entera— un horizontal quedaba
    con el logo y el producto diminutos en un mar de fondo.

    Los campos viajan con el elemento que los contiene —el precio con su
    pastilla—, así no se separa la cifra de su fondo. ``omit`` quita los
    elementos que solo existen para ciertos campos: sin precio en la fila, la
    pastilla vacía no debe quedar en la pieza.
    """

    from PIL import Image, ImageOps

    ancho, alto = size
    origen = capa.size
    zona = (
        ancho * float(safe.get("left", .04)), alto * float(safe.get("top", .04)),
        ancho * (1 - float(safe.get("right", .04))), alto * (1 - float(safe.get("bottom", .04))),
    )
    minima = min((zona[2] - zona[0]) / origen[0], (zona[3] - zona[1]) / origen[1])
    maxima = min(
        1.0 * max(ancho, alto) / max(origen),
        ((zona[2] - zona[0]) * (zona[3] - zona[1]) / (origen[0] * origen[1])) ** .5,
    )
    maxima = max(maxima, minima)

    elementos = components(capa)
    dentro = {
        hueco: _contenedor(caja, elementos) for hueco, caja in cajas_campo.items()
    }
    # Campos de texto escritos directamente sobre el fondo, sin pastilla: se
    # agrupan como un bloque propio para que viajen juntos y en su orden.
    sueltos = [
        caja for hueco, caja in cajas_campo.items()
        if hueco != "product" and dentro.get(hueco) is None
    ]
    if sueltos:
        elementos.append((
            min(c[0] for c in sueltos), min(c[1] for c in sueltos),
            max(c[2] for c in sueltos), max(c[3] for c in sueltos),
        ))
        for hueco in cajas_campo:
            if hueco != "product" and dentro.get(hueco) is None:
                dentro[hueco] = len(elementos) - 1
    # Lo omitido solo deja de pintarse: el plan se calcula con todo, para que
    # la variante sin pastilla ponga el producto y el nombre en el mismo
    # sitio que la placa completa cuyas posiciones usa el renderer.
    quitar = {
        dentro[hueco] for hueco in (omit or set()) if dentro.get(hueco) is not None
    }
    sin_quitar: set[int] = set()
    # Lo que ocupa sitio propio: cada elemento y cada campo suelto (el
    # producto). Dos cosas que no se tocaban en el original no pueden
    # tocarse en la adaptación.
    unidades = [("e", i, caja) for i, caja in enumerate(elementos)]
    unidades += [
        ("c", hueco, caja) for hueco, caja in cajas_campo.items()
        if dentro.get(hueco) is None
    ]
    separadas = [
        (a, b) for x, a in enumerate(unidades) for b in unidades[x + 1:]
        if not _choca(a[2], b[2], 0)
    ]

    # Bajo el área segura de un story o un reel la interfaz tapa texto, no
    # imagen: el producto puede bajar hasta la mitad de esa franja y la pieza
    # no queda con un tercio de fondo vacío.
    reserva = alto - zona[3]
    sangrado = min(zona[3] + (alto * .94 - zona[3]) * .8, alto * .90) if reserva > alto * .12 else None
    if extras or aspecto is not None or abs(math.log((ancho / alto) / (origen[0] / origen[1]))) > .08:
        # Otra proporción, o campos que el original no tenía: se compone por
        # roles con la retícula de cada plataforma, en vez de estirar la del
        # original o apilar el mensaje encima del producto.
        plan = _componer(
            elementos, cajas_campo, dentro, origen, zona, size, tuple(extras), sangrado, aspecto,
            quitar if "product_name" in extras else set(),
        )
        if plan is not None:
            return _pintar(fondo, capa, size, elementos, quitar, zona, *plan)
    if (ancho / alto) / (origen[0] / origen[1]) > 1.5:
        # Un horizontal a partir de un vertical no se arregla anclando: la
        # columna de la izquierda (logo, pastilla, sellos) no cabe en la
        # altura y todo queda diminuto. Se redistribuye en filas.
        plan = _reflujo(elementos, cajas_campo, dentro, sin_quitar, origen, zona, set())
        if plan is not None:
            return _pintar(fondo, capa, size, elementos, quitar, zona, *plan)
    escala = minima
    for paso in range(13):
        prueba = maxima - (maxima - minima) * paso / 12
        destinos, campos = _plan(elementos, cajas_campo, dentro, origen, zona, prueba)

        def donde(unidad):
            return destinos[unidad[1]] if unidad[0] == "e" else campos[unidad[1]]

        fuera = any(
            donde(u)[0] < zona[0] - 2 or donde(u)[1] < zona[1] - 2
            or donde(u)[2] > zona[2] + 2 or donde(u)[3] > zona[3] + 2
            for u in unidades if u[0] == "e"
        )
        if not fuera and not any(_choca(donde(a), donde(b)) for a, b in separadas):
            escala = prueba
            break
    destinos, campos = _plan(elementos, cajas_campo, dentro, origen, zona, escala)
    return _pintar(fondo, capa, size, elementos, quitar, zona, destinos, campos)


def _pintar(fondo, capa, size, elementos, quitar, zona, destinos, campos):
    from PIL import Image, ImageOps

    if fondo is not None:
        # El fondo de un KV suele traer su propio marco en el borde (la franja
        # morada del toolkit). Recortado a otra proporción, ese marco quedaba
        # como dos bandas sueltas arriba y abajo: se descarta antes de encajar.
        margen_x, margen_y = int(fondo.width * .045), int(fondo.height * .045)
        interior = fondo.crop((margen_x, margen_y, fondo.width - margen_x, fondo.height - margen_y))
        placa = ImageOps.fit(interior, size, method=Image.Resampling.LANCZOS).convert("RGBA")
    else:
        placa = Image.new("RGBA", size, (0, 0, 0, 255))
    for indice, (caja, nueva) in enumerate(zip(elementos, destinos)):
        if indice in quitar or nueva[2] <= nueva[0] or nueva[3] <= nueva[1]:
            continue
        recorte = capa.crop(caja)
        medida = (max(1, int(round(nueva[2] - nueva[0]))), max(1, int(round(nueva[3] - nueva[1]))))
        recorte = recorte.resize(medida, Image.Resampling.LANCZOS)
        placa.alpha_composite(recorte, (int(round(nueva[0])), int(round(nueva[1]))))

    salida: dict[str, tuple[int, int, int, int]] = {}
    for hueco, caja in campos.items():
        # Un campo suelto que se sale del área segura se recorta a ella: el
        # producto se encaja dentro de su caja, así que solo pierde aire. El
        # producto no es texto: puede bajar bajo el área segura, no del lienzo.
        limite = (0, 0, size[0], size[1]) if hueco == "product" else zona
        x0, y0 = max(limite[0], caja[0]), max(limite[1], caja[1])
        x1, y1 = min(limite[2], caja[2]), min(limite[3], caja[3])
        if x1 <= x0 or y1 <= y0:
            continue
        salida[hueco] = (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))
    return placa.convert("RGB"), salida


def ampliar(caja, obstaculos, limite, separacion: float):
    """Crece la caja por cada lado hasta tocar otro elemento o el borde."""

    x0, y0, x1, y1 = (float(v) for v in caja)
    otros = [o for o in obstaculos if not _choca(o, caja, 0)]
    paso = max(2.0, min(limite[2] - limite[0], limite[3] - limite[1]) * .01)

    def libre(prueba):
        return all(
            not _choca(prueba, (o[0] - separacion, o[1] - separacion, o[2] + separacion, o[3] + separacion), 0)
            for o in otros
        )

    for _ in range(400):
        movido = False
        for lado in range(4):
            prueba = [x0, y0, x1, y1]
            if lado == 0 and x0 - paso >= limite[0]:
                prueba[0] -= paso
            elif lado == 1 and y0 - paso >= limite[1]:
                prueba[1] -= paso
            elif lado == 2 and x1 + paso <= limite[2]:
                prueba[2] += paso
            elif lado == 3 and y1 + paso <= limite[3]:
                prueba[3] += paso
            else:
                continue
            if libre(tuple(prueba)):
                x0, y0, x1, y1 = prueba
                movido = True
        if not movido:
            break
    return (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))


def roles(elementos, cajas_campo, dentro, origen) -> dict | None:
    """Qué es cada elemento de identidad, por su sitio y su contenido.

    * ``offer``: el que contiene el precio o el nombre (la pastilla).
    * ``brand``: pequeño y pegado a una esquina (el logo de la marca).
    * ``lockup``: el mayor de los demás (el logo de campaña).
    * ``seals``: el resto (sellos, iconos de financiación).
    """

    ancho0, alto0 = origen
    oferta = None
    for hueco in ("price", "installment", "product_name"):
        if dentro.get(hueco) is not None:
            oferta = dentro[hueco]
            break
    marcas, resto = [], []
    for indice, caja in enumerate(elementos):
        if indice == oferta:
            continue
        area = _area(caja) / (ancho0 * alto0)
        cerca_x = caja[0] < ancho0 * .2 or caja[2] > ancho0 * .8
        cerca_y = caja[1] < alto0 * .2 or caja[3] > alto0 * .8
        if area < .05 and cerca_x and cerca_y and caja[1] < alto0 * .5:
            marcas.append(indice)
        else:
            resto.append(indice)
    lockup = max(resto, key=lambda i: _area(elementos[i]), default=None)
    sellos = [i for i in resto if i != lockup]
    if lockup is None:
        return None
    return {
        "offer": oferta, "brand": marcas, "lockup": lockup, "seals": sellos,
        "product": cajas_campo.get("product") if dentro.get("product") is None else None,
    }


def _encajar(caja, marco, alinear=("start", "start"), tope: float = 10.0):
    """La caja escalada para caber en el marco, sin deformar, alineada."""

    w, h = caja[2] - caja[0], caja[3] - caja[1]
    mw, mh = marco[2] - marco[0], marco[3] - marco[1]
    if w <= 0 or h <= 0 or mw <= 0 or mh <= 0:
        return None
    escala = min(mw / w, mh / h, tope)
    nw, nh = w * escala, h * escala
    ax, ay = alinear
    x = marco[0] if ax == "start" else marco[2] - nw if ax == "end" else marco[0] + (mw - nw) / 2
    y = marco[1] if ay == "start" else marco[3] - nh if ay == "end" else marco[1] + (mh - nh) / 2
    return (x, y, x + nw, y + nh)


def _fila(indices, elementos, marco, gap, alinear_x="start"):
    """Varios elementos en una fila, a la misma escala, dentro del marco."""

    if not indices:
        return {}
    anchos = sum(elementos[i][2] - elementos[i][0] for i in indices)
    alto = max(elementos[i][3] - elementos[i][1] for i in indices)
    mw, mh = marco[2] - marco[0], marco[3] - marco[1]
    disponible = mw - gap * (len(indices) - 1)
    if anchos <= 0 or alto <= 0 or disponible <= 0:
        return {}
    escala = min(disponible / anchos, mh / alto)
    total = anchos * escala + gap * (len(indices) - 1)
    x = marco[0] if alinear_x == "start" else marco[0] + (mw - total) / 2
    salida = {}
    for indice in sorted(indices, key=lambda i: elementos[i][0]):
        caja = elementos[indice]
        w, h = (caja[2] - caja[0]) * escala, (caja[3] - caja[1]) * escala
        y = marco[3] - h - (mh - alto * escala) / 2
        salida[indice] = (x, y, x + w, y + h)
        x += w + gap
    return salida


#: Proporciones de cada campo del bloque de mensaje (ancho / alto) a un ancho
#: de referencia de 1000: el titular a dos o tres renglones, el CTA como botón.
_BLOQUE = {
    "product_name": 7.0, "headline": 3.0, "subheadline": 9.0, "previous_price": 9.0,
    "discount": 7.0, "cta": 4.4, "validity": 16.0,
}
#: Orden de lectura del bloque, como en cualquier pieza de retail.
_ORDEN_BLOQUE = ("product_name", "headline", "subheadline", "previous_price", "discount", "cta", "validity")


def _bloque(extras, modo: str):
    """Caja natural del bloque de mensaje y la de cada campo dentro de ella.

    ``columna``: todo apilado y centrado (verticales, columnas estrechas).
    ``fila``: el texto a la izquierda y el botón del CTA a la derecha, como la
    barra "Las mejores PROMOS están aquí · VER PRODUCTOS" de un banner web.
    """

    campos = [c for c in _ORDEN_BLOQUE if c in extras]
    if not campos:
        return None
    base, sep = 1000.0, 1000.0 * .035
    cajas: dict[str, tuple[float, float, float, float]] = {}
    # Sin titular, el subtítulo es el mensaje: con el cuerpo de un subtítulo
    # quedaba una línea diminuta suelta sobre el botón.
    proporcion = dict(_BLOQUE)
    if "headline" not in campos:
        proporcion["subheadline"] = 4.5
    if modo == "fila" and "cta" in campos and len(campos) > 1:
        texto_w, boton_w = base * .60, base * .34
        y = 0.0
        for clave in (c for c in campos if c != "cta"):
            alto = texto_w / proporcion[clave]
            cajas[clave] = (0.0, y, texto_w, y + alto)
            y += alto + sep
        alto_texto = y - sep
        alto_boton = boton_w / 3.4
        medio = alto_texto / 2
        cajas["cta"] = (base - boton_w, medio - alto_boton / 2, base, medio + alto_boton / 2)
        alto_total = max(alto_texto, alto_boton)
        if alto_boton > alto_texto:
            desfase = (alto_boton - alto_texto) / 2
            cajas = {k: (v[0], v[1] + desfase, v[2], v[3] + desfase) for k, v in cajas.items()}
        return base, alto_total, cajas
    y = 0.0
    for clave in campos:
        ancho = base * (.62 if clave in {"cta", "discount"} else 1.0)
        alto = ancho / proporcion[clave]
        x = (base - ancho) / 2
        cajas[clave] = (x, y, x + ancho, y + alto)
        y += alto + sep
    return base, y - sep, cajas


def _pila(items, marco, gap, alinear="center", aire=.07):
    """Apila elementos en el marco y crece a todos hasta llenarlo.

    ``items``: ``(clave, ancho, alto, cuota)``; la cuota es la parte del alto
    que cada uno puede llegar a ocupar. Se busca el mayor factor común que
    quepa —el ancho del marco también limita—, y lo que sobra se reparte entre
    los huecos (hasta ``aire`` del alto cada uno) y el resto arriba y abajo.
    Así una columna no deja medio lienzo vacío bajo los sellos.
    """

    x0, y0, x1, y1 = marco
    ancho, alto = x1 - x0, y1 - y0
    items = [i for i in items if i[1] > 0 and i[2] > 0]
    if not items or ancho <= 0 or alto <= 0:
        return {}
    libre = alto - gap * (len(items) - 1)

    def altos(k):
        return [min(k * cuota * alto, ancho * h / w) for _c, w, h, cuota in items]

    bajo, tope = 0.0, 8.0
    for _ in range(40):
        medio = (bajo + tope) / 2
        if sum(altos(medio)) <= libre:
            bajo = medio
        else:
            tope = medio
    hs = altos(bajo)
    sobra = max(0.0, libre - sum(hs))
    entre = min(sobra / (len(items) - 1), alto * aire) if len(items) > 1 else 0.0
    y = y0 + (sobra - entre * (len(items) - 1)) / 2
    salida = {}
    for (clave, w, h, _cuota), alto_i in zip(items, hs):
        ancho_i = alto_i * w / h
        x = (
            x0 if alinear == "start" else x1 - ancho_i if alinear == "end"
            else x0 + (ancho - ancho_i) / 2
        )
        salida[clave] = (x, y, x + ancho_i, y + alto_i)
        y += alto_i + gap + entre
    return salida


def _hilera(items, marco, gap, aire=.06):
    """Como ``_pila`` pero en horizontal, a toda la altura del marco (banners)."""

    x0, y0, x1, y1 = marco
    ancho, alto = x1 - x0, y1 - y0
    items = [i for i in items if i[1] > 0 and i[2] > 0]
    if not items or ancho <= 0 or alto <= 0:
        return {}
    libre = ancho - gap * (len(items) - 1)
    # Cada uno a su altura máxima (cuota del alto); si no caben, todos a menos.
    anchos = [cuota * alto * w / h for _c, w, h, cuota in items]
    factor = min(1.0, libre / sum(anchos))
    sobra = max(0.0, libre - sum(anchos) * factor)
    entre = min(sobra / (len(items) - 1), ancho * aire) if len(items) > 1 else 0.0
    x = x0 + (sobra - entre * (len(items) - 1)) / 2
    salida = {}
    for (clave, w, h, cuota), ancho_i in zip(items, anchos):
        ancho_i *= factor
        alto_i = ancho_i * h / w
        y = y0 + (alto - alto_i) / 2
        salida[clave] = (x, y, x + ancho_i, y + alto_i)
        x += ancho_i + gap + entre
    return salida


def _componer(
    elementos, cajas_campo, dentro, origen, zona, size, extras=(), sangrado=None, aspecto=None,
    fuera=frozenset(),
):
    """Composición por roles con la retícula de cada plataforma.

    Parámetros de las guías de Meta y Google para piezas de producto: el
    producto es el protagonista; el logo de la marca siempre visible y en su
    esquina; la oferta junto al producto y con cuerpo para leerse en móvil;
    los sellos agrupados; el mensaje (titular y CTA) en un bloque propio, con
    el CTA como botón; y nada de texto en las zonas que tapa la interfaz (el
    área segura ya las excluye; el producto, que no es texto, puede bajar
    hasta ``sangrado``).

    La familia sale del área segura, no del lienzo: un Reel de 1080x1920 con
    un 35 % reservado abajo deja un área casi cuadrada. Y el hueco del
    producto sigue su forma (``aspecto``, ancho/alto de lo que se va a
    pintar): un televisor va en una franja a lo ancho, una refrigeradora en
    una columna. Metidos al revés, cualquiera de los dos salía diminuto.

    ``fuera`` son elementos que no se pintan (la pastilla de una fila sin
    precio): no se les guarda sitio, y el nombre del producto, si viene en
    ``extras``, pasa al bloque de mensaje.
    """

    papel = roles(elementos, cajas_campo, dentro, origen)
    if papel is None:
        return None
    zx0, zy0, zx1, zy1 = zona
    zw, zh = zx1 - zx0, zy1 - zy0
    gap = min(zw, zh) * .035
    destinos: list = [None] * len(elementos)
    campos: dict[str, tuple[float, float, float, float]] = {}
    proporcion = zw / zh
    formato = (
        "banner" if proporcion > 3.2 else "horizontal" if proporcion > 1.45
        else "vertical" if proporcion < .62 else "columnas"
    )
    nada = (0.0, 0.0, 0.0, 0.0)
    oferta = papel["offer"]
    if oferta in fuera:
        destinos[oferta] = nada
        oferta = None
    producto = papel["product"]
    pantalla = sangrado is not None and sangrado > zy1
    suelo_producto = max(zy1, sangrado or zy1)
    # El precio anterior y el descuento son parte de la oferta: tachado bajo
    # la pastilla y una insignia en su esquina. Sueltos en el bloque de
    # mensaje, "$500" y "10%" quedaban como líneas perdidas.
    junto_oferta = (
        {"previous_price", "discount"} & set(extras)
        if oferta is not None and formato != "banner" else set()
    )
    extras = tuple(c for c in extras if c not in junto_oferta)
    satelite: dict[str, tuple[float, float]] = {}
    if "legal" in extras and formato != "banner":
        # El legal, en su franja al pie del área segura; nada la pisa. Antes
        # caía en la retícula genérica encima de la vigencia.
        alto_legal = max(zh * .045, 10.0)
        campos["legal"] = (zx0, zy1 - alto_legal, zx1, zy1)
        zy1 = zy1 - alto_legal - gap * .6
        zh = zy1 - zy0
        zona = (zx0, zy0, zx1, zy1)
        suelo_producto = zy1
    if aspecto is None and producto is not None:
        aspecto = (producto[2] - producto[0]) / max(1, producto[3] - producto[1])
    aspecto = min(3.2, max(.3, aspecto or 1.0))
    ancho_producto = aspecto >= 1.25

    def natural(indice):
        caja = elementos[indice]
        return caja[2] - caja[0], caja[3] - caja[1]

    sellos = sorted(papel["seals"], key=lambda i: elementos[i][0])
    if sellos:
        alto_s = max(natural(i)[1] for i in sellos)
        ancho_s = sum(natural(i)[0] for i in sellos) + alto_s * .18 * (len(sellos) - 1)
    else:
        alto_s = ancho_s = 0.0

    def natural_oferta():
        w, h = natural(oferta)
        return (w, h * 1.24) if "previous_price" in junto_oferta else (w, h)

    def poner_oferta(caja):
        if caja is None or oferta is None:
            return
        if "previous_price" in junto_oferta:
            alto = (caja[3] - caja[1]) / 1.24
            destinos[oferta] = (caja[0], caja[1], caja[2], caja[1] + alto)
            satelite["previous_price"] = (caja[1] + alto * 1.03, caja[3])
        else:
            destinos[oferta] = caja

    def poner(indice, caja, alinear=("center", "center")):
        if indice is not None and caja is not None:
            destinos[indice] = _encajar(elementos[indice], caja, alinear)

    def poner_sellos(caja, alinear="center"):
        if caja is None or not sellos:
            return
        # La fila entera se encaja primero: así respeta el ancho del marco.
        fila = _encajar((0, 0, ancho_s, alto_s), caja, (alinear, "center"))
        if fila is None:
            return
        separa = (fila[3] - fila[1]) * .18
        for indice, destino in _fila(sellos, elementos, fila, separa, "center").items():
            destinos[indice] = destino

    def poner_bloque(caja, modo):
        forma = _bloque(extras, modo)
        if caja is None or forma is None:
            return
        base_w, base_h, partes = forma
        caja = _encajar((0, 0, base_w, base_h), caja, ("center", "center"))
        if caja is None:
            return
        factor = (caja[2] - caja[0]) / base_w
        for clave, parte in partes.items():
            campos[clave] = (
                caja[0] + parte[0] * factor, caja[1] + parte[1] * factor,
                caja[0] + parte[2] * factor, caja[1] + parte[3] * factor,
            )

    def marca_en(marco, alinear=("end", "start")):
        for indice in papel["brand"]:
            destinos[indice] = _encajar(elementos[indice], marco, alinear)
        bajos = [destinos[i][3] for i in papel["brand"] if destinos[i]]
        return max(bajos) + gap if bajos else marco[1]

    def cerrar():
        for indice, destino in enumerate(destinos):
            if destino is None and indice in sellos:
                destinos[indice] = nada
        plan = _cerrar(destinos, campos, elementos, cajas_campo, dentro)
        if plan is None or not junto_oferta or destinos[oferta] is None:
            return plan
        ox0, oy0, ox1, oy1 = destinos[oferta]
        precios = [campos[c] for c in ("price", "installment") if c in campos]
        px0 = min((c[0] for c in precios), default=ox0)
        px1 = max((c[2] for c in precios), default=ox1)
        if "previous_price" in satelite:
            # Justo bajo la parte de color de la pastilla, no bajo su caja: los
            # iconos que cuelgan de ella lo dejaban despegado del precio.
            alto_tachado = (oy1 - oy0) * .17
            base_precio = max((c[3] for c in precios), default=oy1)
            y0 = min(base_precio + (oy1 - oy0) * .07, satelite["previous_price"][0])
            campos["previous_price"] = (px0, y0, px1, y0 + alto_tachado)
        if "discount" in junto_oferta:
            # Fuera de la franja del nombre, colgando de la esquina: encima
            # tapaba el final de "AIR FRYER OSTER".
            lado = (oy1 - oy0) * .40
            x0 = min(ox1 - lado * .45, zona[2] - lado)
            y0 = max(oy0 - lado * .62, zona[1])
            campos["discount"] = (x0, y0, x0 + lado, y0 + lado)
        return plan

    if formato == "banner":
        # Leaderboard y banner móvil: una hilera a toda la altura. Los sellos
        # no se leen a 50-90 px y se omiten, como en cualquier banner retail.
        items = [("lockup", *natural(papel["lockup"]), 1.0)]
        if producto is not None:
            items.append(("product", min(2.4, aspecto) * 100, 100.0, 1.0))
        if oferta is not None:
            items.append(("offer", *natural(oferta), 1.0))
        if "product_name" in extras:
            items.append(("product_name", 4.0, 1.0, .5))
        if "cta" in extras:
            items.append(("cta", 3.8, 1.0, .52))
        for indice in papel["brand"]:
            w_b, h_b = natural(indice)
            # Un logotipo apaisado a media altura se comía un tercio del banner.
            items.append((("brand", indice), w_b, h_b, min(.5, zw * .2 * h_b / (zh * w_b))))
        # Repartidos a lo largo del banner: centrados dejaban dos tercios
        # vacíos a los lados en un 728x90.
        for clave, caja in _hilera(items, zona, gap, aire=.25).items():
            if clave == "lockup":
                destinos[papel["lockup"]] = caja
            elif clave == "offer":
                destinos[oferta] = caja
            elif clave in {"product", "cta", "product_name"}:
                campos[clave] = caja
            elif isinstance(clave, tuple):
                destinos[clave[1]] = caja
        for indice in sellos:
            destinos[indice] = nada
        return cerrar()

    if formato == "vertical":
        # Media página y rascacielos (300x600, 160x600): una sola columna,
        # centrada, que se llena de arriba abajo.
        items = []
        for indice in papel["brand"]:
            items.append((("brand", indice), *natural(indice), .06))
        items.append(("lockup", *natural(papel["lockup"]), .26))
        if producto is not None:
            items.append(("product", aspecto * 100, 100.0, .34))
        if oferta is not None:
            items.append(("offer", *natural_oferta(), .20))
        forma = _bloque(extras, "columna")
        if forma is not None:
            items.append(("bloque", forma[0], forma[1], .16))
        if sellos:
            items.append(("sellos", ancho_s, alto_s, .10))
        for clave, caja in _pila(items, zona, gap * .8).items():
            if clave == "lockup":
                destinos[papel["lockup"]] = caja
            elif clave == "offer":
                poner_oferta(caja)
            elif clave == "product":
                # El hueco toma el ancho entero de la columna.
                campos["product"] = (zx0, caja[1], zx1, caja[3])
            elif clave == "bloque":
                poner_bloque(caja, "columna")
            elif clave == "sellos":
                poner_sellos(caja)
            elif isinstance(clave, tuple):
                destinos[clave[1]] = caja
        return cerrar()

    if formato == "horizontal":
        # 1,91:1 y 16:9: identidad a la izquierda (logo de campaña sobre los
        # sellos), oferta y mensaje al centro, producto a la derecha; más
        # ancho si el producto es apaisado.
        ancho_p = zw * (.44 if ancho_producto else .36)
        techo = marca_en((zx1 - zw * .20, zy0, zx1, zy0 + zh * .11))
        columna_p = (zx1 - ancho_p, techo, zx1, suelo_producto)
        izquierda = (zx0, zy0, zx0 + zw * (.27 if ancho_producto else .29), zy1)
        centro = (izquierda[2] + gap, zy0, columna_p[0] - gap, zy1)
        items = [("lockup", *natural(papel["lockup"]), .72)]
        if sellos:
            items.append(("sellos", ancho_s, alto_s, .26))
        for clave, caja in _pila(items, izquierda, gap).items():
            if clave == "lockup":
                destinos[papel["lockup"]] = caja
            else:
                poner_sellos(caja)
        items = []
        if oferta is not None:
            items.append(("offer", *natural_oferta(), .62 if extras else 1.0))
        forma = _bloque(extras, "columna")
        if forma is not None:
            items.append(("bloque", forma[0], forma[1], .42))
        for clave, caja in _pila(items, centro, gap).items():
            if clave == "offer":
                poner_oferta(caja)
            else:
                poner_bloque(caja, "columna")
        if producto is not None:
            campos["product"] = columna_p
        return cerrar()

    vertical = zh / zw > 1.12
    techo = marca_en((zx1 - zw * (.26 if vertical else .22), zy0, zx1, zy0 + zh * .10))

    if ancho_producto and producto is not None:
        # Producto apaisado (un televisor, un combo): franja a todo el ancho.
        # Arriba el logo de campaña y la oferta lado a lado; abajo el mensaje.
        # En pantalla completa la franja baja por la zona que tapa la
        # interfaz, que no admite texto pero sí imagen.
        alto_arriba = zh * (.40 if pantalla or min(size) < 420 else .34 if not extras else .30)
        arriba = (zx0, zy0, zx1, zy0 + alto_arriba)
        poner(papel["lockup"], (zx0, zy0, zx0 + zw * .48, arriba[3]), ("start", "center"))
        derecha = (zx0 + zw * .52, techo, zx1, arriba[3])
        forma = _bloque(extras, "fila" if not pantalla else "columna")
        y = arriba[3] + gap
        if pantalla:
            # Arriba el logo de campaña con los sellos debajo, y la oferta; el
            # mensaje a todo el ancho; el producto, todo lo que queda hasta
            # el sangrado.
            items = [("lockup", *natural(papel["lockup"]), .75)]
            if sellos:
                items.append(("sellos", ancho_s, alto_s, .22))
            destinos[papel["lockup"]] = None
            for clave, caja in _pila(items, (zx0, zy0, zx0 + zw * .48, arriba[3]), gap * .6).items():
                if clave == "lockup":
                    destinos[papel["lockup"]] = caja
                else:
                    poner_sellos(caja)
            poner_oferta(_encajar((0, 0, *natural_oferta()), derecha, ("end", "center")))
            if forma is not None:
                alto_bloque = min(zw * forma[1] / forma[0], zh * .26)
                poner_bloque((zx0, y, zx1, y + alto_bloque), "columna")
                y += alto_bloque + gap
            campos["product"] = (zx0, y, zx1, suelo_producto)
            return cerrar()
        poner_oferta(_encajar((0, 0, *natural_oferta()), derecha, ("end", "center")))
        abajo = zy1
        if oferta is None and forma is not None:
            # Sin pastilla, el mensaje ocupa su sitio junto al logo de campaña.
            poner_bloque(derecha, "columna")
            forma = None
        if forma is not None:
            alto_franja = min(zw * forma[1] / forma[0], zh * .18)
            poner_bloque((zx0, zy1 - alto_franja, zx1, zy1), "fila")
            abajo = zy1 - alto_franja - gap
        if sellos:
            alto_sellos = zh * .12
            poner_sellos((zx0, abajo - alto_sellos, zx1, abajo), "start" if forma is None else "center")
            abajo -= alto_sellos + gap
        campos["product"] = (zx0, y, zx1, abajo)
        return cerrar()

    # Cuadrado, 4:5 y pantallas completas con un producto alto: dos columnas
    # —identidad y oferta a la izquierda, producto a la derecha—. El mensaje
    # de la fila (titular y botón) va en una franja a todo el ancho abajo; en
    # pantalla completa, en la columna izquierda, para que el producto pueda
    # seguir bajando por la franja que tapa la interfaz.
    base = zy1
    # Sin pastilla, el mensaje va en la columna izquierda, donde iba ella.
    en_columna = pantalla or oferta is None
    forma = None if en_columna else _bloque(extras, "fila")
    if forma is not None:
        alto_franja = min(zw * forma[1] / forma[0], zh * (.24 if vertical else .20))
        poner_bloque((zx0, zy1 - alto_franja, zx1, zy1), "fila")
        base = zy1 - alto_franja - gap * 1.4
        suelo_producto = base
    arriba = zy0
    if pantalla:
        # El logo de campaña encabeza a todo el ancho, bajo el de la marca: en
        # una columna de medio ancho quedaba diminuto en 1920 de alto.
        poner(papel["lockup"], (zx0, techo, zx1, techo + zh * .36), ("center", "start"))
        arriba = techo = destinos[papel["lockup"]][3] + gap
    # En un 300x250 la pastilla a media columna dejaba el nombre y la cuota a
    # 6 px: en lienzos pequeños la oferta se lleva más ancho que el logo.
    pequeno = min(size) < 420
    izquierda = (zx0, arriba, zx0 + zw * (.56 if pequeno else .47), base)
    columna_p = (izquierda[2] + gap, techo, zx1, suelo_producto)
    items = [] if pantalla else [("lockup", *natural(papel["lockup"]), .30 if pequeno else .40)]
    if oferta is not None:
        items.append(("offer", *natural_oferta(), .46 if pequeno else .34))
    columna = _bloque(extras, "columna") if en_columna else None
    if columna is not None:
        items.append(("bloque", columna[0], columna[1], .24 if oferta is not None else .40))
    if sellos:
        items.append(("sellos", ancho_s, alto_s, .14))
    for clave, caja in _pila(items, izquierda, gap).items():
        if clave == "lockup":
            destinos[papel["lockup"]] = caja
        elif clave == "offer":
            poner_oferta(caja)
        elif clave == "bloque":
            poner_bloque(caja, "columna")
        else:
            poner_sellos(caja)
    if producto is not None:
        campos["product"] = columna_p
    return cerrar()


def _cerrar(destinos, campos, elementos, cajas_campo, dentro):
    """Los campos de la oferta viajan con su pastilla; lo omitido no se pinta."""

    for hueco, caja in cajas_campo.items():
        if hueco in campos:
            continue
        indice = dentro.get(hueco)
        if indice is None or destinos[indice] is None:
            continue
        original, nueva = elementos[indice], destinos[indice]
        factor = (nueva[2] - nueva[0]) / max(1, original[2] - original[0])
        x0 = nueva[0] + (caja[0] - original[0]) * factor
        y0 = nueva[1] + (caja[1] - original[1]) * factor
        campos[hueco] = (x0, y0, x0 + (caja[2] - caja[0]) * factor, y0 + (caja[3] - caja[1]) * factor)
    if any(d is None for d in destinos):
        return None
    return destinos, campos


def _reflujo(elementos, cajas_campo, dentro, quitar, origen, zona, omit):
    """Horizontal: el producto a la derecha y la identidad en filas a su izquierda.

    Los elementos pequeños pegados a una esquina (el logo de la marca arriba a
    la derecha) conservan su esquina. El resto se coloca en filas, en el orden
    en que se leen en el original, con la escala más grande que quepa.
    """

    zx0, zy0, zx1, zy1 = zona
    zw, zh = zx1 - zx0, zy1 - zy0
    ancho0, alto0 = origen
    destinos: list = [None] * len(elementos)
    campos: dict[str, tuple[float, float, float, float]] = {}

    esquinas = []
    resto = []
    for indice, caja in enumerate(elementos):
        if indice in quitar:
            continue
        area = _area(caja) / (ancho0 * alto0)
        cerca_x = caja[0] < ancho0 * .2 or caja[2] > ancho0 * .8
        cerca_y = caja[1] < alto0 * .2 or caja[3] > alto0 * .8
        if area < .06 and cerca_x and cerca_y:
            esquinas.append(indice)
        else:
            resto.append(indice)
    escala_e = min(zw / ancho0, zh / alto0) * 1.4
    for indice in esquinas:
        destinos[indice] = _mover(elementos[indice], origen, zona, escala_e)

    producto = cajas_campo.get("product") if "product" not in omit else None
    derecha = zx1
    if producto is not None and dentro.get("product") is None:
        # El producto ocupa la columna derecha, por debajo o por encima de lo
        # que ya vive en esas esquinas (el logo de la marca).
        techo = max(
            [zy0] + [destinos[i][3] + zh * .03 for i in esquinas
                     if destinos[i][2] > zx1 - zw * .4 and destinos[i][1] < zy0 + zh / 2]
        )
        suelo = min(
            [zy1] + [destinos[i][1] - zh * .03 for i in esquinas
                     if destinos[i][2] > zx1 - zw * .4 and destinos[i][1] >= zy0 + zh / 2]
        )
        pw, ph = producto[2] - producto[0], producto[3] - producto[1]
        escala_p = min((suelo - techo) / ph, zw * .38 / pw)
        w, h = pw * escala_p, ph * escala_p
        medio = (techo + suelo) / 2
        campos["product"] = (zx1 - w, medio - h / 2, zx1, medio + h / 2)
        derecha = zx1 - w - zw * .03

    resto.sort(key=lambda i: (elementos[i][1], elementos[i][0]))
    izquierda_w = derecha - zx0
    if not resto or izquierda_w <= 0:
        return None

    def filas(escala):
        gap = zw * .025
        lineas, actual, ancho_actual = [], [], 0.0
        for indice in resto:
            w = (elementos[indice][2] - elementos[indice][0]) * escala
            if actual and ancho_actual + gap + w > izquierda_w:
                lineas.append(actual)
                actual, ancho_actual = [], 0.0
            actual.append(indice)
            ancho_actual += (gap if len(actual) > 1 else 0) + w
            if w > izquierda_w:
                return None
        if actual:
            lineas.append(actual)
        alto_total = sum(
            max((elementos[i][3] - elementos[i][1]) * escala for i in linea) for linea in lineas
        ) + gap * (len(lineas) - 1)
        return (lineas, alto_total, gap) if alto_total <= zh else None

    bajo, alto_e, mejor = .05, 3.0, None
    for _ in range(24):
        medio = (bajo + alto_e) / 2
        resultado = filas(medio)
        if resultado is None:
            alto_e = medio
        else:
            bajo, mejor = medio, (medio, resultado)
    if mejor is None:
        return None
    escala, (lineas, alto_total, gap) = mejor
    y = zy0 + (zh - alto_total) / 2
    for linea in lineas:
        alto_linea = max((elementos[i][3] - elementos[i][1]) * escala for i in linea)
        ancho_linea = sum((elementos[i][2] - elementos[i][0]) * escala for i in linea) + gap * (len(linea) - 1)
        x = zx0 + (izquierda_w - ancho_linea) / 2
        for indice in linea:
            caja = elementos[indice]
            w, h = (caja[2] - caja[0]) * escala, (caja[3] - caja[1]) * escala
            destinos[indice] = (x, y + (alto_linea - h) / 2, x + w, y + (alto_linea + h) / 2)
            x += w + gap
        y += alto_linea + gap

    for hueco, caja in cajas_campo.items():
        if hueco in campos or hueco in omit:
            continue
        indice = dentro.get(hueco)
        if indice is None or destinos[indice] is None:
            continue
        original, nueva = elementos[indice], destinos[indice]
        factor = (nueva[2] - nueva[0]) / max(1, original[2] - original[0])
        x0 = nueva[0] + (caja[0] - original[0]) * factor
        y0 = nueva[1] + (caja[1] - original[1]) * factor
        campos[hueco] = (x0, y0, x0 + (caja[2] - caja[0]) * factor, y0 + (caja[3] - caja[1]) * factor)
    destinos = [
        d if d is not None else (0.0, 0.0, 0.0, 0.0) for d in destinos
    ]
    return destinos, campos


def field_boxes(pieza: Pieza, escala: float) -> tuple[dict[str, tuple[int, int, int, int]], list[dict]]:
    """Qué campo es cada caja variable, en píxeles de la placa."""

    from .campaign_layout_from_art import clasificar

    size = (int(round(pieza.width * escala)), int(round(pieza.height * escala)))
    lecturas = reads_for(pieza, escala)
    cajas = dict(clasificar(lecturas, size))
    producto = product_box(pieza, escala)
    if producto is not None:
        cajas["product"] = producto
        # Junto a un producto, el texto de cuerpo pequeño que el clasificador
        # toma por titular es su nombre: "REFRIGERADORA TOP MOUNT".
        titular = cajas.get("headline")
        if titular is not None and "product_name" not in cajas:
            lectura = next((l for l in lecturas if tuple(l["bbox"]) == tuple(titular)), None)
            if lectura is not None and float(lectura.get("size") or 0) < size[1] * .06:
                cajas["product_name"] = cajas.pop("headline")
    return cajas, lecturas


def ensanchar(capa, cajas: dict[str, tuple[int, int, int, int]]) -> dict[str, tuple[int, int, int, int]]:
    """Cada texto de una pastilla, al ancho de su franja de color.

    La caja medida es la del texto del ejemplo: "NOMBRE DEL PRODUCTO" ocupa
    dos tercios de la franja celeste. Un nombre más largo, o la misma
    pastilla en un 300x250, no cabía y salía a 6 px o cortado. La franja es
    el sitio real: se busca hacia los lados mientras el color siga siendo el
    de detrás del texto (en la capa ya no hay texto), y se deja un respiro.
    """

    import numpy as np

    pixeles = np.asarray(capa.convert("RGBA"), dtype=np.int16)
    alto, ancho = pixeles.shape[:2]
    salida = dict(cajas)
    for hueco, caja in cajas.items():
        if hueco == "product":
            continue
        x0, y0, x1, y1 = (int(v) for v in caja)
        if x1 - x0 < 4 or y1 - y0 < 2:
            continue
        filas = sorted({min(alto - 1, max(0, y)) for y in (y0 + (y1 - y0) // 4, (y0 + y1) // 2, y1 - (y1 - y0) // 4)})
        izquierda, derecha = [], []
        for y in filas:
            fila = pixeles[y]
            centro = min(ancho - 1, max(0, (x0 + x1) // 2))
            referencia = fila[centro]
            if referencia[3] < 200:
                break

            def igual(x):
                pixel = fila[x]
                return pixel[3] >= 200 and int(np.abs(pixel[:3] - referencia[:3]).sum()) < 60

            x = centro
            while x > 0 and igual(x - 1):
                x -= 1
            izquierda.append(x)
            x = centro
            while x < ancho - 1 and igual(x + 1):
                x += 1
            derecha.append(x + 1)
        if len(izquierda) != len(filas):
            continue
        franja_x0, franja_x1 = max(izquierda), min(derecha)
        respiro = (franja_x1 - franja_x0) * .07
        nuevo_x0, nuevo_x1 = int(franja_x0 + respiro), int(franja_x1 - respiro)
        salida[hueco] = (min(x0, nuevo_x0), y0, max(x1, nuevo_x1), y1)
    return salida


#: Medida de cada familia y el preset del que sale su área segura.
FAMILIES = {
    "portrait": ((1280, 1600), "meta_feed_4_5"),
    "square": ((1600, 1600), "meta_feed_square"),
    "story": ((900, 1600), "meta_stories"),
    "landscape": ((1600, 838), "meta_feed_landscape"),
}
#: Sube cuando cambia lo que se extrae: las campañas ya analizadas se rehacen.
VERSION = 7


def build_templates(pdf_path: Path, folder: Path) -> dict:
    """Placas y posiciones para las cuatro familias desde un editable.

    Devuelve un resumen siempre; ``plates`` vacío cuando el documento no trae
    ninguna pieza que sirva de plantilla (un catálogo, un manual).
    """

    from ..models.formats import DEFAULT_SAFE_AREA, FORMAT_PRESETS
    from .campaign_layout_from_art import aspect_key, normalize_boxes

    fitz = _fitz()
    doc = fitz.open(pdf_path)
    try:
        piezas = find_pieces(doc)
    finally:
        doc.close()
    resumen: dict = {
        "version": VERSION,
        "pieces": len(piezas),
        "catalog_pieces": sum(1 for pieza in piezas if pieza.catalog),
        "plates": [],
        "placements": {},
        "text_colors": {},
        "text_reads": [],
    }
    elegidas = best_pieces(piezas)
    if not elegidas:
        return resumen

    def seguro(preset: str) -> dict[str, float]:
        crudo = FORMAT_PRESETS.get(preset, {}).get("safe_area", DEFAULT_SAFE_AREA)
        return {clave: float(valor) for clave, valor in crudo.items()}

    principal = elegidas[0]
    propias: dict[str, Pieza] = {}
    for pieza in elegidas:
        propias.setdefault(aspect_key((int(pieza.width), int(pieza.height))), pieza)

    escala_base = PLATE_MAX_SIDE / max(principal.width, principal.height)
    fondo_base, capa_base = decompose(pdf_path, principal, escala_base)
    cajas_base, lecturas_base = field_boxes(principal, escala_base)
    cajas_base = ensanchar(capa_base, cajas_base)
    if "product" in cajas_base:
        # El hueco medido es la silueta del producto del ejemplo: se amplía al
        # espacio libre para que cualquier otro producto quepa con cuerpo.
        medida_base = capa_base.size
        margen_base = min(medida_base) * .035
        cajas_base["product"] = ampliar(
            cajas_base["product"],
            components(capa_base) + [c for h, c in cajas_base.items() if h != "product"],
            (margen_base, margen_base, medida_base[0] - margen_base, medida_base[1] - margen_base),
            min(medida_base) * .03,
        )
    descompuesta = (fondo_base, capa_base, cajas_base, lecturas_base)
    # La descomposición se guarda: el renderer recompone cada medida exacta
    # (320x50, 1280x720…) en vez de recortar la placa de la familia más
    # parecida, que cortaba el logo por los lados.
    folder.mkdir(parents=True, exist_ok=True)
    if fondo_base is not None:
        fondo_base.convert("RGB").save(folder / "vector-fondo.jpg", format="JPEG", quality=92)
    capa_base.save(folder / "vector-capa.png", format="PNG", optimize=True)
    resumen["decomposition"] = {
        "fondo": "vector-fondo.jpg" if fondo_base is not None else "",
        "capa": "vector-capa.png",
        "fields": {hueco: list(caja) for hueco, caja in cajas_base.items()},
    }
    for familia, (medida, preset) in FAMILIES.items():
        safe = seguro(preset)
        pieza = propias.get(familia)
        destino = folder / f"vector-plate-{familia}.png"
        if pieza is not None:
            # La pieza dibujada para esta proporción: se renderiza tal cual.
            ancho, alto, escala = render_plate(pdf_path, pieza, destino)
            cajas, lecturas = field_boxes(pieza, escala)
            size = (ancho, alto)
            origen = "piece"
            if pieza is principal and capa_base.size == size:
                cajas = ensanchar(capa_base, cajas)
            if "product" in cajas and pieza is principal:
                # El hueco medido es la silueta del producto del ejemplo (una
                # refrigeradora alta y estrecha): un cilindro o una licuadora
                # quedaban diminutos. Se amplía al espacio libre que lo rodea.
                obstaculos = [
                    caja for caja in components(descompuesta[1])
                ] + [caja for hueco, caja in cajas.items() if hueco != "product"]
                margen = min(size) * .035
                cajas["product"] = ampliar(
                    cajas["product"], obstaculos,
                    (margen, margen, size[0] - margen, size[1] - margen), min(size) * .03,
                )
        else:
            # Ninguna pieza en esta proporción: se recompone la principal.
            fondo, capa, cajas_base, lecturas = descompuesta
            placa, cajas = adapt(fondo, capa, cajas_base, medida, safe)
            destino.parent.mkdir(parents=True, exist_ok=True)
            placa.save(destino, format="PNG", optimize=True)
            size = medida
            origen = "adapted"
        posiciones = normalize_boxes(cajas, size, safe)
        if posiciones:
            resumen["placements"][familia] = {
                hueco: caja.model_dump(mode="json") for hueco, caja in posiciones.items()
            }
        # Variante sin la pastilla del precio, para las filas que no traen
        # precio ni cuota: una pastilla vacía en la pieza final parece un error.
        desnuda = None
        if {"price", "installment"} & set(cajas_base):
            fondo, capa, cajas_b, _l = descompuesta
            sin_margen = {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}
            placa_b, _cajas = adapt(
                fondo, capa, cajas_b, size,
                sin_margen if origen == "piece" else safe,
                omit={"price", "installment"},
            )
            desnuda = folder / f"vector-plate-{familia}-sin-precio.png"
            placa_b.save(desnuda, format="PNG", optimize=True)
        resumen["plates"].append({
            "family": familia,
            "bare_file": desnuda.name if desnuda else "",
            "file": destino.name,
            "size": [size[0], size[1]],
            "origin": origen,
            "page": (pieza or principal).page + 1,
        })
        if familia == aspect_key((int(principal.width), int(principal.height))) or not resumen["text_reads"]:
            resumen["text_reads"] = lecturas[:60]
    # El color con que el diseñador escribió cada campo, desde el texto real.
    escala = PLATE_MAX_SIDE / max(principal.width, principal.height)
    cajas, lecturas = field_boxes(principal, escala)
    por_caja = {tuple(l["bbox"]): l for l in lecturas}
    for hueco, caja in cajas.items():
        lectura = por_caja.get(tuple(caja))
        if lectura and re.match(r"^#[0-9A-F]{6}$", str(lectura.get("color", ""))):
            resumen["text_colors"][hueco] = lectura["color"]
    # Si el diseñador escribió el campo en mayúsculas ("REFRIGERADORA TOP
    # MOUNT"), la fila "cilindro" también debe salir así.
    resumen["text_case"] = {
        hueco: "upper"
        for hueco, caja in cajas.items()
        if tuple(caja) in por_caja
        and any(c.isalpha() for c in por_caja[tuple(caja)]["text"])
        and por_caja[tuple(caja)]["text"] == por_caja[tuple(caja)]["text"].upper()
    }
    resumen["fonts"] = {
        hueco: por_caja[tuple(caja)]["font"]
        for hueco, caja in cajas.items()
        if tuple(caja) in por_caja and por_caja[tuple(caja)].get("font")
    }
    return resumen


def is_vector_source(path: Path) -> int | None:
    """Desplazamiento del PDF dentro del archivo, o ``None`` si no lo hay."""

    try:
        with path.open("rb") as handle:
            head = handle.read(4 * 1024 * 1024)
    except OSError:
        return None
    offset = head.find(b"%PDF-")
    return offset if offset >= 0 else None


__all__ = [
    "FAMILIES", "Pieza", "VERSION", "adapt", "best_pieces", "build_templates",
    "decompose", "field_boxes", "find_pieces", "is_vector_source", "product_box",
    "reads_for", "render_plate",
]
