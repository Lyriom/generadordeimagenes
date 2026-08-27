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

        # --- Límites de subida ---
        # Los PSD de KV pesan 60-100 MB: el límite debe cubrirlos.
        self.max_upload_mb: int = _env_int("MAX_UPLOAD_MB", 250)
        self.min_image_side: int = _env_int("MIN_IMAGE_SIDE", 200)
        self.max_image_side: int = _env_int("MAX_IMAGE_SIDE", 8000)

        # --- Proveedores ---
        # auto | sam | local | manual
        self.segmentation_provider: str = os.getenv("SEGMENTATION_PROVIDER", "auto").lower()
        self.sam_checkpoint: str | None = os.getenv("SAM_CHECKPOINT") or None
        self.sam_model_type: str = os.getenv("SAM_MODEL_TYPE", "vit_b")
        self.sam_variant: str = os.getenv("SAM_VARIANT", "sam2")  # sam2 | sam

        # auto | opencv | openai | flux | adobe
        self.inpainting_provider: str = os.getenv("INPAINTING_PROVIDER", "auto").lower()
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
