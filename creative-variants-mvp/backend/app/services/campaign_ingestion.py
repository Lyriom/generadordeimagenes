"""Extraccion segura de fuentes de campana.

El resultado siempre es un :class:`CampaignSource`.  Renderizar paginas o sacar
imagenes incrustadas solo crea evidencia para el brief; nunca crea proyectos ni
KV activos.
"""
from __future__ import annotations

import io
import math
import posixpath
import re
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
from . import campaign_store
from .security import FileValidationError


SUPPORTED_CAMPAIGN_EXTENSIONS = {
    ".pdf",
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
    "cuota", "titulo", "titular", "headline", "copy", "texto", "text",
    "legal", "cta", "llamado", "fecha", "date", "vigencia", "validity",
    "vencimiento", "vence", "codigo", "code", "qr", "url", "link", "web",
    "website", "instagram", "insta", "ig", "facebook", "fb", "tiktok", "youtube",
    "yt", "linkedin", "whatsapp", "twitter", "redes", "social", "icon", "icono",
    "iconos", "emoji", "foto", "photo", "imagen", "image", "persona", "person",
}
_PSD_LOGO_MARKERS = {"logo", "logotipo", "isotipo"}


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
    if extension in {".psd", ".psb"}:
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
    elif kind == CampaignSourceKind.PRESENTATION:
        _extract_pptx(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.LAYERED_DESIGN:
        _extract_psd(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.IMAGE:
        _extract_image(client_id, campaign_id, original_path, source)
    elif kind == CampaignSourceKind.FONT:
        _inspect_font(original_path, source)
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


def _safe_zip(path: Path, expected_prefix: str) -> zipfile.ZipFile:
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
    if not any(item.filename.startswith(expected_prefix) for item in entries):
        archive.close()
        raise FileValidationError("La extension no coincide con el contenido del documento.")
    return archive


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


def _psd_fixed_asset_role(name: str, kind: str, is_group: bool) -> str | None:
    """Devuelve un rol seguro para una capa PSD que puede congelarse.

    Nunca interpretamos grupos: un grupo llamado ""fondo"" puede incluir el
    titular, una foto o un precio. Tampoco se reutilizan capas de texto ni
    nombres que indiquen contenido variable o iconografía de redes. Esta
    restricción es la que evita que una plantilla copie un arte fuente entero.
    """

    if is_group or kind.casefold() == "type":
        return None
    tokens = _psd_name_tokens(name)
    if not tokens or tokens.intersection(_PSD_EXCLUDED_MARKERS):
        return None
    if tokens.intersection(_PSD_LOGO_MARKERS):
        return "logo"
    if tokens.intersection(_PSD_BACKGROUND_MARKERS):
        return "fixed_background"
    if tokens.intersection(_PSD_DECORATION_MARKERS):
        return "fixed_decoration"
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

            role = _psd_fixed_asset_role(name, kind, is_group)
            if (
                role is None
                or bounded_bbox is None
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
                layer_width = bounded_bbox[2] - bounded_bbox[0]
                layer_height = bounded_bbox[3] - bounded_bbox[1]
                if layer_width * layer_height > settings.campaign_max_source_pixels:
                    continue
                rgba = _asset_rgba_from_psd_layer(
                    layer, bounded_bbox, (source.width, source.height)
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
                    "bbox": list(bounded_bbox),
                    "source_size": [source.width, source.height],
                    "rendered_size": rendered_size,
                    "z_index": index,
                })
                manifest_item["reusable_asset_role"] = role
            except Exception:  # noqa: BLE001 - el resto del PSD sigue siendo util
                continue
        source.meta["layer_manifest"] = manifest
        if layer_assets:
            source.meta["layer_assets"] = layer_assets
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


def _extract_text(path: Path, source: CampaignSource) -> None:
    payload = path.read_bytes()
    if b"\x00" in payload[:4096]:
        raise FileValidationError("El documento de texto contiene datos binarios.")
    source.extracted_text = _clean_text(payload.decode("utf-8", errors="replace"))
    source.page_count = 1


def classify_roles(source: CampaignSource) -> list[CampaignSourceRole]:
    """Clasificacion conservadora: se puede enriquecer despues con vision."""
    haystack = f"{source.filename}\n{source.extracted_text[:20_000]}".lower()
    roles: list[CampaignSourceRole] = []

    def add(role: CampaignSourceRole) -> None:
        if role not in roles:
            roles.append(role)

    if source.kind == CampaignSourceKind.FONT:
        add(CampaignSourceRole.TYPOGRAPHY)
    if source.kind == CampaignSourceKind.LAYERED_DESIGN:
        add(CampaignSourceRole.KEY_VISUAL)
        add(CampaignSourceRole.VISUAL_REFERENCE)
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
