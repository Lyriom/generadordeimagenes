"""Reconstrucción de fondos con GPT Image mediante la API oficial de OpenAI.

La imagen comercial final no se delega al modelo: solo se reconstruye el fondo.
Producto, logo y copy se recomponen después desde sus capas originales.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

import httpx
from PIL import Image

from ..config import settings
from ..services.imaging import resize_cover
from .base import ProviderUnavailableError


#: Los modelos de imagen de OpenAI que sirven para esto: los dos admiten
#: `/v1/images/edits` con máscara, que es lo único que se les pide aquí.
CATALOG: tuple[dict[str, object], ...] = (
    {
        "id": "gpt-image-2.5-sunburst",
        "label": "GPT Image 2.5 Sunburst",
        "description": (
            "El más capaz de OpenAI para generar y editar. Inpainting con máscara: "
            "repinta solo el hueco de lo borrado."
        ),
        "provider": "openai",
        "supports_mask": True,
        "resolutions": [],
    },
    {
        "id": "gpt-image-2.5-flare",
        "label": "GPT Image 2.5 Flare",
        "description": "Rápido y más barato, con la misma edición por máscara.",
        "provider": "openai",
        "supports_mask": True,
        "resolutions": [],
    },
    {
        "id": "gpt-image-2",
        "label": "GPT Image 2",
        "description": "La generación anterior. Sigue disponible; 2.5 la mejora.",
        "provider": "openai",
        "supports_mask": True,
        "resolutions": [],
    },
)

MODELS: dict[str, dict[str, object]] = {str(item["id"]): item for item in CATALOG}
DEFAULT_MODEL = "gpt-image-2.5-sunburst"


def model_catalog() -> list[dict[str, object]]:
    """Catálogo legible para la API y la interfaz (sin exponer claves)."""
    return [dict(item) for item in CATALOG]


def _openai_mask(mask_path: str) -> bytes:
    """Convierte blanco=borrar a alfa transparente, semántica de Images Edits."""
    with Image.open(mask_path) as source:
        gray = source.convert("L")
        rgba = Image.new("RGBA", gray.size, (255, 255, 255, 255))
        # Tabla de 256 entradas en vez de una lambda: se aplica de una vez en C.
        rgba.putalpha(gray.point([0 if value > 127 else 255 for value in range(256)]))
        output = io.BytesIO()
        rgba.save(output, format="PNG")
        return output.getvalue()


class OpenAIInpaintProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = settings.openai_api_key if api_key is None else api_key
        self.endpoint = settings.openai_image_endpoint
        self.model_id = (model or settings.openai_image_model or DEFAULT_MODEL).strip()
        self.quality = settings.openai_image_quality
        self.timeout = settings.request_timeout

    @property
    def model(self) -> str:
        return self.model_id

    def available(self) -> bool:
        return bool(self.api_key)

    def unavailable_reason(self) -> str | None:
        """Por qué no se puede usar, en una frase que sirva en pantalla."""
        if not self.api_key:
            return "falta OPENAI_API_KEY en el servidor"
        if self.model_id not in MODELS:
            # No se rechaza: OpenAI publica modelos nuevos antes de que este
            # catálogo los conozca, y bloquear uno válido es peor que avisar.
            return None
        return None

    def fill(
        self,
        image_path: str,
        mask_path: str,
        prompt: str | None = None,
        output_path: str | None = None,
    ) -> str:
        if not self.available():
            raise ProviderUnavailableError("OPENAI_API_KEY no está configurada.")

        base_instruction = (
            "Reconstruye exclusivamente un fondo publicitario limpio y premium, "
            "coherente con los colores, iluminación y estilo de la imagen de referencia. "
            "Elimina productos, personas, logos, letras, precios y marcas. No incluyas "
            "texto ni objetos protagonistas. No copies ni inventes productos vistos en la "
            "referencia. Continúa formas, texturas y sombras de manera natural. Deja una "
            "zona visual limpia para componer después un único producto recortado."
        )
        instruction = (
            f"{base_instruction} Dirección artística adicional: {prompt}"
            if prompt
            else base_instruction
        )
        files = {
            "image": ("reference.png", Path(image_path).read_bytes(), "image/png"),
            "mask": ("mask.png", _openai_mask(mask_path), "image/png"),
        }
        data = {
            "model": self.model_id,
            "prompt": instruction,
            "quality": self.quality,
            "size": "auto",
            "output_format": "png",
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files=files,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise ProviderUnavailableError(
                f"OpenAI Images devolvió {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailableError(f"OpenAI Images no respondió: {exc}") from exc

        outputs = payload.get("data") or []
        encoded = outputs[0].get("b64_json") if outputs else None
        if not encoded:
            raise ProviderUnavailableError("OpenAI Images terminó sin devolver una imagen.")

        try:
            generated = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError("La imagen devuelta por OpenAI no es válida.") from exc

        with Image.open(image_path) as source:
            expected = source.size
        if generated.size != expected:
            generated = resize_cover(generated, *expected)

        target = Path(output_path) if output_path else Path(image_path).with_name("background.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        generated.save(target, format="PNG", optimize=True)
        generated.close()
        return str(target)
