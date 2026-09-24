"""Lectura y normalización de la matriz de producción.

La hoja que entrega el cliente es una *orden de producción*, no una tabla
interna del renderer. Por eso este módulo acepta cabeceras habituales en
español/inglés y conserva una distinción importante:

* una celda vacía permite que el brief/IA complete el campo;
* ``no poner`` u ``omitir`` prohíbe que ese campo aparezca.

Mantener esta lógica fuera del navegador evita que un CSV produzca una tanda
distinta según quién lo abra y permite validarlo antes de gastar en imágenes.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pydantic import BaseModel, Field


class MatrixParseError(ValueError):
    """La matriz no se puede interpretar sin adivinar silenciosamente."""


class MatrixRow(BaseModel):
    """Una solicitud de arte normalizada, lista para planificar la tanda."""

    row_number: int = Field(ge=2)
    producto: str = ""
    imagen: str | None = None
    titular: str | None = None
    subtitulo: str | None = None
    precio_anterior: str | None = None
    precio_actual: str | None = None
    cuota: str | None = None
    descuento: str | None = None
    cta: str | None = None
    legal: str | None = None
    vigencia: str | None = None
    formatos: list[str] = Field(default_factory=list)
    cantidad_propuestas: int = Field(default=1, ge=1, le=6)
    plantilla: str | None = None
    notas: str | None = None
    # Campos que el usuario pidió excluir de forma explícita. Una lista (y no
    # un set) mantiene JSON estable y facilita mostrarla en la revisión.
    suppressed_fields: list[str] = Field(default_factory=list)
    # Campos que redactó la IA y no la persona. Sobre la plantilla del
    # editable, lo escrito a mano siempre se dibuja; lo redactado solo si el
    # arte tiene sitio para ello.
    ai_fields: list[str] = Field(default_factory=list)


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


ALIASES: dict[str, set[str]] = {
    "producto": {"producto", "product", "nombre_producto", "nombre", "sku"},
    "imagen": {"imagen", "image", "foto", "archivo", "file", "product_image"},
    "titular": {"titular", "headline", "copy", "titulo"},
    "subtitulo": {"subtitulo", "subtitle", "subheadline", "bajada"},
    "precio_anterior": {
        "precio_anterior",
        "old_price",
        "previous_price",
        "precio_antes",
        "pvp",
    },
    "precio_actual": {
        "precio_actual",
        "precio",
        "price",
        "current_price",
        "precio_oferta",
    },
    "cuota": {"cuota", "cuotas", "installment", "installments"},
    "descuento": {"descuento", "discount", "ahorro"},
    "cta": {"cta", "llamado", "llamado_a_la_accion", "call_to_action"},
    "legal": {"legal", "legales", "terminos", "terms", "restricciones"},
    "vigencia": {"vigencia", "validity", "fecha", "fechas"},
    "formatos": {"formatos", "formato", "formats", "format", "tamanos", "medidas"},
    "cantidad_propuestas": {
        "cantidad_propuestas",
        "propuestas",
        "cantidad",
        "variantes",
        "variants",
        "proposals",
        "count",
    },
    "plantilla": {"plantilla", "template"},
    "notas": {"notas", "notes", "instrucciones", "instruction"},
}

ALIAS_TO_FIELD = {
    alias: field
    for field, aliases in ALIASES.items()
    for alias in aliases
}

SUPPRESS_TOKENS = {
    "no_poner",
    "no_mostrar",
    "no_usar",
    "omitir",
    "suprimir",
    "sin_contenido",
}

OPTIONAL_TEXT_FIELDS = (
    "imagen",
    "titular",
    "subtitulo",
    "precio_anterior",
    "precio_actual",
    "cuota",
    "descuento",
    "cta",
    "legal",
    "vigencia",
    "plantilla",
    "notas",
)


def _decode(payload: bytes | str) -> str:
    if isinstance(payload, str):
        return payload.lstrip("\ufeff")
    encodings = ("utf-16", "utf-8-sig", "cp1252") if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1252")
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MatrixParseError("La matriz no usa una codificación de texto compatible.")


def _header_score(text: str, delimiter: str) -> tuple[int, int, int]:
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))[:6]
    except csv.Error:
        return (-1, -1, -1)
    if not rows:
        return (-1, -1, -1)
    known = sum(1 for cell in rows[0] if _key(cell) in ALIAS_TO_FIELD)
    width = len(rows[0])
    consistent = sum(1 for row in rows[1:] if len(row) == width)
    return known, consistent, width


def _delimiter(text: str) -> str:
    """Elige por cabeceras conocidas, no por signos dentro del contenido.

    ``csv.Sniffer`` suele confundir el ``;`` de ``feed;story`` con el
    delimitador de la hoja. Las cabeceras del contrato ofrecen una señal mucho
    más fuerte y determinista.
    """

    candidates = (",", "\t", ";")
    scores = {candidate: _header_score(text, candidate) for candidate in candidates}
    best = max(candidates, key=lambda candidate: scores[candidate])
    if scores[best][0] <= 0:
        raise MatrixParseError(
            "No se reconocen las cabeceras de la matriz. Incluye al menos "
            "producto, imagen, titular, precio, formatos o cantidad_propuestas."
        )
    return best


def _columns(header: Sequence[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, raw in enumerate(header):
        canonical = ALIAS_TO_FIELD.get(_key(raw))
        if canonical is None:
            continue
        if canonical in columns:
            raise MatrixParseError(f"La columna «{canonical}» aparece más de una vez.")
        columns[canonical] = index
    return columns


def _cell(cells: Sequence[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip()


def _optional(value: str, field: str, suppressed: list[str]) -> str | None:
    if not value:
        return None
    if _key(value) in SUPPRESS_TOKENS:
        suppressed.append(field)
        return None
    return value


def _formats(value: str, row_number: int) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[|;]", value):
        item = raw.strip()
        if not item:
            continue
        marker = item.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    if not result and value.strip():
        raise MatrixParseError(f"Fila {row_number}: formatos no contiene ninguna medida válida.")
    return result


def _quantity(value: str, row_number: int) -> int:
    if not value:
        return 1
    try:
        quantity = int(value)
    except ValueError as exc:
        raise MatrixParseError(
            f"Fila {row_number}: cantidad_propuestas debe ser un entero entre 1 y 6."
        ) from exc
    if not 1 <= quantity <= 6:
        raise MatrixParseError(
            f"Fila {row_number}: cantidad_propuestas debe estar entre 1 y 6."
        )
    return quantity


def parse_csv(payload: bytes | str) -> list[MatrixRow]:
    """Convierte CSV/TSV en filas canónicas sin completar contenido faltante."""

    text = _decode(payload)
    if not text.strip():
        return []
    delimiter = _delimiter(text)
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        header = next(reader, [])
        columns = _columns(header)
        result: list[MatrixRow] = []
        for row_number, cells in enumerate(reader, start=2):
            if not any(cell.strip() for cell in cells):
                continue
            suppressed: list[str] = []
            optional = {
                field: _optional(_cell(cells, columns.get(field)), field, suppressed)
                for field in OPTIONAL_TEXT_FIELDS
            }
            producto = _cell(cells, columns.get("producto"))
            if _key(producto) in SUPPRESS_TOKENS:
                producto = ""
                suppressed.append("producto")
            row = MatrixRow(
                row_number=row_number,
                producto=producto,
                **optional,
                formatos=_formats(_cell(cells, columns.get("formatos")), row_number),
                cantidad_propuestas=_quantity(
                    _cell(cells, columns.get("cantidad_propuestas")), row_number
                ),
                suppressed_fields=suppressed,
            )
            # Una línea con notas en una columna desconocida no es una orden de
            # arte. Se ignora, igual que una línea realmente vacía.
            meaningful = row.producto or row.imagen or any(
                getattr(row, field) is not None
                for field in OPTIONAL_TEXT_FIELDS
                if field not in {"imagen", "plantilla"}
            )
            if meaningful or row.formatos or row.suppressed_fields:
                result.append(row)
    except csv.Error as exc:
        raise MatrixParseError(f"CSV inválido: {exc}.") from exc
    return result


_SHEET_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _excel_column(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    if not letters:
        return 0
    result = 0
    for char in letters.group(0).upper():
        result = result * 26 + ord(char) - ord("A") + 1
    return max(0, result - 1)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ElementTree.ParseError as exc:
        raise MatrixParseError("El XLSX tiene sharedStrings.xml invalido.") from exc
    return [
        "".join(node.text or "" for node in item.findall(".//main:t", _SHEET_NS))
        for item in root.findall("main:si", _SHEET_NS)
    ]


def _xlsx_table(
    archive: zipfile.ZipFile, name: str, shared: list[str]
) -> list[list[str]]:
    try:
        root = ElementTree.fromstring(archive.read(name))
    except (KeyError, ElementTree.ParseError) as exc:
        raise MatrixParseError(f"No se pudo leer {Path(name).name} del XLSX.") from exc
    table: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", _SHEET_NS)[:20_001]:
        cells: dict[int, str] = {}
        for cell in row.findall("main:c", _SHEET_NS):
            column = _excel_column(cell.attrib.get("r", ""))
            kind = cell.attrib.get("t", "")
            if kind == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.findall(".//main:t", _SHEET_NS)
                )
            else:
                node = cell.find("main:v", _SHEET_NS)
                value = node.text or "" if node is not None else ""
                if kind == "s" and value:
                    try:
                        value = shared[int(value)]
                    except (ValueError, IndexError) as exc:
                        raise MatrixParseError(
                            "El XLSX referencia un texto compartido inexistente."
                        ) from exc
            cells[column] = value.strip()
        if not cells:
            continue
        width = max(cells) + 1
        table.append([cells.get(index, "") for index in range(width)])
    return table


def parse_xlsx(payload: bytes) -> list[MatrixRow]:
    """Lee la hoja que tenga más cabeceras reconocibles, sin depender de Excel."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise MatrixParseError("El archivo XLSX no es un ZIP de Office valido.") from exc
    with archive:
        members = archive.infolist()
        if len(members) > 2_000 or sum(max(0, item.file_size) for item in members) > 60 * 1024 * 1024:
            raise MatrixParseError("El XLSX declara demasiado contenido interno.")
        sheets = sorted(
            item.filename
            for item in members
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename)
        )
        if not sheets:
            raise MatrixParseError("El XLSX no contiene hojas de calculo.")
        shared = _xlsx_shared_strings(archive)
        best: tuple[int, list[list[str]]] | None = None
        for name in sheets:
            table = _xlsx_table(archive, name, shared)
            if not table:
                continue
            header_index = next(
                (
                    index
                    for index, row in enumerate(table[:20])
                    if any(_key(cell) in ALIAS_TO_FIELD for cell in row)
                ),
                0,
            )
            table = table[header_index:]
            score = sum(1 for cell in table[0] if _key(cell) in ALIAS_TO_FIELD)
            if best is None or score > best[0]:
                best = score, table
        if best is None or best[0] <= 0:
            raise MatrixParseError(
                "No se reconocen cabeceras de matriz en ninguna hoja del XLSX."
            )
        output = io.StringIO()
        csv.writer(output).writerows(best[1])
        return parse_csv(output.getvalue())


def parse_matrix(payload: bytes | str, filename: str = "matriz.csv") -> list[MatrixRow]:
    extension = Path(filename or "").suffix.casefold()
    if extension == ".xlsx":
        if isinstance(payload, str):
            raise MatrixParseError("El XLSX debe enviarse como archivo binario.")
        return parse_xlsx(payload)
    if extension not in {"", ".csv", ".tsv", ".txt"}:
        raise MatrixParseError("La matriz debe ser CSV, TSV o XLSX.")
    return parse_csv(payload)


def requested_piece_count(rows: Iterable[MatrixRow]) -> int:
    """Total exacto: formatos por fila × propuestas pedidas por esa fila.

    Una fila sin formatos todavía representa una salida en el formato que el
    flujo elija como predeterminado.
    """

    return sum(max(1, len(row.formatos)) * row.cantidad_propuestas for row in rows)


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _template_slots(template: Any) -> list[Any]:
    slots = _value(template, "slots", [])
    return list(slots or [])


def _slot_id(slot: Any) -> str:
    # Las plantillas aprobadas usan ``id``; las propuestas previas a aprobación
    # usan ``key``. El selector sirve en ambos lados de ese paso.
    return _key(str(_value(slot, "id", _value(slot, "key", ""))))


_AI_COPY_SLOT_ALIASES = {
    "headline": "titular",
    "title": "titular",
    "subheadline": "subtitulo",
    "subtitle": "subtitulo",
    "call_to_action": "cta",
}
_AI_COPY_FIELDS = frozenset({"titular", "subtitulo", "cta"})


def ai_fillable_fields(template: Any) -> set[str]:
    """Devuelve el copy que una plantilla aprobada autorizó a completar.

    El mismo cálculo lo consumen el plan previo y el worker. Así el modelo no
    escribe un CTA o un titular que no tenga una zona aprobada en la plantilla.
    """

    fields: set[str] = set()
    for slot in _template_slots(template):
        if not bool(_value(slot, "generate_if_missing", False)):
            continue
        key = _AI_COPY_SLOT_ALIASES.get(_slot_id(slot), _slot_id(slot))
        if key in _AI_COPY_FIELDS:
            fields.add(key)
    return fields


def product_count(row: MatrixRow) -> int:
    # Una celda puede describir un combo como ``TV | Soundbar``. No se parte
    # por ``+``: forma parte de muchos nombres comerciales y SKU.
    product_parts = [
        part.strip() for part in re.split(r"[|;]", row.producto) if part.strip()
    ]
    image_parts = [
        part.strip() for part in re.split(r"[|;]", row.imagen or "") if part.strip()
    ]
    if not product_parts and not image_parts:
        return 0
    return max(1, len(product_parts), len(image_parts))


def score_template(row: MatrixRow, template: Any) -> float:
    """Puntúa compatibilidad entre una fila y una plantilla/candidata.

    Es deliberadamente explicable: premia slots para los campos presentes,
    penaliza campos requeridos ausentes y descarta plantillas con menos huecos
    de producto que el combo. El selector visual/IA puede sumar señales después,
    pero nunca debería ignorar estas restricciones estructurales.
    """

    template_name = str(_value(template, "name", _value(template, "title", "")) or "")
    template_id = str(
        _value(template, "template_id", _value(template, "candidate_id", "")) or ""
    )
    if row.plantilla:
        forced = row.plantilla.casefold()
        if forced not in {template_name.casefold(), template_id.casefold()}:
            return float("-inf")

    slots = _template_slots(template)
    slot_ids = {_slot_id(slot) for slot in slots}
    product_slots = sum(
        1
        for slot in slots
        if _slot_id(slot).startswith("producto")
        or _key(str(_value(slot, "category", ""))) == "product"
    )
    # Una candidata de combo suele expresar «productos» como un único slot
    # repetible y declarar su capacidad (2–4) en el rango. Contarlo como un solo
    # hueco hacía que el selector rechazara precisamente la plantilla de combo.
    product_range = _value(template, "supported_product_count", {}) or {}
    range_max = int(
        _value(product_range, "maximum", _value(product_range, "max", product_slots))
        or product_slots
    )
    if any(bool(_value(slot, "repeatable", False)) for slot in slots):
        product_slots = max(product_slots, range_max)
    products = product_count(row)
    range_min = int(
        _value(product_range, "minimum", _value(product_range, "min", 1)) or 0
    )
    if products < range_min or products > range_max or product_slots < products:
        return float("-inf")

    score = 20.0 if products and product_slots == products else 0.0
    category = _key(str(_value(template, "category", "")))
    has_commercial_content = any(
        getattr(row, field) is not None
        for field in ("precio_anterior", "precio_actual", "cuota", "descuento")
    )
    # Una plantilla base puede aceptar todos los campos opcionales, pero cuando
    # existe una plantilla aprobada específicamente para oferta/combo/mensaje
    # conviene elegirla: conserva la flexibilidad sin convertir toda pieza en
    # la misma retícula genérica.
    if products >= 2 and category == "combo":
        score += 8.0
    elif products == 1 and has_commercial_content and category == "price_promotion":
        score += 6.0
    elif products == 0 and category == "institutional":
        score += 6.0
    for field in (
        "titular",
        "subtitulo",
        "precio_anterior",
        "precio_actual",
        "cuota",
        "descuento",
        "cta",
        "legal",
        "vigencia",
    ):
        value = getattr(row, field)
        aliases = {field}
        if field == "precio_actual":
            aliases.add("precio")
        if value is not None:
            # Una celda con contenido es una orden, no una sugerencia. Elegir
            # una plantilla sin ese slot haría que el renderer descartase en
            # silencio precio, CTA o legal. Es preferible pedir otra plantilla
            # aprobada (o corregir la matriz) a entregar un arte incompleto.
            if not aliases & slot_ids:
                return float("-inf")
            score += 3.0
        elif field in row.suppressed_fields and aliases & slot_ids:
            # No invalida: un slot opcional debe poder desaparecer, pero una
            # plantilla que no depende de él necesita menos reflow.
            score -= 0.25

    for slot in slots:
        slot_id = _slot_id(slot)
        if not bool(_value(slot, "required", False)) or slot_id.startswith("producto"):
            continue
        row_field = "precio_actual" if slot_id == "precio" else slot_id
        if not hasattr(row, row_field) or getattr(row, row_field) in (None, ""):
            score -= 25.0
    return score


def select_template(row: MatrixRow, templates: Sequence[Any]) -> Any | None:
    """Devuelve la plantilla compatible mejor puntuada, estable ante empates."""

    if not templates:
        return None
    ranked = [(score_template(row, template), index, template) for index, template in enumerate(templates)]
    compatible = [item for item in ranked if item[0] != float("-inf")]
    if not compatible:
        return None
    return max(compatible, key=lambda item: (item[0], -item[1]))[2]


__all__ = [
    "ai_fillable_fields",
    "MatrixParseError",
    "MatrixRow",
    "parse_csv",
    "parse_matrix",
    "parse_xlsx",
    "product_count",
    "requested_piece_count",
    "score_template",
    "select_template",
]
