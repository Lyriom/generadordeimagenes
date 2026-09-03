"""Configuración central del backend (leída de variables de entorno / .env)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Ajustes del MVP. Sin dependencias externas obligatorias."""

    def __init__(self) -> None:
        self.app_name: str = os.getenv("APP_NAME", "Creative Variants MVP")
        self.app_version: str = "0.1.0"

        # --- Almacenamiento ---
        default_data = Path(__file__).resolve().parents[2] / "data"
        self.data_dir: Path = Path(os.getenv("DATA_DIR", str(default_data))).resolve()
        self.projects_dir: Path = self.data_dir / "projects"
        # Carpeta para dejar artes/KV grandes sin subirlos por el navegador.
        self.ingest_dir: Path = Path(
            os.getenv("INGEST_DIR", str(default_data / "ingest"))
        ).resolve()

        # --- Retención ---
        # Nada está pensado para quedarse: un PSD de 100 MB se convierte en
        # cientos de MB de capas, máscaras, fondos y variantes. Sin barrido el
        # disco del servidor se llena y el sitio se cae.
        #
        # El trabajo vive mientras dura la sesión y se borra pasadas estas
        # horas. El tope por cantidad es la red de seguridad para el caso de
        # muchas campañas seguidas dentro de la ventana de retención.
        self.project_retention_hours: int = _env_int("PROJECT_RETENTION_HOURS", 8)
        self.max_projects_kept: int = _env_int("MAX_PROJECTS_KEPT", 60)

        # --- Límites de subida ---
        # Un KV suelto pesa 100-150 MB y un pliego con cinco encima se va a
        # varios cientos. Este es el único tope de tamaño que queda: los proxys
        # de delante van sin límite a propósito, porque tres números que hay que
        # mantener iguales acaban siempre en uno que se quedó atrás. Aquí, en
        # cambio, se puede contestar con un mensaje que se entiende.
        self.max_upload_mb: int = _env_int("MAX_UPLOAD_MB", 4096)
        self.min_image_side: int = _env_int("MIN_IMAGE_SIDE", 200)
        # Lo que cuesta una imagen es su área, no su lado más largo. Medir por
        # el lado rechazaba un pliego de 11700x3100 (36 Mpx) y en cambio dejaba
        # pasar un cuadrado de 8000x8000 (64 Mpx), que ocupa casi el doble en
        # memoria: el pliego, que es el formato que este proyecto existe para
        # cortar en piezas, era justo el que no entraba.
        #
        # Ya no hay tope de lado. Con un mínimo por lado y un tope de área, la
        # forma queda acotada por los dos extremos y sobra el tercero, que era
        # además el que se desincronizaba con el del lienzo.
        #
        # Lo que queda no es un límite al trabajo: es la guarda contra las
        # "bombas de descompresión", archivos de pocos KB que declaran miles de
        # millones de píxeles para tumbar al que los abra. Está puesto en 400
        # Mpx, unas diez veces el pliego más grande que se ha visto (11700x3100
        # son 36), así que ningún arte real lo va a rozar. Pillow usa este mismo
        # valor (ver imaging.py), para avisar en castellano en vez de reventar
        # con DecompressionBombError.
        self.max_image_megapixels: int = _env_int("MAX_IMAGE_MEGAPIXELS", 400)

        # --- Proveedores ---
        # auto | sam | local | manual
        self.segmentation_provider: str = os.getenv("SEGMENTATION_PROVIDER", "auto").lower()
        self.sam_checkpoint: str | None = os.getenv("SAM_CHECKPOINT") or None
        self.sam_model_type: str = os.getenv("SAM_MODEL_TYPE", "vit_b")
        self.sam_variant: str = os.getenv("SAM_VARIANT", "sam2")  # sam2 | sam

        # auto | opencv | magnific | openai | flux | adobe
        self.inpainting_provider: str = os.getenv("INPAINTING_PROVIDER", "auto").lower()

        # --- Magnific (Mystic, Flux, Seedream, Ideogram…) ---
        # Una sola clave da acceso a todo el catálogo; el modelo se elige por
        # petición desde la interfaz y este valor es solo el predeterminado.
        self.magnific_api_key: str | None = os.getenv("MAGNIFIC_API_KEY") or None
        self.magnific_base_url: str = os.getenv(
            "MAGNIFIC_BASE_URL", "https://api.magnific.com"
        )
        self.magnific_model: str = os.getenv("MAGNIFIC_MODEL", "ideogram-image-edit")
        self.magnific_resolution: str = os.getenv("MAGNIFIC_RESOLUTION", "2k")
        # Ideogram: TURBO | DEFAULT | QUALITY
        self.magnific_rendering_speed: str = os.getenv(
            "MAGNIFIC_RENDERING_SPEED", "QUALITY"
        ).upper()
        # Mystic: realism | fluid | zen | flexible | super_real | editorial_portraits
        self.magnific_mystic_model: str = os.getenv("MAGNIFIC_MYSTIC_MODEL", "realism")
        # Mystic: automatic | magnific_illusio | magnific_sharpy | magnific_sparkle
        self.magnific_engine: str = os.getenv("MAGNIFIC_ENGINE", "automatic")
        self.magnific_structure_strength: int = _env_int("MAGNIFIC_STRUCTURE_STRENGTH", 70)
        self.magnific_adherence: int = _env_int("MAGNIFIC_ADHERENCE", 60)
        self.magnific_hdr: int = _env_int("MAGNIFIC_HDR", 40)
        self.magnific_creative_detailing: int = _env_int("MAGNIFIC_CREATIVE_DETAILING", 20)
        # Bajo este tamaño la imagen viaja en base64; por encima se sube primero.
        # Modelo de edición por instrucción para las fotos de ambiente: aísla el
        # producto sobre fondo plano y vacía la escena. Necesita entender la orden,
        # así que no puede ser el de máscara (ideogram-image-edit). Kontext está
        # hecho para editar conservando el resto: medido con un KV real, deja la
        # puerta, la planta, la repisa, el cuadro y la alfombra en su sitio, donde
        # Seedream reencuadraba el cuarto entero.
        self.magnific_scene_model: str = os.getenv(
            "MAGNIFIC_SCENE_MODEL", "flux-kontext-max"
        )
        self.magnific_inline_max_mb: int = _env_int("MAGNIFIC_INLINE_MAX_MB", 6)
        # Suavizado del borde al recomponer lo generado sobre el arte original.
        self.magnific_feather: int = _env_int("MAGNIFIC_FEATHER", 6)
        self.openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
        self.openai_image_model: str = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
        self.openai_image_quality: str = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
        self.openai_image_endpoint: str = os.getenv(
            "OPENAI_IMAGE_ENDPOINT", "https://api.openai.com/v1/images/edits"
        )
        self.bfl_api_key: str | None = os.getenv("BFL_API_KEY") or None
        self.bfl_endpoint: str = os.getenv(
            "BFL_ENDPOINT", "https://api.bfl.ai/v1/flux-pro-1.0-fill"
        )
        self.adobe_client_id: str | None = os.getenv("ADOBE_CLIENT_ID") or None
        self.adobe_client_secret: str | None = os.getenv("ADOBE_CLIENT_SECRET") or None

        self.psd_max_layers: int = _env_int("PSD_MAX_LAYERS", 60)
        self.psd_min_opaque_ratio: float = float(
            os.getenv("PSD_MIN_OPAQUE_RATIO", "0.002")
        )

        self.enable_ocr: bool = _env_bool("ENABLE_OCR", True)
        self.ocr_lang: str = os.getenv("OCR_LANG", "es")

        # --- Render ---
        self.default_font: str = os.getenv(
            "DEFAULT_FONT",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
        self.default_font_bold: str = os.getenv(
            "DEFAULT_FONT_BOLD",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        )
        self.max_variants: int = _env_int("MAX_VARIANTS", 30)
        self.min_variants: int = _env_int("MIN_VARIANTS", 4)
        self.request_timeout: int = _env_int("PROVIDER_TIMEOUT", 180)

        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.ingest_dir.mkdir(parents=True, exist_ok=True)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_image_pixels(self) -> int:
        return self.max_image_megapixels * 1_000_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
