"""Proveedor opcional FLUX Fill / Erase (Black Forest Labs) vía BFL_API_KEY.

Nunca se activa sin clave. Si la llamada falla, el orquestador cae a OpenCV.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import httpx

from ..config import settings
from .base import ProviderUnavailableError

logger = logging.getLogger(__name__)


class FluxInpaintProvider:
    name = "flux"

    def __init__(self, api_key: str | None = None, endpoint: str | None = None) -> None:
        self.api_key = api_key or settings.bfl_api_key
        self.endpoint = endpoint or settings.bfl_endpoint
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
            raise ProviderUnavailableError("BFL_API_KEY no está configurada.")

        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        mask_b64 = base64.b64encode(Path(mask_path).read_bytes()).decode()
        payload = {
            "image": image_b64,
            "mask": mask_b64,
            "prompt": prompt or "clean advertising background, seamless, no objects, no text",
            "output_format": "png",
            "safety_tolerance": 2,
        }
        headers = {"x-key": self.api_key, "Content-Type": "application/json"}

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.endpoint, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                result_url = self._poll(client, data, headers)
                image_bytes = client.get(result_url, timeout=self.timeout).content
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"FLUX no respondió correctamente: {exc}") from exc

        target = Path(output_path) if output_path else Path(image_path).with_name("background.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image_bytes)
        return str(target)

    def _poll(self, client: httpx.Client, data: dict, headers: dict) -> str:
        """La API de BFL es asíncrona: devuelve un polling_url."""
        direct = (data.get("result") or {}).get("sample") if isinstance(data, dict) else None
        if direct:
            return direct
        polling_url = data.get("polling_url") or data.get("result_url")
        if not polling_url:
            raise ProviderUnavailableError("Respuesta de FLUX sin polling_url.")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            poll = client.get(polling_url, headers=headers, timeout=self.timeout).json()
            status = (poll.get("status") or "").lower()
            if status in {"ready", "succeeded", "complete"}:
                sample = (poll.get("result") or {}).get("sample")
                if not sample:
                    raise ProviderUnavailableError("FLUX terminó sin imagen.")
                return sample
            if status in {"error", "failed", "content_moderated", "request_moderated"}:
                raise ProviderUnavailableError(f"FLUX devolvió estado {status}.")
            time.sleep(1.5)
        raise ProviderUnavailableError("Tiempo de espera agotado en FLUX.")
