"""Extraccion segura de fuentes de campana.

El resultado siempre es un :class:`CampaignSource`.  Renderizar paginas o sacar
imagenes incrustadas solo crea evidencia para el brief; nunca crea proyectos ni
KV activos.
"""
from __future__ import annotations

import io
import logging
import math
import posixpath
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..config import settings
from ..models.campaign import (
    CampaignSource,
    CampaignSourceKind,
    CampaignSourceRole,
)
from . import campaign_plate, campaign_store, campaign_vector
from .security import FileValidationError

logger = logging.getLogger(__name__)


SUPPORTED_CAMPAIGN_EXTENSIONS = {
    ".pdf",
    ".ai",
    ".pptx",
    ".psd",
    ".psb",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".avif",
    ".docx",
    ".xlsx",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".rtf",
    ".ttf",
    ".otf",
    ".zip",
}

_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"
}
_ZIP_MAX_MEMBERS = 5000
_TEXT_LIMIT = 60_000
_PREVIEW_LIMIT = 16
_PDF_TEXT_PAGE_LIMIT = 500
_EMBEDDED_IMAGE_LIMIT = 50 * 1024 * 1024

# Un PSD puede traer cientos de capas auxiliares. Solo congelamos las que el
# diseñador identificó de forma explícita como fondo o decoración de marca. Es
# deliberadamente conservador: una capa ambigua es mejor dejarla como evidencia
# del brief que reutilizar por error un producto, una oferta o un icono social.
_PSD_FIXED_ASSET_LIMIT = 8
_PSD_FIXED_ASSET_MAX_PIXELS = 6_000_000
_PSD_BACKGROUND_MARKERS = {
    "background", "backdrop", "bg", "fondo", "gradiente", "gradient",
    "textura", "texture", "pattern", "patron", "relleno", "backplate",
}
_PSD_DECORATION_MARKERS = {
    "deco", "decoracion", "decorative", "ornamento", "ornament", "marco",
    "frame", "trama", "linea", "lineas", "line", "forma", "formas",
    "shape", "shapes", "brillo", "glow", "destello", "spark", "estrella",
    "star", "onda", "wave", "rayo", "ray", "curva", "curve",
}
_PSD_EXCLUDED_MARKERS = {
    "producto", "product", "sku", "modelo", "pack", "combo", "precio",
    "price", "oferta", "promo", "promotion", "discount", "descuento",
    "cuota", "titulo", "titular", "headline", "texto", "text",
    "legal", "cta", "llamado", "fecha", "date", "vigencia", "validity",
    "vencimiento", "vence", "codigo", "code", "qr", "url", "link", "web",
    "website", "instagram", "insta", "ig", "facebook", "fb", "tiktok", "youtube",
    "yt", "linkedin", "whatsapp", "twitter", "redes", "social", "icon", "icono",
    "iconos", "emoji", "foto", "photo", "imagen", "image", "persona", "person",
}
# Los diseñadores suelen llamar a este grupo "Marca" o "Brand", sobre todo
# cuando el logo está construido de varias formas. Es una señal tan fuerte
# como "logo" y no debe terminar como una decoración genérica.
_PSD_LOGO_MARKERS = {"logo", "logotipo", "isotipo", "marca", "brand", "wordmark"}
# Photoshop bautiza cada duplicado añadiendo "copy" o "copia" al nombre, y los
# PSD de agencia están llenos de ellos. Mientras "copy" estuvo en la lista de
# exclusión, una capa llamada "logo copy" —un logotipo real— se descartaba, y
# "logo cece copia" se conservaba solo porque el sufijo estaba en español. Estas
# palabras no dicen nada del contenido: se quitan antes de clasificar.
_PSD_DUPLICATE_TOKENS = {"copy", "copia", "copie", "kopie", "duplicado"}


def _representative_indices(total: int, limit: int) -> list[int]:
    """Reparte las vistas previas por todo el documento.

    Los manuales y toolkits suelen abrir con estrategia y dejar el sistema
    visual, logos y cierres para el final. Guardar simplemente las primeras 16
    paginas hacia que la IA nunca viera esa evidencia. La muestra incluye
    siempre la primera y la ultima pagina y distribuye el resto de forma
    uniforme, sin duplicar indices por redondeo.
    """

    if total <= 0 or limit <= 0:
        return []
    if total <= limit:
        return list(range(total))
    if limit == 1:
        return [0]
    selected = {
        round(position * (total - 1) / (limit - 1))
        for position in range(limit)
    }
    # ``round`` puede colisionar con combinaciones pequenas; completar desde
    # los huecos mas alejados mantiene el contrato de exactamente ``limit``.
    while len(selected) < limit:
        remaining = [index for index in range(total) if index not in selected]
        candidate = max(
            remaining,
            key=lambda index: min(abs(index - chosen) for chosen in selected),
        )
        selected.add(candidate)
    return sorted(selected)


def source_kind(extension: str) -> CampaignSourceKind:
    if extension == ".pdf":
        return CampaignSourceKind.PDF
    if extension == ".pptx":
        return CampaignSourceKind.PRESENTATION
    if extension in {".psd", ".psb", ".ai"}:
        return CampaignSourceKind.LAYERED_DESIGN
    if extension in _IMAGE_EXTENSIONS:
        return CampaignSourceKind.IMAGE
    if extension in {".ttf", ".otf"}:
        return CampaignSourceKind.FONT
    return CampaignSourceKind.DOCUMENT


def validate_extension(filename: str) -> str:
    extension = Path(filename or "").suffix.lower()
    if extension not in SUPPORTED_CAMPAIGN_EXTENSIONS:
        accepted = ", ".join(sorted(SUPPORTED_CAMPAIGN_EXTENSIONS))
        raise FileValidationError(
            f"'{filename}' no es una fuente de campana compatible. Se acepta: {accepted}."
        )
    return extension


def inspect_source(
    client_id: str,
    campaign_id: str,
    source_id: str,
    original_path: Path,
    original_filename: str,
    media_type: str,
    sha256: str,
    size_bytes: int,
) -> CampaignSource:
    """Valida, extrae y clasifica el archivo ya almacenado."""
    extension = validate_extension(original_filename)
    kind = source_kind(extension)
    source = CampaignSource(
        source_id=source_id,
        filename=(original_filename or "archivo")[:240],
        kind=kind,
        media_type=(media_type or "application/octet-stream")[:160],
        extension=extension,
        size_bytes=size_bytes,
        sha256=sha256,
        stored_path=campaign_store.relative_path(client_id, campaign_id, original_path),
    )

    if kind == CampaignSourceKind.PDF:
        _extract_pdf(client_id, campaign_id, original_path, source)
    elif extension == ".ai":
        _extract_illustrator(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.PRESENTATION:
        _extract_pptx(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.LAYERED_DESIGN:
        _extract_psd(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.IMAGE:
        _extract_image(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.FONT:
        _inspect_font(original_path, source)
    elif extension == ".zip":
        _extract_font_archive(client_id, campaign_id, original_path, source)
    elif extension == ".docx":
        _extract_docx(client_id, campaign_id, original_path, source)
    elif extension == ".xlsx":
        _extract_xlsx(original_path, source)
    else:
        _extract_text(original_path, source)

    source.roles = classify_roles(source)
    return source


def _source_folder(client_id: str, campaign_id: str, source: CampaignSource) -> Path:
    return campaign_store.source_dir(client_id, campaign_id, source.source_id)


def _relative(client_id: str, campaign_id: str, path: Path) -> str:
    return campaign_store.relative_path(client_id, campaign_id, path)


def _clean_text(value: str) -> str:
    compact = re.sub(r"[ \t]+", " ", value or "")
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()[:_TEXT_LIMIT]


def _save_thumbnail(image: Image.Image, target: Path, max_side: int = 1200) -> None:
    preview = image.convert("RGBA")
    preview.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if preview.mode == "RGBA":
        flat = Image.new("RGB", preview.size, (245, 245, 245))
        flat.paste(preview, mask=preview.getchannel("A"))
    else:  # pragma: no cover - convert("RGBA") mantiene esta rama defensiva
        flat = preview.convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    flat.save(target, format="JPEG", quality=86, optimize=True)


def _extract_pdf(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise FileValidationError("El archivo .pdf no tiene una firma PDF valida.")
    try:
        try:
            import pymupdf as fitz
        except ImportError:  # PyMuPDF < 1.24 conserva solo el alias historico.
            import fitz

        document = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        raise FileValidationError(f"No se pudo abrir el PDF: {exc}") from exc
    source.page_count = len(document)
    text_parts: list[str] = []
    text_chars = 0
    previewed = 0
    preview_indices = set(_representative_indices(source.page_count, _PREVIEW_LIMIT))
    previewed_pages: list[int] = []
    oversized_previews = 0
    folder = _source_folder(client_id, campaign_id, source) / "previews"
    try:
        for index, page in enumerate(document):
            if index >= _PDF_TEXT_PAGE_LIMIT:
                break
            page_text = page.get_text("text").strip()
            if page_text:
                remaining = _TEXT_LIMIT - text_chars
                if remaining > 0:
                    excerpt = f"[Pagina {index + 1}]\n{page_text}"[:remaining]
                    text_parts.append(excerpt)
                    text_chars += len(excerpt)
            if index not in preview_indices:
                continue
            # 1.25x basta para comprender jerarquia y color sin guardar otro PDF.
            estimated_width = max(1, int(page.rect.width * 1.25))
            estimated_height = max(1, int(page.rect.height * 1.25))
            if estimated_width * estimated_height > settings.campaign_max_source_pixels:
                oversized_previews += 1
                continue
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
            target = folder / f"page-{index + 1:03d}.jpg"
            with Image.open(io.BytesIO(pixmap.tobytes("png"))) as rendered:
                _save_thumbnail(rendered, target)
            source.preview_files.append(_relative(client_id, campaign_id, target))
            previewed += 1
            previewed_pages.append(index + 1)
    except FileValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - un PDF roto debe responder 400, no 500
        raise FileValidationError(f"No se pudo analizar el contenido del PDF: {exc}") from exc
    finally:
        document.close()
    source.extracted_text = _clean_text("\n\n".join(text_parts))
    source.meta.update(
        {
            "previewed_pages": previewed,
            "previewed_page_numbers": previewed_pages,
            "pages_with_text": len(text_parts),
        }
    )
    if source.page_count > _PDF_TEXT_PAGE_LIMIT:
        source.warnings.append(
            f"El PDF tiene {source.page_count} paginas; se analizaron las primeras "
            f"{_PDF_TEXT_PAGE_LIMIT} para mantener estable el servidor."
        )
    if oversized_previews:
        source.warnings.append(
            f"No se renderizaron {oversized_previews} paginas con dimensiones excesivas."
        )
    if source.page_count > previewed:
        source.warnings.append(
            f"Se analizaron textos de {source.page_count} paginas y se guardaron "
            f"{previewed} vistas previas representativas, incluyendo inicio y cierre."
        )


def _extract_illustrator(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    """Analiza un Illustrator PDF-compatible sin fingir capas de Photoshop.

    Illustrator puede guardar un PDF completo dentro del `.ai`. Ese contenido
    conserva la composición, texto y vectores suficientes para que la IA vea
    el arte. Los AI nativos sin PDF también se aceptan: se registran como
    contexto, pero se explica que para capas reutilizables se necesita PSD.
    """

    with path.open("rb") as handle:
        head = handle.read(4 * 1024 * 1024)
    offset = head.find(b"%PDF-")
    if offset < 0:
        visible = re.sub(rb"[^\x20-\x7e\n\r\t]+", b" ", head).decode("latin-1", errors="ignore")
        source.extracted_text = _clean_text(visible)
        source.page_count = 0
        source.meta["illustrator_pdf_compatible"] = False
        source.warnings.append(
            "El AI se guardó sin compatibilidad PDF: quedó como contexto de texto, "
            "pero para analizar el arte exporta PDF compatible o añade el PSD con capas."
        )
        return
    if offset == 0:
        _extract_pdf(client_id, campaign_id, path, source)
    else:
        # MuPDF abre de forma fiable el PDF aislado, no el encabezado PostScript
        # que Illustrator deja antes. Se escribe dentro de la fuente y se borra
        # al terminar; nunca se expone como archivo del usuario.
        temporary = _source_folder(client_id, campaign_id, source) / ".illustrator-preview.pdf"
        with path.open("rb") as original, temporary.open("wb") as target:
            original.seek(offset)
            shutil.copyfileobj(original, target, length=1024 * 1024)
        try:
            _extract_pdf(client_id, campaign_id, temporary, source)
        finally:
            temporary.unlink(missing_ok=True)
    source.meta["illustrator_pdf_compatible"] = True
    source.warnings.append(
        "AI PDF-compatible analizado como referencia visual; para conservar capas editables usa PSD."
    )


def _safe_zip(path: Path, expected_prefix: str | None) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FileValidationError("El documento Office no es un ZIP valido.") from exc
    entries = archive.infolist()
    if len(entries) > _ZIP_MAX_MEMBERS:
        archive.close()
        raise FileValidationError("El documento contiene demasiados recursos internos.")
    expanded = sum(max(0, item.file_size) for item in entries)
    # Nunca expandir una bomba ZIP. El multiplicador admite presentaciones muy
    # comprimidas sin convertir un archivo pequeno en gigabytes en el servidor.
    # El menor de ambos topes: el límite global no debe convertir un XLSX de
    # 200 KB en permiso para declarar gigabytes al descomprimir.
    limit = min(settings.max_upload_bytes, max(50 * 1024 * 1024, path.stat().st_size * 40))
    if expanded > limit:
        archive.close()
        raise FileValidationError("El documento comprimido declara demasiado contenido interno.")
    if expected_prefix and not any(item.filename.startswith(expected_prefix) for item in entries):
        archive.close()
        raise FileValidationError("La extension no coincide con el contenido del documento.")
    return archive


def _extract_font_archive(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    """Extrae únicamente fuentes reales de un ZIP sin aceptar rutas internas."""

    from .security import validate_font_bytes

    accepted: list[dict[str, str]] = []
    skipped = 0
    with _safe_zip(path, None) as archive:
        members = [
            item for item in archive.infolist()
            if not item.is_dir() and Path(item.filename).suffix.casefold() in {".ttf", ".otf"}
            and ".." not in Path(item.filename).parts
        ]
        if not members:
            raise FileValidationError("El ZIP no contiene tipografías .ttf u .otf.")
        if len(members) > 80:
            raise FileValidationError("El ZIP contiene demasiadas tipografías; divídelo en archivos menores.")
        folder = _source_folder(client_id, campaign_id, source) / "fonts"
        for index, member in enumerate(sorted(members, key=lambda item: item.filename.casefold()), 1):
            try:
                payload = archive.read(member)
                suffix = validate_font_bytes(payload, member.filename)
                target = folder / f"font-{index:03d}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                relative = _relative(client_id, campaign_id, target)
                source.asset_files.append(relative)
                prueba = font_trial_name(target)
                accepted.append({
                    "name": Path(member.filename).name[:240],
                    "path": relative,
                    "trial": prueba,
                })
            except Exception:  # noqa: BLE001 - un archivo basura no descarta toda la familia
                skipped += 1
    if not accepted:
        raise FileValidationError("No se pudo validar ninguna tipografía dentro del ZIP.")
    source.page_count = 0
    source.meta["font_assets"] = accepted
    source.extracted_text = _clean_text("\n".join(item["name"] for item in accepted))
    if skipped:
        source.warnings.append(f"Se ignoraron {skipped} archivos que no eran tipografías válidas.")
    de_prueba = [item["name"] for item in accepted if item.get("trial")]
    if de_prueba:
        source.warnings.append(
            "Versión de prueba: " + ", ".join(de_prueba[:4])
            + ". No se usa para componer los artes —esas caras estampan «DEMO» "
            "sobre el texto—. Sube la licenciada o se usará la del cliente."
        )


#: Palabras con las que las fundiciones marcan una licencia de prueba. Muchas
#: de esas caras sustituyen glifos por la palabra DEMO o TRIAL, y el arte sale
#: firmado con ella sin que nadie haya escrito eso en ninguna casilla.
_TRIAL_TOKENS = ("demo", "trial", "personal use", "personaluse", "unlicensed", "prueba")


def font_trial_name(path: Path) -> str:
    """Nombre de la tipografía si es una versión de prueba; "" si es de uso libre.

    No se mira el nombre del archivo —se renombra en un segundo— sino la tabla
    de nombres que lleva la propia fuente, que es la que la fundición firma.
    """

    try:
        from PIL import ImageFont

        familia, estilo = ImageFont.truetype(str(path), 24).getname()
    except Exception:  # noqa: BLE001 - una fuente ilegible ya falla en su sitio
        return ""
    completo = f"{familia or ''} {estilo or ''}".strip()
    plano = completo.casefold()
    return completo if any(token in plano for token in _TRIAL_TOKENS) else ""


def _xml_text(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return ""
    chunks: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] in {"t", "tab", "br"}:
            if element.tag.endswith("}t") and element.text:
                chunks.append(element.text)
            elif chunks:
                chunks.append("\n")
    return " ".join(chunks)


def _natural_slide_key(name: str) -> tuple[int, str]:
    match = re.search(r"slide(\d+)\.xml$", name)
    return (int(match.group(1)) if match else 10**9, name)


def _copy_zip_images(
    archive: zipfile.ZipFile,
    names: list[str],
    client_id: str,
    campaign_id: str,
    source: CampaignSource,
    *,
    add_previews: bool = True,
) -> None:
    asset_folder = _source_folder(client_id, campaign_id, source) / "assets"
    preview_folder = _source_folder(client_id, campaign_id, source) / "previews"
    for index, name in enumerate(names):
        if index >= 80:
            source.warnings.append("Se conservaron los primeros 80 recursos visuales incrustados.")
            break
        suffix = Path(name).suffix.lower()
        if suffix not in _IMAGE_EXTENSIONS:
            continue
        info = archive.getinfo(name)
        if info.file_size > _EMBEDDED_IMAGE_LIMIT:
            source.warnings.append(
                f"El recurso incrustado {Path(name).name} era demasiado grande y se omitio."
            )
            continue
        payload = archive.read(name)
        try:
            with Image.open(io.BytesIO(payload)) as probe:
                if probe.width * probe.height > settings.campaign_max_source_pixels:
                    raise ValueError("demasiados pixeles")
                probe.verify()
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                target = asset_folder / f"asset-{index + 1:03d}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                source.asset_files.append(_relative(client_id, campaign_id, target))
                if add_previews and len(source.preview_files) < _PREVIEW_LIMIT:
                    preview = preview_folder / f"asset-{index + 1:03d}.jpg"
                    _save_thumbnail(image, preview)
                    source.preview_files.append(_relative(client_id, campaign_id, preview))
        except Exception:  # noqa: BLE001 - un recurso malo no invalida el documento
            source.warnings.append(f"El recurso incrustado {Path(name).name} no era una imagen valida.")


_PPTX_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _pptx_theme(archive: zipfile.ZipFile) -> dict[str, str]:
    defaults = {
        "dk1": "1A1A1A", "lt1": "FFFFFF", "dk2": "333333", "lt2": "F2F2F2",
        "accent1": "4472C4", "accent2": "ED7D31", "accent3": "A5A5A5",
        "accent4": "FFC000", "accent5": "5B9BD5", "accent6": "70AD47",
    }
    try:
        root = ElementTree.fromstring(archive.read("ppt/theme/theme1.xml"))
    except (KeyError, ElementTree.ParseError):
        return defaults
    scheme = root.find(".//a:clrScheme", _PPTX_NS)
    if scheme is None:
        return defaults
    for item in scheme:
        key = item.tag.rsplit("}", 1)[-1]
        colour = next(iter(item), None)
        if colour is None:
            continue
        value = colour.attrib.get("val") or colour.attrib.get("lastClr")
        if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value):
            defaults[key] = value.upper()
    return defaults


def _pptx_colour(
    parent: ElementTree.Element | None,
    theme: dict[str, str],
    fallback: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    if parent is None:
        return fallback
    solid = parent.find(".//a:solidFill", _PPTX_NS)
    if solid is None:
        return fallback
    direct = solid.find("a:srgbClr", _PPTX_NS)
    value = direct.attrib.get("val", "") if direct is not None else ""
    if not value:
        scheme = solid.find("a:schemeClr", _PPTX_NS)
        value = theme.get(scheme.attrib.get("val", ""), "") if scheme is not None else ""
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value or ""):
        return fallback
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4)) + (fallback[3],)


def _pptx_size(archive: zipfile.ZipFile) -> tuple[int, int]:
    default = (13_333_333, 7_500_000)
    try:
        root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
        size = root.find(".//p:sldSz", _PPTX_NS)
        if size is None:
            return default
        return max(1, int(size.attrib.get("cx", default[0]))), max(
            1, int(size.attrib.get("cy", default[1]))
        )
    except (KeyError, ValueError, ElementTree.ParseError):
        return default


def _pptx_box(
    element: ElementTree.Element,
    slide_size: tuple[int, int],
    canvas: tuple[int, int],
    fallback_index: int,
) -> tuple[int, int, int, int]:
    transform = element.find(".//a:xfrm", _PPTX_NS)
    if transform is not None:
        offset = transform.find("a:off", _PPTX_NS)
        extent = transform.find("a:ext", _PPTX_NS)
        if offset is not None and extent is not None:
            try:
                sx, sy = canvas[0] / slide_size[0], canvas[1] / slide_size[1]
                return (
                    round(int(offset.attrib.get("x", 0)) * sx),
                    round(int(offset.attrib.get("y", 0)) * sy),
                    max(1, round(int(extent.attrib.get("cx", slide_size[0])) * sx)),
                    max(1, round(int(extent.attrib.get("cy", slide_size[1])) * sy)),
                )
            except ValueError:
                pass
    # Los PPTX mínimos de pruebas o exportadores poco ortodoxos pueden omitir
    # xfrm. Todavía se conserva el texto en una franja legible.
    margin = round(min(canvas) * .055)
    height = max(80, round(canvas[1] * .16))
    return margin, margin + fallback_index * (height + margin // 2), canvas[0] - margin * 2, height


def _pptx_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    configured = settings.default_font_bold if bold else settings.default_font
    paths = [
        configured,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in paths:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, max(10, size))
            except OSError:
                continue
    return ImageFont.load_default()


def _pptx_text(
    canvas: Image.Image,
    element: ElementTree.Element,
    box: tuple[int, int, int, int],
    theme: dict[str, str],
) -> None:
    paragraphs: list[str] = []
    for paragraph in element.findall(".//a:p", _PPTX_NS):
        value = "".join(node.text or "" for node in paragraph.findall(".//a:t", _PPTX_NS)).strip()
        if value:
            paragraphs.append(value)
    if not paragraphs:
        return
    text = "\n".join(paragraphs)
    x, y, width, height = box
    run = element.find(".//a:rPr", _PPTX_NS) or element.find(".//a:defRPr", _PPTX_NS)
    raw_size = run.attrib.get("sz", "") if run is not None else ""
    try:
        points = int(raw_size) / 100 if raw_size else 24
    except ValueError:
        points = 24
    font_size = max(11, min(round(points * canvas.width / 960 * 1.15), max(12, round(height * .44))))
    bold = bool(run is not None and run.attrib.get("b") in {"1", "true"})
    font = _pptx_font(font_size, bold)
    colour = _pptx_colour(run, theme, (30, 30, 35, 255))
    paragraph_props = element.find(".//a:pPr", _PPTX_NS)
    alignment = paragraph_props.attrib.get("algn", "l") if paragraph_props is not None else "l"
    align = "center" if alignment == "ctr" else "right" if alignment == "r" else "left"
    draw = ImageDraw.Draw(canvas, "RGBA")
    words = text.replace("\n", " \n ").split()
    lines: list[str] = []
    current = ""
    for word in words:
        if word == "\n":
            if current:
                lines.append(current)
                current = ""
            continue
        trial = (current + " " + word).strip()
        if current and draw.textbbox((0, 0), trial, font=font)[2] > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    line_height = max(font_size + 3, round(font_size * 1.18))
    lines = lines[: max(1, height // line_height)]
    for index, line in enumerate(lines):
        measured = draw.textbbox((0, 0), line, font=font)[2]
        tx = x if align == "left" else x + (width - measured) // 2 if align == "center" else x + width - measured
        draw.text((tx, y + index * line_height), line, font=font, fill=colour)


def _pptx_relationships(
    archive: zipfile.ZipFile, slide_name: str
) -> dict[str, str]:
    folder, filename = posixpath.split(slide_name)
    rel_name = posixpath.join(folder, "_rels", filename + ".rels")
    try:
        root = ElementTree.fromstring(archive.read(rel_name))
    except (KeyError, ElementTree.ParseError):
        return {}
    result: dict[str, str] = {}
    for relation in root.findall("rel:Relationship", _PPTX_NS):
        target = relation.attrib.get("Target", "")
        if not target or relation.attrib.get("TargetMode") == "External":
            continue
        result[relation.attrib.get("Id", "")] = posixpath.normpath(
            posixpath.join(folder, target)
        )
    return result


def _render_pptx_slide(
    archive: zipfile.ZipFile,
    slide_name: str,
    slide_size: tuple[int, int],
    theme: dict[str, str],
) -> Image.Image:
    root = ElementTree.fromstring(archive.read(slide_name))
    ratio = slide_size[0] / max(1, slide_size[1])
    width = 1600
    height = max(600, round(width / ratio))
    background = _pptx_colour(
        root.find(".//p:bg", _PPTX_NS), theme, (255, 255, 255, 255)
    )
    canvas = Image.new("RGBA", (width, height), background)
    relations = _pptx_relationships(archive, slide_name)
    tree = root.find(".//p:spTree", _PPTX_NS)
    if tree is None:
        return canvas
    fallback_index = 0
    for element in tree.iter():
        kind = element.tag.rsplit("}", 1)[-1]
        if kind not in {"sp", "pic", "graphicFrame"}:
            continue
        box = _pptx_box(element, slide_size, canvas.size, fallback_index)
        fallback_index += 1
        x, y, item_width, item_height = box
        if kind in {"sp", "graphicFrame"}:
            properties = element.find("p:spPr", _PPTX_NS)
            fill = _pptx_colour(properties, theme, (255, 255, 255, 0))
            if fill[3]:
                ImageDraw.Draw(canvas, "RGBA").rounded_rectangle(
                    (x, y, x + item_width, y + item_height),
                    radius=max(0, round(min(item_width, item_height) * .025)),
                    fill=fill,
                )
            _pptx_text(canvas, element, box, theme)
            continue
        blip = element.find(".//a:blip", _PPTX_NS)
        rel_id = blip.attrib.get(f"{{{_PPTX_NS['r']}}}embed", "") if blip is not None else ""
        media_name = relations.get(rel_id, "")
        if not media_name:
            continue
        try:
            with Image.open(io.BytesIO(archive.read(media_name))) as picture:
                fitted = ImageOps.fit(
                    picture.convert("RGBA"),
                    (item_width, item_height),
                    method=Image.Resampling.LANCZOS,
                )
            canvas.alpha_composite(fitted, (x, y))
            fitted.close()
        except Exception:  # noqa: BLE001 - una imagen rota no anula la diapositiva
            continue
    return canvas


def _extract_pptx(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    with _safe_zip(path, "ppt/") as archive:
        slide_names = sorted(
            (
                item.filename
                for item in archive.infolist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename)
            ),
            key=_natural_slide_key,
        )
        source.page_count = len(slide_names)
        texts = [
            f"[Diapositiva {index + 1}]\n{_xml_text(archive.read(name))}"
            for index, name in enumerate(slide_names)
        ]
        source.extracted_text = _clean_text("\n\n".join(texts))
        media = sorted(
            item.filename
            for item in archive.infolist()
            if item.filename.startswith("ppt/media/") and not item.is_dir()
        )
        _copy_zip_images(
            archive,
            media,
            client_id,
            campaign_id,
            source,
            add_previews=False,
        )
        slide_size = _pptx_size(archive)
        theme = _pptx_theme(archive)
        preview_indices = _representative_indices(len(slide_names), _PREVIEW_LIMIT)
        rendered_slides: list[int] = []
        preview_folder = _source_folder(client_id, campaign_id, source) / "previews"
        for index in preview_indices:
            try:
                rendered = _render_pptx_slide(
                    archive, slide_names[index], slide_size, theme
                )
                target = preview_folder / f"slide-{index + 1:03d}.jpg"
                _save_thumbnail(rendered, target, max_side=1400)
                rendered.close()
                source.preview_files.append(_relative(client_id, campaign_id, target))
                rendered_slides.append(index + 1)
            except Exception:  # noqa: BLE001 - conservar texto/assets si una slide esta rota
                source.warnings.append(
                    f"No se pudo reconstruir la vista de la diapositiva {index + 1}."
                )
    source.meta.update(
        {
            "slides_with_text": sum(bool(item.strip()) for item in texts),
            "embedded_media": len(media),
            "previewed_slides": rendered_slides,
        }
    )
    if not source.preview_files:
        source.warnings.append(
            "Se extrajo el texto y los recursos de la presentacion, pero no se pudo reconstruir ninguna diapositiva."
        )


def _extract_docx(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    with _safe_zip(path, "word/") as archive:
        try:
            source.extracted_text = _clean_text(_xml_text(archive.read("word/document.xml")))
        except KeyError as exc:
            raise FileValidationError("El DOCX no contiene word/document.xml.") from exc
        media = sorted(
            item.filename
            for item in archive.infolist()
            if item.filename.startswith("word/media/") and not item.is_dir()
        )
        _copy_zip_images(archive, media, client_id, campaign_id, source)
    source.page_count = 1
    source.meta["embedded_media"] = len(media)


def _extract_xlsx(path: Path, source: CampaignSource) -> None:
    """Extrae texto visible suficiente para entender cronogramas y matrices."""
    with _safe_zip(path, "xl/") as archive:
        worksheets = sorted(
            item.filename
            for item in archive.infolist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename)
        )
        chunks: list[str] = []
        for name in ("xl/sharedStrings.xml", *worksheets):
            try:
                text = _xml_text(archive.read(name))
            except KeyError:
                continue
            if text.strip():
                chunks.append(f"[{Path(name).stem}]\n{text}")
        source.extracted_text = _clean_text("\n\n".join(chunks))
        source.page_count = len(worksheets)
        source.meta["worksheets"] = len(worksheets)


def _psd_name_tokens(name: str) -> set[str]:
    """Normaliza el nombre humano de una capa sin depender del idioma.

    Photoshop conserva los nombres literalmente. Normalizarlos aqui permite
    reconocer ``Decoración`` y ``decoracion`` igual, pero sin intentar deducir
    semántica desde los píxeles de una capa que podría contener un producto.
    """

    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return set(re.findall(r"[a-z0-9]+", folded.casefold()))


def _psd_fixed_asset_role(
    name: str,
    kind: str,
    is_group: bool,
    *,
    bbox: tuple[int, int, int, int] | None = None,
    canvas: tuple[int, int] | None = None,
) -> str | None:
    """Devuelve un rol seguro para una capa PSD que puede congelarse.

    Nunca se interpretan grupos: medido en un KV real, un grupo llamado ``BG``
    contenía la foto del producto, el logo y el legal a la vez. Tampoco se
    reutilizan capas de texto ni nombres que indiquen contenido variable o
    iconografía de redes. Esa restricción es la que evita que una plantilla
    copie el arte fuente entero con su producto dentro.
    """

    kind_folded = kind.casefold()
    # El texto es variable por definición: es lo que cada fila de la matriz
    # reescribe. Esto va primero para que ningún nombre lo rescate.
    if kind_folded == "type":
        return None
    tokens = _psd_name_tokens(name) - _PSD_DUPLICATE_TOKENS
    if tokens.intersection(_PSD_EXCLUDED_MARKERS):
        return None
    if tokens.intersection(_PSD_LOGO_MARKERS):
        # Un logotipo real muchas veces es un grupo con letras, vectoriales y
        # máscaras dentro. Componer el grupo preserva sus proporciones y evita
        # reemplazarlo por una "M" o por el icono de una red social.
        return "logo"
    # Un grupo NO se congela aunque se llame "fondo". Medido en el KV de
    # muebles: su grupo "BG" contiene la foto del producto, el logo y el legal
    # juntos, así que componerlo horneaba la mesa en todas las piezas y sacaba
    # el logo por duplicado. El nombre del grupo describe su intención, no su
    # contenido.
    if is_group:
        return None
    if tokens.intersection(_PSD_BACKGROUND_MARKERS):
        return "fixed_background"
    if tokens.intersection(_PSD_DECORATION_MARKERS):
        return "fixed_decoration"
    return None


def _psd_artboard_frame_of(
    artboard,
) -> tuple[tuple[int, int, int, int], tuple[int, int]] | None:
    """Marco de un artboard concreto, no el de la capa que lo contiene."""

    raw = getattr(artboard, "bbox", None)
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        left, top, right, bottom = (int(value) for value in raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom), (right - left, bottom - top)


def _psd_clip_to_frame(
    bbox: tuple[int, int, int, int] | None,
    frame: tuple[int, int, int, int],
) -> tuple[int, int, int, int] | None:
    """Recorta una capa a su artboard, en coordenadas del documento."""

    if bbox is None:
        return None
    left = max(bbox[0], frame[0])
    top = max(bbox[1], frame[1])
    right = min(bbox[2], frame[2])
    bottom = min(bbox[3], frame[3])
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _psd_artboard_frame(layer) -> tuple[tuple[int, int, int, int], tuple[int, int]] | None:
    """Devuelve el artboard que contiene a esta capa, si lo hay.

    Un PSD de agencia trae varias piezas en el MISMO documento: en el KV de
    muebles conviven cuatro artboards —portada y producto, en post 1080x1080 y
    en story 1080x1920— dentro de un lienzo de 2235x3100. Medir sus capas contra
    el documento entero metía el fondo de una pieza en un cuadrante de la
    plantilla y superponía dos artes distintos, con la costura a la vista.

    El arte de verdad es el artboard, así que cada capa se mide contra el suyo.
    """

    actual = getattr(layer, "parent", None)
    while actual is not None:
        if str(getattr(actual, "kind", "") or "").casefold() == "artboard":
            raw = getattr(actual, "bbox", None)
            if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                return None
            try:
                left, top, right, bottom = (int(value) for value in raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if right <= left or bottom <= top:
                return None
            return (left, top, right, bottom), (right - left, bottom - top)
        actual = getattr(actual, "parent", None)
    return None


def _psd_bbox(
    raw_bbox: object, source_width: int, source_height: int
) -> tuple[int, int, int, int] | None:
    """Acota una caja de PSD antes de asignar memoria o escribir un asset."""

    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        return None
    try:
        left, top, right, bottom = (int(value) for value in raw_bbox)
    except (TypeError, ValueError, OverflowError):
        return None
    left = max(0, min(source_width, left))
    right = max(0, min(source_width, right))
    top = max(0, min(source_height, top))
    bottom = max(0, min(source_height, bottom))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _asset_rgba_from_psd_layer(
    layer,
    bbox: tuple[int, int, int, int],
    source_size: tuple[int, int],
) -> Image.Image | None:
    """Lee una capa aislada con dimensiones conocidas y alpha preservado.

    Dependiendo de la versión de ``psd-tools``, ``Layer.composite`` devuelve
    bien el recorte de la capa o un canvas completo. Ambos son contratos
    conocidos; cualquier otro tamaño se descarta en vez de reescalar contenido
    incierto sobre una zona de plantilla.
    """

    expected = (bbox[2] - bbox[0], bbox[3] - bbox[1])
    try:
        # Los objetos inteligentes pueden devolver el lienzo entero aunque su
        # caja visible sea pequeña. Pedir el viewport evita decodificar el PSD
        # completo por cada decoración. Algunos dobles/formatos antiguos no
        # aceptan ese argumento, por eso conservamos el camino compatible.
        try:
            rendered = layer.composite(viewport=bbox)
        except TypeError:
            rendered = layer.composite()
        if rendered is None or rendered.width <= 0 or rendered.height <= 0:
            return None
        if rendered.width * rendered.height > settings.campaign_max_source_pixels:
            return None
        rgba = rendered.convert("RGBA")
        if rendered is not rgba:
            rendered.close()
        if rgba.size == source_size:
            cropped = rgba.crop(bbox)
            rgba.close()
            rgba = cropped
        elif rgba.size != expected:
            rgba.close()
            return None
        if rgba.getchannel("A").getbbox() is None:
            rgba.close()
            return None
        if rgba.width * rgba.height > _PSD_FIXED_ASSET_MAX_PIXELS:
            scale = math.sqrt(_PSD_FIXED_ASSET_MAX_PIXELS / (rgba.width * rgba.height))
            resized = rgba.resize(
                (
                    max(1, int(round(rgba.width * scale))),
                    max(1, int(round(rgba.height * scale))),
                ),
                Image.Resampling.LANCZOS,
            )
            rgba.close()
            rgba = resized
        return rgba
    except Exception:  # noqa: BLE001 - una capa rota no invalida todo el PSD
        return None


def _extract_psd(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    with path.open("rb") as handle:
        header = handle.read(6)
    if len(header) < 6 or header[:4] != b"8BPS" or int.from_bytes(header[4:6], "big") not in {1, 2}:
        raise FileValidationError("El archivo no tiene una firma PSD/PSB valida.")
    try:
        from psd_tools import PSDImage

        document = PSDImage.open(path)
        source.width, source.height = document.size
        if source.width * source.height > settings.campaign_max_source_pixels:
            raise FileValidationError(
                "El PSD/PSB supera el limite de pixeles para una fuente de campaña."
            )
        composite = document.composite()
        if composite is None:
            raise ValueError("sin vista compuesta")
        layers = list(document.descendants())
        source.meta["layer_count"] = len(layers)
        manifest: list[dict[str, object]] = []
        text_chunks: list[str] = []
        layer_assets: list[dict[str, object]] = []
        asset_folder = _source_folder(client_id, campaign_id, source) / "assets"
        for index, layer in enumerate(layers[:500]):
            name = str(getattr(layer, "name", "") or f"Capa {index + 1}").strip()
            raw_kind = str(getattr(layer, "kind", "") or "")
            is_group = bool(layer.is_group())
            kind = raw_kind or ("group" if is_group else "pixel")
            bounded_bbox = _psd_bbox(
                getattr(layer, "bbox", None), source.width, source.height
            )
            bbox = list(bounded_bbox or (0, 0, 0, 0))
            text_value = ""
            if kind == "type":
                try:
                    text_value = str(layer.text or "").replace("\r", "\n").strip()
                except Exception:  # noqa: BLE001 - metadato de texto opcional
                    text_value = ""
            manifest_item: dict[str, object] = {
                "name": name[:240],
                "kind": kind[:40],
                "visible": bool(getattr(layer, "visible", True)),
                "bbox": bbox,
                "text": text_value[:1000],
            }
            manifest.append(manifest_item)
            if name:
                text_chunks.append(f"[Capa] {name}")
            if text_value:
                text_chunks.append(text_value)

            # El marco de referencia de una capa es su artboard cuando lo
            # tiene, y el documento solo cuando el PSD trae una sola pieza.
            frame = _psd_artboard_frame(layer)
            frame_box = frame[0] if frame else (0, 0, source.width, source.height)
            frame_size = frame[1] if frame else (source.width, source.height)
            raster_bbox = _psd_clip_to_frame(bounded_bbox, frame_box)
            asset_bbox = (
                None if raster_bbox is None
                else (
                    raster_bbox[0] - frame_box[0], raster_bbox[1] - frame_box[1],
                    raster_bbox[2] - frame_box[0], raster_bbox[3] - frame_box[1],
                )
            )
            role = _psd_fixed_asset_role(
                name,
                kind,
                is_group,
                bbox=asset_bbox,
                canvas=frame_size,
            )
            if (
                role is None
                or raster_bbox is None
                or asset_bbox is None
                or not bool(getattr(layer, "visible", True))
            ):
                continue
            # Los logos aislados pueden repetirse unas pocas veces; los fondos
            # y decoraciones se acotan más fuerte porque se componen en cada
            # preview y cada pieza de producción.
            same_role = sum(1 for item in layer_assets if item.get("role") == role)
            fixed_count = sum(
                1 for item in layer_assets
                if item.get("role") in {"fixed_background", "fixed_decoration"}
            )
            if role != "logo" and fixed_count >= _PSD_FIXED_ASSET_LIMIT:
                continue
            role_limit = {
                "logo": 4,
                "fixed_background": 2,
                "fixed_decoration": 6,
            }[role]
            if same_role >= role_limit:
                continue
            try:
                layer_width = raster_bbox[2] - raster_bbox[0]
                layer_height = raster_bbox[3] - raster_bbox[1]
                if layer_width * layer_height > settings.campaign_max_source_pixels:
                    continue
                rgba = _asset_rgba_from_psd_layer(
                    layer, raster_bbox, (source.width, source.height)
                )
                if rgba is None:
                    continue
                asset_type = {
                    "logo": "logo",
                    "fixed_background": "background",
                    "fixed_decoration": "decoration",
                }[role]
                target_asset = asset_folder / f"psd-{asset_type}-{same_role + 1:02d}.png"
                target_asset.parent.mkdir(parents=True, exist_ok=True)
                rgba.save(target_asset, format="PNG", optimize=True)
                rendered_size = [rgba.width, rgba.height]
                rgba.close()
                relative = _relative(client_id, campaign_id, target_asset)
                source.asset_files.append(relative)
                layer_assets.append({
                    "name": name[:240],
                    "role": role,
                    "path": relative,
                    "bbox": list(asset_bbox),
                    "source_size": [frame_size[0], frame_size[1]],
                    "rendered_size": rendered_size,
                    "z_index": index,
                })
                manifest_item["reusable_asset_role"] = role
            except Exception:  # noqa: BLE001 - el resto del PSD sigue siendo util
                continue
        source.meta["layer_manifest"] = manifest
        if layer_assets:
            source.meta["layer_assets"] = layer_assets
        source.meta["layer_evidence"] = {
            "visible_layers": sum(1 for item in manifest if item["visible"]),
            "text_layers": sum(1 for item in manifest if item["kind"] == "type" and item["text"]),
            "logo_assets": sum(1 for item in layer_assets if item["role"] == "logo"),
            "fixed_backgrounds": sum(1 for item in layer_assets if item["role"] == "fixed_background"),
            "fixed_decorations": sum(1 for item in layer_assets if item["role"] == "fixed_decoration"),
        }
        # Placa de plantilla: el arte real del artboard con su contenido
        # variable borrado. Es lo que permite que la plantilla comunique la
        # marca en vez de un degradado con "TITULAR DE CAMPAÑA" encima.
        congelados = {str(item.get("name", "")) for item in layer_assets}
        try:
            piezas = [
                capa for capa in document
                if str(getattr(capa, "kind", "") or "").casefold() == "artboard"
            ]
        except TypeError:
            # Un PSD sin capas de primer nivel iterables: se trata como una
            # pieza única. Nunca debe costar la ingesta entera del documento.
            piezas = []
        placas: list[dict[str, object]] = []
        for orden, pieza in enumerate(piezas or [None], 1):
            marco = _psd_artboard_frame_of(pieza) if pieza is not None else None
            frame_box = marco[0] if marco else (0, 0, source.width, source.height)
            frame_size = marco[1] if marco else (source.width, source.height)
            if frame_size[0] <= 0 or frame_size[1] <= 0:
                continue
            if frame_size[0] * frame_size[1] > settings.campaign_max_source_pixels:
                continue
            try:
                recorte = composite.convert("RGB").crop(frame_box)
                propio = [
                    {
                        "name": str(item["name"]),
                        "visible": item["visible"],
                        "bbox": [
                            item["bbox"][0] - frame_box[0], item["bbox"][1] - frame_box[1],
                            item["bbox"][2] - frame_box[0], item["bbox"][3] - frame_box[1],
                        ],
                    }
                    for item in manifest
                    if item["bbox"][2] > item["bbox"][0]
                    and item["bbox"][0] >= frame_box[0] and item["bbox"][2] <= frame_box[2]
                    and item["bbox"][1] >= frame_box[1] and item["bbox"][3] <= frame_box[3]
                ]
                destino = (
                    _source_folder(client_id, campaign_id, source)
                    / "assets" / f"psd-plate-{orden:02d}.png"
                )
                hecho = campaign_plate.build_plate(recorte, propio, congelados, destino)
                recorte.close()
                if hecho is None:
                    continue
                ruta, motor, avisos = hecho
                relativa = _relative(client_id, campaign_id, ruta)
                source.asset_files.append(relativa)
                placas.append({
                    "name": str(getattr(pieza, "name", "") or source.filename)[:240],
                    "path": relativa,
                    "size": [frame_size[0], frame_size[1]],
                    "engine": motor,
                })
                if orden == 1:
                    # El PSD no necesita OCR: ya sabe dónde está cada texto y
                    # qué dice. Se reutiliza el mismo clasificador que el arte
                    # plano para que las dos vías compongan igual de bien.
                    _plate_layout_from_manifest(source, manifest, frame_box, frame_size)
                source.warnings.extend(avisos[:1])
            except Exception:  # noqa: BLE001 - una placa fallida no anula el PSD
                continue
        if placas:
            source.meta["template_plates"] = placas

        source.extracted_text = _clean_text("\n".join(text_chunks))
        target = _source_folder(client_id, campaign_id, source) / "previews" / "composite.jpg"
        _save_thumbnail(composite, target)
        source.preview_files.append(_relative(client_id, campaign_id, target))
    except Exception as exc:  # noqa: BLE001
        raise FileValidationError(f"No se pudo leer la composicion del PSD/PSB: {exc}") from exc


def _extract_image(
    client_id: str, campaign_id: str, path: Path, source: CampaignSource
) -> None:
    try:
        with Image.open(path) as probe:
            if probe.width * probe.height > settings.campaign_max_source_pixels:
                raise FileValidationError("La imagen declara demasiados pixeles.")
            probe.verify()
        with Image.open(path) as image:
            image.load()
            source.width, source.height = image.size
            source.meta["image_format"] = image.format or source.extension.lstrip(".").upper()
            target = _source_folder(client_id, campaign_id, source) / "previews" / "image.jpg"
            _save_thumbnail(image, target)
            source.preview_files.append(_relative(client_id, campaign_id, target))
    except FileValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - toda decodificacion invalida responde 400
        raise FileValidationError(f"La imagen no se puede decodificar: {exc}") from exc


def _inspect_font(path: Path, source: CampaignSource) -> None:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise FileValidationError("La tipografia supera 20 MB.")
    try:
        from PIL import ImageFont

        ImageFont.truetype(str(path), 24)
    except Exception as exc:  # noqa: BLE001
        raise FileValidationError("La tipografia no es valida.") from exc
    source.page_count = 0
    prueba = font_trial_name(path)
    if prueba:
        source.meta["font_trial"] = prueba
        source.warnings.append(
            f"«{prueba}» es una versión de prueba. No se usa para componer los "
            "artes: esas caras estampan «DEMO» sobre el texto. Sube la "
            "licenciada o se usará la tipografía del cliente."
        )


def _extract_text(path: Path, source: CampaignSource) -> None:
    payload = path.read_bytes()
    if b"\x00" in payload[:4096]:
        raise FileValidationError("El documento de texto contiene datos binarios.")
    source.extracted_text = _clean_text(payload.decode("utf-8", errors="replace"))
    source.page_count = 1


#: Cuantas placas se construyen por campaña. Cada una cuesta una llamada de
#: OCR y otra de reconstrucción: con dos artes maestros ya hay de dónde elegir
#: por proporción, y una carpeta de treinta fotos no se convierte en factura.
FLAT_PLATE_LIMIT = 2
#: Lado largo de la placa. Por encima de esto solo se paga resolución que el
#: renderer va a reducir a 1080 px de todas formas.
FLAT_PLATE_MAX_SIDE = 1600
_PLATE_ROLE_RANK = {
    CampaignSourceRole.KEY_VISUAL: 0,
    CampaignSourceRole.FINAL_ART: 0,
    CampaignSourceRole.BACKGROUND: 1,
    CampaignSourceRole.VISUAL_REFERENCE: 2,
}
_PLATE_KINDS = {
    CampaignSourceKind.IMAGE,
    CampaignSourceKind.LAYERED_DESIGN,
    CampaignSourceKind.PDF,
    CampaignSourceKind.PRESENTATION,
}
#: Un arte exportado a PDF tiene una página; un manual o una presentación de
#: estrategia, muchas. Es lo único que separa a un KV de un documento cuando el
#: nombre del archivo no dice nada.
_PLATE_MAX_PAGES = 2


def _recover_psd_placements(source: CampaignSource) -> None:
    """Rescata las posiciones de un PSD cuya placa se construyó antes.

    Solo cuando la placa cubre el documento entero: en un PSD de varias mesas
    de trabajo las cajas del manifiesto están en coordenadas del documento y
    haría falta el desplazamiento de la mesa, que las placas antiguas no
    guardaron. Colocar el precio con un desfase es peor que la retícula.
    """

    manifest = source.meta.get("layer_manifest")
    placas = source.meta.get("template_plates")
    if not isinstance(manifest, list) or not isinstance(placas, list) or not placas:
        return
    medida = placas[0].get("size") if isinstance(placas[0], dict) else None
    if not isinstance(medida, (list, tuple)) or len(medida) != 2:
        return
    try:
        ancho, alto = int(medida[0]), int(medida[1])
    except (TypeError, ValueError, OverflowError):
        return
    if (ancho, alto) != (source.width, source.height):
        return
    _plate_layout_from_manifest(source, manifest, (0, 0, ancho, alto), (ancho, alto))


def _plate_layout_from_manifest(
    source: CampaignSource,
    manifest: list[dict],
    frame_box: tuple[int, int, int, int],
    frame_size: tuple[int, int],
) -> None:
    """Posiciones y tintas de una placa de PSD, desde sus capas de texto.

    Un PSD trae lo que el OCR tiene que adivinar: el texto exacto y su caja.
    Pasarlo por el mismo clasificador evita dos composiciones distintas según
    el formato en que llegó el arte.
    """

    from . import campaign_layout_from_art as desde_arte
    from ..models.formats import DEFAULT_SAFE_AREA, FORMAT_PRESETS

    lecturas: list[dict[str, object]] = []
    for item in manifest:
        if str(item.get("kind", "")) != "type":
            continue
        texto = str(item.get("text", "") or "").strip()
        bbox = item.get("bbox")
        if not texto or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = (int(valor) for valor in bbox)
        if x1 <= x0 or y1 <= y0:
            continue
        lecturas.append({
            "text": texto[:200],
            "bbox": [x0 - frame_box[0], y0 - frame_box[1], x1 - frame_box[0], y1 - frame_box[1]],
            "confidence": 1.0,
            "role": "content",
        })
    if not lecturas:
        return
    familia = desde_arte.aspect_key(frame_size)
    preset = {
        "portrait": "meta_feed_4_5", "square": "meta_feed_square",
        "story": "meta_stories", "landscape": "meta_feed_landscape",
    }[familia]
    segura = FORMAT_PRESETS.get(preset, {}).get("safe_area", DEFAULT_SAFE_AREA)
    posiciones = desde_arte.placements(lecturas, frame_size, segura)
    if posiciones:
        source.meta["plate_placements"] = {
            familia: {
                hueco: caja.model_dump(mode="json") for hueco, caja in posiciones.items()
            }
        }
        source.meta["plate_text_reads"] = lecturas[:60]


def _plate_artwork_path(
    client_id: str, campaign_id: str, source: CampaignSource
) -> Path | None:
    """El archivo del que sacar la placa: el original si es imagen, si no su vista."""

    candidatas: list[str] = []
    if source.kind == CampaignSourceKind.IMAGE:
        candidatas.append(source.stored_path)
    candidatas.extend(source.preview_files[:1])
    for relativa in candidatas:
        try:
            path = campaign_store.campaign_path(client_id, campaign_id, relativa)
        except Exception:  # noqa: BLE001
            continue
        if path.is_file():
            return path
    return None


_VECTOR_EXTENSIONS = {".ai", ".pdf"}


def _plate_origin(source: CampaignSource) -> str:
    placas = source.meta.get("template_plates")
    if not isinstance(placas, list) or not placas or not isinstance(placas[0], dict):
        return ""
    return str(placas[0].get("origin") or "psd")


def _drop_plate(source: CampaignSource) -> None:
    for clave in ("template_plates", "plate_placements", "plate_text_colors", "plate_text_reads"):
        source.meta.pop(clave, None)


def _ensure_vector_templates(client_id: str, campaign_id: str, campaign) -> list[str]:
    """Saca la plantilla del editable vectorial (`.ai`, PDF) cuando lo hay.

    Se hace una vez por fuente y versión del extractor: un `.ai` de 400 MB
    tarda medio minuto en recorrerse y no cambia entre un análisis y otro.
    """

    avisos: list[str] = []
    catalogos: list[str] = []
    for source in campaign.sources:
        if Path(source.filename).suffix.casefold() not in _VECTOR_EXTENSIONS:
            continue
        if source.meta.get("vector_version") == campaign_vector.VERSION:
            if source.meta.get("vector_catalog"):
                catalogos.append(source.filename)
            continue
        try:
            path = campaign_store.campaign_path(client_id, campaign_id, source.stored_path)
        except Exception:  # noqa: BLE001
            continue
        offset = campaign_vector.is_vector_source(path) if path.is_file() else None
        if offset is None:
            source.meta["vector_version"] = campaign_vector.VERSION
            continue
        carpeta = _source_folder(client_id, campaign_id, source) / "assets"
        temporal = None
        try:
            documento = path
            if offset > 0:
                temporal = carpeta / ".vector-source.pdf"
                temporal.parent.mkdir(parents=True, exist_ok=True)
                with path.open("rb") as original, temporal.open("wb") as destino:
                    original.seek(offset)
                    shutil.copyfileobj(original, destino, length=1024 * 1024)
                documento = temporal
            resumen = campaign_vector.build_templates(documento, carpeta)
        except Exception as exc:  # noqa: BLE001 - sin plantilla vectorial se sigue
            logger.info("No se pudo leer %s como editable (%s)", source.filename, exc)
            source.meta["vector_version"] = campaign_vector.VERSION
            continue
        finally:
            if temporal is not None:
                temporal.unlink(missing_ok=True)
        source.meta["vector_version"] = campaign_vector.VERSION
        source.meta["vector_summary"] = {
            "pieces": resumen["pieces"],
            "catalog_pieces": resumen["catalog_pieces"],
            "plates": [
                {k: placa[k] for k in ("family", "origin", "page")} for placa in resumen["plates"]
            ],
        }
        if resumen["plates"]:
            placas = []
            for placa in resumen["plates"]:
                relativa = _relative(client_id, campaign_id, carpeta / placa["file"])
                if relativa not in source.asset_files:
                    source.asset_files.append(relativa)
                placas.append({
                    "name": f"{source.filename} · pág. {placa['page']}",
                    "path": relativa,
                    "size": placa["size"],
                    "engine": "vector",
                    "origin": "vector",
                    "family": placa["family"],
                })
            source.meta["template_plates"] = placas
            source.meta["plate_placements"] = resumen["placements"]
            source.meta["plate_text_colors"] = resumen["text_colors"]
            source.meta["plate_text_reads"] = resumen["text_reads"]
            source.meta["plate_text_fonts"] = resumen.get("fonts", {})
            source.meta.pop("vector_catalog", None)
        elif resumen["catalog_pieces"]:
            source.meta["vector_catalog"] = True
            if _plate_origin(source) != "psd":
                _drop_plate(source)
            catalogos.append(source.filename)
    if catalogos and not any(_plate_origin(s) == "vector" for s in campaign.sources):
        avisos.append(
            f"{', '.join(catalogos)} es un catálogo con muchos productos por página: "
            "no sirve como plantilla de una pieza. Sube el KV de producto editable "
            "(el toolkit en PDF, el .ai o el PSD de una pieza) para sacar la plantilla real."
        )
    return avisos


def ensure_template_plates(
    client_id: str, campaign_id: str, campaign, brand_name: str = ""
) -> list[str]:
    """Garantiza que la campaña tenga una placa real de la que partir.

    El PSD por capas la produce en la ingesta, pero casi ningún KV llega en
    PSD: llega como JPG, como página de PDF o como un AI aplanado. Sin placa el
    renderer caía a una copia desenfocada del anuncio anterior —el fondo
    borroso— y dibujaba su propia retícula encima. Aquí se construye igual, con
    OCR para saber qué texto borrar y el motor de reconstrucción para rellenar.

    Devuelve avisos. Nunca lanza: una campaña sin placa sigue produciendo.
    """

    avisos: list[str] = list(_ensure_vector_templates(client_id, campaign_id, campaign))
    if any(_plate_origin(source) == "vector" for source in campaign.sources):
        # El editable ya dio la plantilla de verdad. Una placa sacada por OCR
        # de otra fuente competiría con ella en el renderer y en las
        # posiciones, así que se retira.
        for source in campaign.sources:
            if _plate_origin(source) == "ocr":
                _drop_plate(source)
        return avisos
    con_placa = [source for source in campaign.sources if source.meta.get("template_plates")]
    if con_placa:
        # Una campaña con la placa ya hecha por el PSD puede no tener las
        # posiciones: si el PSD se subió antes de que existieran, se quedaría
        # con la retícula genérica para siempre, porque la ingesta no se repite
        # al regenerar el brief. Se recuperan aquí desde su propio manifiesto.
        for source in con_placa:
            if not source.meta.get("plate_placements"):
                _recover_psd_placements(source)
        return avisos

    ordenadas: list[tuple[int, int, CampaignSource]] = []
    for source in campaign.sources:
        if source.kind not in _PLATE_KINDS:
            continue
        rango = min(
            (_PLATE_ROLE_RANK[role] for role in source.roles if role in _PLATE_ROLE_RANK),
            default=None,
        )
        if rango is None:
            # Un KV exportado a PDF no trae ninguna palabra que lo delate y el
            # clasificador lo deja en "other": sin esto, una campaña cuyo arte
            # llegó en PDF se quedaba sin placa y volvía a la retícula. Se
            # admite el último, y solo si parece un arte y no un documento.
            if (
                source.kind in {CampaignSourceKind.PDF, CampaignSourceKind.PRESENTATION}
                and 0 < source.page_count <= _PLATE_MAX_PAGES
                and CampaignSourceRole.BRAND_MANUAL not in source.roles
            ):
                rango = max(_PLATE_ROLE_RANK.values()) + 1
            else:
                continue
        # Un arte de campaña es cuadrado o vertical; una lámina apaisada de
        # 16:9 suele ser una presentación, no el KV.
        proporcion = (source.width / source.height) if source.height else 1.0
        forma = 0 if 0.5 <= proporcion <= 1.6 else 1
        ordenadas.append((rango, forma, source))
    ordenadas.sort(key=lambda item: (item[0], item[1]))

    for _rango, _forma, source in ordenadas[:FLAT_PLATE_LIMIT]:
        if source.meta.get("vector_catalog"):
            # Un desplegable de doce productos no es una pieza: su placa era
            # el catálogo entero con la retícula encima.
            continue
        origen = _plate_artwork_path(client_id, campaign_id, source)
        if origen is None:
            continue
        try:
            with Image.open(origen) as probe:
                if probe.width * probe.height > settings.campaign_max_source_pixels:
                    continue
                probe.verify()
            with Image.open(origen) as imagen:
                arte = ImageOps.exif_transpose(imagen).convert("RGB")
                arte.thumbnail(
                    (FLAT_PLATE_MAX_SIDE, FLAT_PLATE_MAX_SIDE), Image.Resampling.LANCZOS
                )
                medida = arte.size
                destino = (
                    _source_folder(client_id, campaign_id, source)
                    / "assets" / "plate-01.png"
                )
                hecho = campaign_plate.build_plate_from_artwork(
                    arte, destino, brand_name=brand_name
                )
                arte.close()
        except Exception as exc:  # noqa: BLE001 - la campaña sigue sin placa
            logger.info("No se pudo construir la placa de %s (%s)", source.filename, exc)
            continue
        if hecho is None:
            continue
        ruta, motor, propios, leidas = hecho
        relativa = _relative(client_id, campaign_id, ruta)
        if relativa not in source.asset_files:
            source.asset_files.append(relativa)
        source.meta["template_plates"] = [{
            "name": source.filename,
            "path": relativa,
            "size": [medida[0], medida[1]],
            "engine": motor,
            "origin": "ocr",
        }]
        if leidas:
            source.meta["plate_text_reads"] = leidas[:60]
            # Donde el diseñador puso cada cosa. Vale tanto como el fondo
            # limpio: sin esto la plantilla vuelve a ser una retícula genérica
            # encima del arte de la marca.
            from . import campaign_layout_from_art as desde_arte
            from ..models.formats import DEFAULT_SAFE_AREA, FORMAT_PRESETS

            familia = desde_arte.aspect_key(medida)
            preset = {
                "portrait": "meta_feed_4_5", "square": "meta_feed_square",
                "story": "meta_stories", "landscape": "meta_feed_landscape",
            }[familia]
            segura = FORMAT_PRESETS.get(preset, {}).get("safe_area", DEFAULT_SAFE_AREA)
            posiciones = desde_arte.placements(leidas, medida, segura)
            if posiciones:
                source.meta["plate_placements"] = {
                    familia: {
                        hueco: caja.model_dump(mode="json")
                        for hueco, caja in posiciones.items()
                    }
                }
            tintas = desde_arte.colores(leidas, medida)
            if tintas:
                source.meta["plate_text_colors"] = tintas
        avisos.extend(propios[:1])
        break
    return avisos


def apply_plate_placements(campaign, candidates) -> int:
    """Traslada a las candidatas las posiciones medidas sobre el arte real.

    Solo se tocan los huecos que el arte declaró, y solo en la familia de
    formato de la que salieron: adivinar un story a partir de un feed sería
    inventar. El resto de las posiciones sigue viniendo de la retícula, que es
    un resultado aceptable, no una mentira.

    No pisa a una candidata que ya traiga posiciones propias —una corrección
    humana o un sistema aprobado antes manda sobre la deducción automática—.
    """

    from ..models.campaign import NormalizedPlacement

    medidas: dict[str, dict] = {}
    tintas: dict[str, str] = {}
    rango = {"vector": 0, "psd": 1, "ocr": 2}
    fuentes = sorted(campaign.sources, key=lambda source: rango.get(_plate_origin(source), 3))
    vectorial = bool(fuentes) and _plate_origin(fuentes[0]) == "vector"
    for source in fuentes:
        crudo = source.meta.get("plate_placements")
        if isinstance(crudo, dict):
            for familia, huecos in crudo.items():
                if isinstance(huecos, dict):
                    medidas.setdefault(str(familia), huecos)
        propias = source.meta.get("plate_text_colors")
        if isinstance(propias, dict):
            for hueco, color in propias.items():
                tintas.setdefault(str(hueco), str(color))
    if not medidas:
        return 0

    tocadas = 0
    for candidate in candidates:
        # Las cajas del editable son las de la placa: la pastilla del precio
        # está pintada ahí. Unas posiciones propuestas por la IA dejarían la
        # cifra fuera de su pastilla, así que mandan las medidas, salvo en una
        # candidata ya aprobada.
        manda_la_placa = vectorial and not candidate.approved
        if manda_la_placa:
            candidate.meta["plate_measured"] = True
        for familia, huecos in medidas.items():
            propias = candidate.blueprint.placements.get(familia) or {}
            if propias and not manda_la_placa:
                continue
            convertidas: dict[str, NormalizedPlacement] = {}
            for hueco, caja in huecos.items():
                try:
                    convertidas[str(hueco)] = NormalizedPlacement.model_validate(caja)
                except Exception:  # noqa: BLE001 - una caja rota no anula el resto
                    continue
            if convertidas:
                candidate.blueprint.placements[familia] = convertidas
                tocadas += 1
        if tintas and (manda_la_placa or not candidate.blueprint.text_colors):
            candidate.blueprint.text_colors = dict(tintas)
    return tocadas


def classify_roles(source: CampaignSource) -> list[CampaignSourceRole]:
    """Clasificacion conservadora: se puede enriquecer despues con vision."""
    haystack = f"{source.filename}\n{source.extracted_text[:20_000]}".lower()
    roles: list[CampaignSourceRole] = []

    def add(role: CampaignSourceRole) -> None:
        if role not in roles:
            roles.append(role)

    if source.kind == CampaignSourceKind.FONT:
        add(CampaignSourceRole.TYPOGRAPHY)
    if source.meta.get("font_assets"):
        add(CampaignSourceRole.TYPOGRAPHY)
    if source.kind == CampaignSourceKind.LAYERED_DESIGN:
        add(CampaignSourceRole.KEY_VISUAL)
        add(CampaignSourceRole.VISUAL_REFERENCE)
        assets = source.meta.get("layer_assets", [])
        if isinstance(assets, list):
            asset_roles = {str(item.get("role")) for item in assets if isinstance(item, dict)}
            if "logo" in asset_roles:
                add(CampaignSourceRole.LOGO)
            if "fixed_background" in asset_roles:
                add(CampaignSourceRole.BACKGROUND)
    if source.kind in {CampaignSourceKind.IMAGE, CampaignSourceKind.PRESENTATION}:
        add(CampaignSourceRole.VISUAL_REFERENCE)
    if any(word in haystack for word in ("manual de marca", "brand book", "brandbook", "guideline")):
        add(CampaignSourceRole.BRAND_MANUAL)
    if any(word in haystack for word in ("brief", "objetivo", "audiencia", "insight", "concepto", "estrateg")):
        add(CampaignSourceRole.STRATEGY)
    if any(word in haystack for word in ("key visual", "keyvisual", " kv ", "arte maestro")):
        add(CampaignSourceRole.KEY_VISUAL)
    if any(word in haystack for word in ("arte final", "final art", "aprobado")):
        add(CampaignSourceRole.FINAL_ART)
    if any(word in haystack for word in ("logo", "logotipo", "isotipo")):
        add(CampaignSourceRole.LOGO)
    if any(word in haystack for word in ("fondo", "background", "textura")):
        add(CampaignSourceRole.BACKGROUND)
    if any(word in haystack for word in ("legal", "terminos", "terminos y condiciones", "restriccion")):
        add(CampaignSourceRole.LEGAL)
    if any(word in haystack for word in ("cronograma", "calendario", "vigencia", "fecha de publicacion")):
        add(CampaignSourceRole.SCHEDULE)
    if any(word in haystack for word in ("producto", "catalogo", "sku", "referencia comercial")):
        add(CampaignSourceRole.PRODUCT_REFERENCE)
    if not roles:
        add(CampaignSourceRole.OTHER)
    return roles
