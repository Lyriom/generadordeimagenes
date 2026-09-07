"""Reconoce qué producto hay en un recorte, mirándolo.

Antes el tamaño real de un producto salía del nombre del archivo, así que un
`producto1.png` no se podía medir y el motor tenía que igualar los altos. Pedirle
al usuario que renombre sus recortes es trasladarle a él un trabajo del motor.

Aquí se le enseña la foto a un modelo con visión y se le pide **una** familia de
una lista cerrada. Una lista cerrada y no texto libre porque así la respuesta o
es una de las que el motor sabe medir o no vale, sin nada que interpretar.

La imagen va reducida y con `detail: low`: para distinguir una cocina de un
cilindro no hacen falta píxeles, y así cada consulta cuesta lo mínimo. El
resultado se guarda en la capa, de modo que se paga una vez por recorte y la
generación sigue siendo local y determinista.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path

import httpx
from PIL import Image

from ..config import settings
from .base import ProviderUnavailableError

logger = logging.getLogger(__name__)

#: Lado máximo que se envía. `detail: low` reescala a 512 de todas formas.
_MAX_SIDE = 512


def _data_url(image_path: str | Path) -> str:
    with Image.open(image_path) as source:
        image = source.convert("RGBA")
        image.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
        # Fondo blanco: un PNG recortado sobre negro esconde los electrodomésticos
        # oscuros, que son la mitad del catálogo.
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        buffer = io.BytesIO()
        flat.save(buffer, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class OpenAIVisionProvider:
    """Identifica el producto de una foto entre una lista cerrada de familias."""

    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = settings.openai_api_key if api_key is None else api_key
        self.endpoint = settings.openai_vision_endpoint
        self.model = settings.openai_vision_model
        self.timeout = settings.product_vision_timeout

    def available(self) -> bool:
        return bool(self.api_key) and settings.enable_product_vision

    def identify(self, image_path: str | Path, options: list[tuple[str, str]]) -> str | None:
        """Devuelve la clave de familia elegida, o `None` si no reconoce el producto.

        `options` son pares (clave, nombre). Nunca lanza por una respuesta rara:
        lo que no esté en la lista se trata como «no lo sé», que es lo que activa
        el resto de la cascada.
        """
        if not self.available():
            raise ProviderUnavailableError(
                "OPENAI_API_KEY no está configurada o el reconocimiento está apagado."
            )
        catalogo = "\n".join(f"- {key}: {label}" for key, label in options)
        instruccion = (
            "Eres un catalogador de una tienda de electrodomésticos. Mira la foto "
            "del producto y responde SOLO con la clave de la familia a la que "
            "pertenece, tal cual, sin comillas ni explicación.\n\n"
            f"Claves posibles:\n{catalogo}\n\n"
            "Si el producto no es ninguna de esas, o no distingues cuál es, "
            "responde exactamente: desconocido"
        )
        # Sin tope de tokens a propósito: el nombre del campo cambia entre familias
        # de modelos (`max_tokens` / `max_completion_tokens`) y mandar el que no
        # toca es un 400. La respuesta es corta por el enunciado, y de la que se
        # alargue se rescata la clave igual (ver más abajo).
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruccion},
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_url(image_path), "detail": "low"},
                        },
                    ],
                }
            ],
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    f"OpenAI respondió {response.status_code}: {response.text[:200]}"
                )
            data = response.json()

        try:
            answer = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("Respuesta de visión sin contenido: %s", str(data)[:200])
            return None
        return self._parse(str(answer), [key for key, _ in options])

    @staticmethod
    def _parse(answer: str, valid: list[str]) -> str | None:
        """Saca la clave de la respuesta, aunque venga envuelta en una frase.

        Lo esperado es una palabra suelta. Pero un modelo puede contestar «Es una
        cocina (clave: cocina)», y tirar eso a la basura sería perder una
        identificación correcta. Se acepta si la frase menciona **una sola** de
        las claves; con dos no hay forma de saber cuál quiso decir.
        """
        limpio = re.sub(r"[^a-z_ ]", " ", answer.strip().lower())
        directo = limpio.strip().replace(" ", "_")
        if directo in valid:
            return directo
        encontradas = {
            key for key in valid if re.search(rf"(?<![a-z_]){re.escape(key)}(?![a-z_])", limpio)
        }
        if len(encontradas) == 1:
            return encontradas.pop()
        if limpio.strip() and limpio.strip() != "desconocido":
            logger.info("Visión devolvió algo que no identifica una familia: %r", answer)
        return None
