"""Extraccion segura de fuentes de campana.

El resultado siempre es un :class:`CampaignSource`.  Renderizar paginas o sacar
imagenes incrustadas solo crea evidencia para el brief; nunca crea proyectos ni
KV activos.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

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

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".avif"}
_ZIP_MAX_MEMBERS = 5000
_TEXT_LIMIT = 60_000
_PREVIEW_LIMIT = 16
_PDF_TEXT_PAGE_LIMIT = 500
_EMBEDDED_IMAGE_LIMIT = 50 * 1024 * 1024


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
            if index >= _PREVIEW_LIMIT:
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
    except FileValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - un PDF roto debe responder 400, no 500
        raise FileValidationError(f"No se pudo analizar el contenido del PDF: {exc}") from exc
    finally:
        document.close()
    source.extracted_text = _clean_text("\n\n".join(text_parts))
    source.meta.update({"previewed_pages": previewed, "pages_with_text": len(text_parts)})
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
            f"vistas previas de las primeras {previewed}."
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
                if len(source.preview_files) < _PREVIEW_LIMIT:
                    preview = preview_folder / f"asset-{index + 1:03d}.jpg"
                    _save_thumbnail(image, preview)
                    source.preview_files.append(_relative(client_id, campaign_id, preview))
        except Exception:  # noqa: BLE001 - un recurso malo no invalida el documento
            source.warnings.append(f"El recurso incrustado {Path(name).name} no era una imagen valida.")


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
        _copy_zip_images(archive, media, client_id, campaign_id, source)
    source.meta.update(
        {"slides_with_text": sum(bool(item.strip()) for item in texts), "embedded_media": len(media)}
    )
    if not source.preview_files:
        source.warnings.append(
            "Se extrajo el texto de la presentacion, pero no tenia imagenes incrustadas utilizables."
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
            kind = raw_kind or ("group" if layer.is_group() else "pixel")
            bbox = [int(value) for value in getattr(layer, "bbox", (0, 0, 0, 0))]
            text_value = ""
            if kind == "type":
                try:
                    text_value = str(layer.text or "").replace("\r", "\n").strip()
                except Exception:  # noqa: BLE001 - metadato de texto opcional
                    text_value = ""
            manifest.append(
                {
                    "name": name[:240],
                    "kind": kind[:40],
                    "visible": bool(getattr(layer, "visible", True)),
                    "bbox": bbox,
                    "text": text_value[:1000],
                }
            )
            if name:
                text_chunks.append(f"[Capa] {name}")
            if text_value:
                text_chunks.append(text_value)

            # Si el PSD nombra su logo, se conserva ese recurso aislado. Esto
            # evita usar por error la composicion completa (o un icono social)
            # como marca en las plantillas.
            marker = name.casefold()
            is_logo = any(token in marker for token in ("logo", "logotipo", "isotipo"))
            if not is_logo or len(layer_assets) >= 4 or not bool(getattr(layer, "visible", True)):
                continue
            try:
                layer_width = max(0, bbox[2] - bbox[0])
                layer_height = max(0, bbox[3] - bbox[1])
                if not layer_width or not layer_height or layer_width * layer_height > 30_000_000:
                    continue
                rendered = layer.composite()
                if rendered is None or rendered.width * rendered.height > 30_000_000:
                    continue
                rgba = rendered.convert("RGBA")
                if rgba.getchannel("A").getbbox() is None:
                    continue
                rgba.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
                target_asset = asset_folder / f"psd-logo-{len(layer_assets) + 1:02d}.png"
                target_asset.parent.mkdir(parents=True, exist_ok=True)
                rgba.save(target_asset, format="PNG", optimize=True)
                relative = _relative(client_id, campaign_id, target_asset)
                source.asset_files.append(relative)
                layer_assets.append(
                    {"name": name[:240], "role": "logo", "path": relative, "bbox": bbox}
                )
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
