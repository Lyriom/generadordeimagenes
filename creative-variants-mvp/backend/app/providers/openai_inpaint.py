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


def _openai_mask(mask_path: str) -> bytes:
    """Convierte blanco=borrar a alfa transparente, semántica de Images Edits."""
    with Image.open(mask_path) as source:
        gray = source.convert("L")
        rgba = Image.new("RGBA", gray.size, (255, 255, 255, 255))
        rgba.putalpha(gray.point(lambda value: 0 if value > 127 else 255))
        output = io.BytesIO()
        rgba.save(output, format="PNG")
        return output.getvalue()


class OpenAIInpaintProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = settings.openai_api_key if api_key is None else api_key
        self.endpoint = settings.openai_image_endpoint
        self.model = settings.openai_image_model
        self.quality = settings.openai_image_quality
        self.timeout = settings.request_timeout

    def available(self) -> bool:
        return bool(self.api_key)

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
            "model": self.model,
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
