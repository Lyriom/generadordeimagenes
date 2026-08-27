"""Validación de archivos y rutas seguras.

Reglas del MVP:
- Solo JPEG y PNG (verificados por magic bytes + decodificación real con Pillow).
- SVG y cualquier otro formato quedan rechazados.
- Nombres internos seguros (uuid + extensión canónica).
- Sin escapes de directorio (path traversal).
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ..config import settings

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
#: El PSD solo se admite como arte/KV de origen (se aplana al importarlo).
ALLOWED_PSD_EXTENSIONS = {".psd"}
ALLOWED_FONT_EXTENSIONS = {".ttf", ".otf"}

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"8BPS", "PSD"),
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class FileValidationError(ValueError):
    """El archivo subido no cumple las reglas de seguridad/formato."""


class PathTraversalError(ValueError):
    """Se intentó salir del directorio permitido."""


def slugify(value: str, fallback: str = "proyecto", max_length: int = 60) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = _SAFE_NAME_RE.sub("-", ascii_only).strip("-._")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or fallback)[:max_length]


def safe_stored_name(original_filename: str, image_format: str | None = None) -> str:
    """Genera un nombre interno seguro; nunca reutiliza el nombre subido."""
    suffix = Path(original_filename or "").suffix.lower()
    if image_format:
        fmt = image_format.upper()
        suffix = {"JPEG": ".jpg", "JPG": ".jpg", "PSD": ".psd"}.get(fmt, ".png")
    allowed = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_PSD_EXTENSIONS | ALLOWED_FONT_EXTENSIONS
    if suffix not in allowed:
        suffix = ".bin"
    return f"{uuid.uuid4().hex}{suffix}"


def detect_image_format(payload: bytes) -> str:
    for signature, fmt in _MAGIC_SIGNATURES:
        if payload.startswith(signature):
            return fmt
    head = payload[:1024].lstrip().lower()
    if head.startswith(b"<?xml") or b"<svg" in head:
        raise FileValidationError("SVG no está permitido en este MVP.")
    raise FileValidationError(
        "Formato no soportado. Se aceptan JPG/JPEG, PNG y PSD (solo como arte de origen)."
    )


def validate_image_bytes(
    payload: bytes,
    filename: str,
    *,
    max_bytes: int | None = None,
    min_side: int | None = None,
    allow_psd: bool = False,
) -> tuple[str, int, int]:
    """Valida tamaño, magic bytes y dimensiones. Devuelve (formato, ancho, alto)."""
    max_bytes = max_bytes or settings.max_upload_bytes
    min_side = settings.min_image_side if min_side is None else min_side

    if not payload:
        raise FileValidationError("El archivo está vacío.")
    if len(payload) > max_bytes:
        raise FileValidationError(
            f"El archivo supera el límite de {max_bytes // (1024 * 1024)} MB."
        )

    # El SVG se rechaza por contenido antes que por extensión: puede llevar scripts.
    head = payload[:1024].lstrip().lower()
    if head.startswith(b"<?xml") or b"<svg" in head:
        raise FileValidationError("SVG no está permitido en este MVP.")

    allowed_extensions = ALLOWED_IMAGE_EXTENSIONS | (
        ALLOWED_PSD_EXTENSIONS if allow_psd else set()
    )
    suffix = Path(filename or "").suffix.lower()
    if suffix and suffix not in allowed_extensions:
        readable = ", ".join(sorted(allowed_extensions))
        raise FileValidationError(f"Extensión no permitida: {suffix}. Use {readable}.")

    magic_format = detect_image_format(payload)
    if magic_format == "PSD" and not allow_psd:
        raise FileValidationError(
            "El PSD solo se admite como arte o KV de origen, no para este campo."
        )

    import io

    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(payload)) as image:
            real_format = (image.format or magic_format).upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:  # pragma: no cover - defensivo
        raise FileValidationError(f"La imagen no se puede decodificar: {exc}") from exc

    accepted = {"JPEG", "PNG"} | ({"PSD"} if allow_psd else set())
    if real_format not in accepted:
        raise FileValidationError(f"Formato interno no soportado: {real_format}.")
    if real_format != magic_format:
        raise FileValidationError("El contenido no coincide con la firma del archivo.")
    if width < min_side or height < min_side:
        raise FileValidationError(
            f"Dimensiones demasiado pequeñas ({width}x{height}). Mínimo {min_side}px por lado."
        )
    if max(width, height) > settings.max_image_side:
        raise FileValidationError(
            f"Dimensiones demasiado grandes ({width}x{height}). "
            f"Máximo {settings.max_image_side}px por lado."
        )
    return real_format, width, height


def validate_image_path(
    path: Path,
    filename: str,
    *,
    max_bytes: int | None = None,
    min_side: int | None = None,
    allow_psd: bool = False,
) -> tuple[str, int, int]:
    """Igual que `validate_image_bytes` pero sin cargar el archivo en memoria.

    Necesario para los PSD de KV, que pesan decenas de megabytes.
    """
    max_bytes = max_bytes or settings.max_upload_bytes
    min_side = settings.min_image_side if min_side is None else min_side

    if not path.exists() or not path.is_file():
        raise FileValidationError("El archivo no existe.")
    size = path.stat().st_size
    if size == 0:
        raise FileValidationError("El archivo está vacío.")
    if size > max_bytes:
        raise FileValidationError(
            f"El archivo supera el límite de {max_bytes // (1024 * 1024)} MB."
        )

    with path.open("rb") as handle:
        head = handle.read(1024)

    stripped = head.lstrip().lower()
    if stripped.startswith(b"<?xml") or b"<svg" in stripped:
        raise FileValidationError("SVG no está permitido en este MVP.")

    allowed_extensions = ALLOWED_IMAGE_EXTENSIONS | (
        ALLOWED_PSD_EXTENSIONS if allow_psd else set()
    )
    suffix = Path(filename or path.name).suffix.lower()
    if suffix and suffix not in allowed_extensions:
        readable = ", ".join(sorted(allowed_extensions))
        raise FileValidationError(f"Extensión no permitida: {suffix}. Use {readable}.")

    magic_format = detect_image_format(head)
    if magic_format == "PSD" and not allow_psd:
        raise FileValidationError(
            "El PSD solo se admite como arte o KV de origen, no para este campo."
        )

    try:
        with Image.open(path) as image:
            real_format = (image.format or magic_format).upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise FileValidationError(f"La imagen no se puede decodificar: {exc}") from exc

    accepted = {"JPEG", "PNG"} | ({"PSD"} if allow_psd else set())
    if real_format not in accepted:
        raise FileValidationError(f"Formato interno no soportado: {real_format}.")
    if real_format != magic_format:
        raise FileValidationError("El contenido no coincide con la firma del archivo.")
    if width < min_side or height < min_side:
        raise FileValidationError(
            f"Dimensiones demasiado pequeñas ({width}x{height}). Mínimo {min_side}px por lado."
        )
    if max(width, height) > settings.max_image_side:
        raise FileValidationError(
            f"Dimensiones demasiado grandes ({width}x{height}). "
            f"Máximo {settings.max_image_side}px por lado."
        )
    return real_format, width, height


def validate_font_bytes(payload: bytes, filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_FONT_EXTENSIONS:
        raise FileValidationError("La tipografía debe ser .ttf u .otf.")
    if len(payload) > 10 * 1024 * 1024:
        raise FileValidationError("La tipografía supera 10 MB.")
    try:
        import io

        from PIL import ImageFont

        ImageFont.truetype(io.BytesIO(payload), 24)
    except Exception as exc:  # noqa: BLE001
        raise FileValidationError(f"La tipografía no es válida: {exc}") from exc
    return suffix


def resolve_inside(base_dir: Path, relative: str) -> Path:
    """Resuelve `relative` dentro de `base_dir` bloqueando path traversal."""
    if relative is None:
        raise PathTraversalError("Ruta vacía.")
    raw = str(relative).replace("\\", "/").strip()
    if not raw:
        raise PathTraversalError("Ruta vacía.")
    if raw.startswith("/") or ".." in Path(raw).parts:
        raise PathTraversalError(f"Ruta no permitida: {relative}")
    base_resolved = base_dir.resolve()
    candidate = (base_resolved / raw).resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise PathTraversalError(f"Ruta fuera del proyecto: {relative}")
    return candidate


def validate_project_id(project_id: str) -> str:
    try:
        return str(uuid.UUID(str(project_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PathTraversalError("project_id inválido.") from exc
