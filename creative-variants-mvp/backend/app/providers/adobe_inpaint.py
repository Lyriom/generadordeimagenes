"""Proveedor opcional Adobe Firefly (Generative Fill) vía credenciales IMS.

Requiere ADOBE_CLIENT_ID y ADOBE_CLIENT_SECRET. Firefly exige que la imagen y la
máscara estén accesibles por URL (o subidas a su storage), por lo que este
proveedor solo funciona si `ADOBE_UPLOAD_BASE_URL` expone la carpeta del
proyecto. En el MVP queda documentado y desactivado por defecto.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from ..config import settings
from .base import ProviderUnavailableError

logger = logging.getLogger(__name__)

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FIREFLY_FILL_URL = "https://firefly-api.adobe.io/v3/images/fill"


class AdobeInpaintProvider:
    name = "adobe"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        upload_base_url: str | None = None,
    ) -> None:
        self.client_id = client_id or settings.adobe_client_id
        self.client_secret = client_secret or settings.adobe_client_secret
        self.upload_base_url = upload_base_url or os.getenv("ADOBE_UPLOAD_BASE_URL") or None
        self.timeout = settings.request_timeout

    def available(self) -> bool:
        return bool(self.client_id and self.client_secret and self.upload_base_url)

    def _token(self, client: httpx.Client) -> str:
        response = client.post(
            IMS_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "openid,AdobeID,firefly_api,ff_apis",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise ProviderUnavailableError("Adobe IMS no devolvió access_token.")
        return token

    def fill(
        self,
        image_path: str,
        mask_path: str,
        prompt: str | None = None,
        output_path: str | None = None,
    ) -> str:
        # Se comprueban aquí y no con `available()` para que quede a la vista que
        # a partir de esta línea las tres existen: antes se llamaba `.rstrip` y
        # se mandaba la clave sobre lo que el tipo declaraba como opcional.
        client_id, base = self.client_id, self.upload_base_url
        if not (client_id and self.client_secret and base):
            raise ProviderUnavailableError(
                "Adobe Firefly requiere ADOBE_CLIENT_ID, ADOBE_CLIENT_SECRET y "
                "ADOBE_UPLOAD_BASE_URL."
            )
        image_url = f"{base.rstrip('/')}/{Path(image_path).name}"
        mask_url = f"{base.rstrip('/')}/{Path(mask_path).name}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                token = self._token(client)
                response = client.post(
                    FIREFLY_FILL_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "x-api-key": client_id,
                        "Content-Type": "application/json",
                    },
                    json={
                        "prompt": prompt or "clean seamless advertising background",
                        "image": {
                            "source": {"url": image_url},
                            "mask": {"url": mask_url},
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()
                outputs = data.get("outputs") or []
                if not outputs:
                    raise ProviderUnavailableError("Firefly no devolvió salidas.")
                url = outputs[0].get("image", {}).get("url")
                if not url:
                    raise ProviderUnavailableError("Firefly no devolvió URL de imagen.")
                image_bytes = client.get(url, timeout=self.timeout).content
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Adobe Firefly falló: {exc}") from exc

        target = Path(output_path) if output_path else Path(image_path).with_name("background.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image_bytes)
        return str(target)
