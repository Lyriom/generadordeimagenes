"""Generación de imagen con la API de Magnific (Mystic, Flux, Seedream, Ideogram…).

Un único cliente cubre todo el catálogo: lo que cambia entre modelos es el
endpoint y la forma de entregar la imagen de partida (máscara real, referencia
de estructura o imagen de entrada). Los modelos sin máscara nativa devuelven la
imagen completa; en ese caso el resultado se recompone localmente solo dentro
de la zona borrada, así el resto del arte queda intacto pixel a pixel.

Nunca se activa sin MAGNIFIC_API_KEY. Si la llamada falla, el orquestador de
inpainting cae al proveedor local (OpenCV).

Referencia: https://docs.magnific.com/api-reference (autenticación por cabecera
`x-magnific-api-key`; todos los endpoints son asíncronos con `task_id`).
"""
from __future__ import annotations

import base64
import io
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image

from ..config import settings
from ..services.imaging import blur, read_mask, resize_cover
from .base import ProviderUnavailableError
from .opencv_inpaint import OpenCVInpaintProvider

logger = logging.getLogger(__name__)

# Proporciones que aceptan los endpoints, con su valor numérico para elegir la
# más parecida al arte original (luego se recorta al tamaño exacto).
RATIO_VALUES: dict[str, float] = {
    "square_1_1": 1.0,
    "classic_4_3": 4 / 3,
    "traditional_3_4": 3 / 4,
    "widescreen_16_9": 16 / 9,
    "social_story_9_16": 9 / 16,
    "smartphone_horizontal_20_9": 20 / 9,
    "smartphone_vertical_9_20": 9 / 20,
    "standard_3_2": 3 / 2,
    "portrait_2_3": 2 / 3,
    "horizontal_2_1": 2.0,
    "vertical_1_2": 0.5,
    "social_5_4": 5 / 4,
    "social_post_4_5": 4 / 5,
    "cinematic_21_9": 21 / 9,
    "1:1": 1.0,
    "2:3": 2 / 3,
    "3:2": 3 / 2,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "5:4": 5 / 4,
    "4:5": 4 / 5,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "21:9": 21 / 9,
}

MYSTIC_RATIOS = (
    "square_1_1", "classic_4_3", "traditional_3_4", "widescreen_16_9",
    "social_story_9_16", "smartphone_horizontal_20_9", "smartphone_vertical_9_20",
    "standard_3_2", "portrait_2_3", "horizontal_2_1", "vertical_1_2",
    "social_5_4", "social_post_4_5",
)
FLUX_RATIOS = (
    "square_1_1", "classic_4_3", "traditional_3_4", "widescreen_16_9",
    "social_story_9_16", "standard_3_2", "portrait_2_3", "horizontal_2_1",
    "vertical_1_2", "social_post_4_5",
)
KONTEXT_RATIOS = (
    "square_1_1", "classic_4_3", "traditional_3_4", "widescreen_16_9",
    "social_story_9_16", "standard_3_2", "portrait_2_3", "horizontal_2_1",
    "vertical_1_2", "social_post_4_5",
)
SEEDREAM_RATIOS = (
    "square_1_1", "widescreen_16_9", "social_story_9_16", "portrait_2_3",
    "traditional_3_4", "standard_3_2", "classic_4_3",
)
SEEDREAM_45_RATIOS = SEEDREAM_RATIOS + ("cinematic_21_9",)
NANO_RATIOS = ("1:1", "2:3", "3:2", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16", "21:9")


@dataclass(frozen=True)
class MagnificModel:
    """Un modelo del catálogo y cómo se le habla.

    `mode` decide la forma del payload:
      - ``mask``       → la API acepta imagen + máscara (inpainting real).
      - ``structure``  → la imagen guía la estructura (Mystic).
      - ``image``      → campo `input_image` con la imagen de partida.
      - ``references`` → lista `reference_images` de cadenas.
      - ``reference_objects`` → lista `reference_images` de objetos con mime.
    """

    id: str
    label: str
    endpoint: str
    mode: str
    description: str
    aspect_ratios: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    # Algunos endpoints exigen URL pública: en esos hay que subir el archivo.
    inline_ok: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def supports_mask(self) -> bool:
        return self.mode == "mask"


CATALOG: tuple[MagnificModel, ...] = (
    MagnificModel(
        id="ideogram-image-edit",
        label="Ideogram Inpainting",
        endpoint="/v1/ai/ideogram-image-edit",
        mode="mask",
        description=(
            "Único con máscara real: solo repinta el hueco de los productos "
            "borrados. La opción más fiel al arte original."
        ),
    ),
    MagnificModel(
        id="mystic",
        label="Mystic (Magnific)",
        endpoint="/v1/ai/mystic",
        mode="structure",
        description=(
            "Modelo propio de Magnific. Fotorrealismo alto en 1K/2K/4K guiado "
            "por la estructura del KV. Ideal para fondos publicitarios."
        ),
        aspect_ratios=MYSTIC_RATIOS,
        resolutions=("1k", "2k", "4k"),
    ),
    MagnificModel(
        id="flux-kontext-pro",
        label="Flux Kontext Pro",
        endpoint="/v1/ai/text-to-image/flux-kontext-pro",
        mode="image",
        description="Edición por instrucción con buena coherencia de contexto.",
        aspect_ratios=KONTEXT_RATIOS,
        inline_ok=False,
    ),
    MagnificModel(
        id="flux-kontext-max",
        label="Flux Kontext Max",
        endpoint="/v1/ai/text-to-image/flux-kontext-max",
        mode="image",
        description="Como Kontext Pro pero con más fidelidad al pedido (y más costo).",
        aspect_ratios=KONTEXT_RATIOS,
        inline_ok=False,
    ),
    MagnificModel(
        id="flux-2-pro",
        label="Flux 2 Pro",
        endpoint="/v1/ai/text-to-image/flux-2-pro",
        mode="image",
        description="Calidad alta con tamaño libre en píxeles.",
        extras={"size_in_pixels": True},
    ),
    MagnificModel(
        id="flux-2-flex",
        label="Flux 2 Flex",
        endpoint="/v1/ai/text-to-image/flux-2-flex",
        mode="image",
        description="Flux 2 con control fino de pasos y guía.",
        aspect_ratios=FLUX_RATIOS,
    ),
    MagnificModel(
        id="flux-2-turbo",
        label="Flux 2 Turbo",
        endpoint="/v1/ai/text-to-image/flux-2-turbo",
        mode="image",
        description="Flux 2 optimizado para velocidad y costo.",
        aspect_ratios=FLUX_RATIOS,
    ),
    MagnificModel(
        id="flux-2-klein",
        label="Flux 2 Klein",
        endpoint="/v1/ai/text-to-image/flux-2-klein",
        mode="image",
        description="El más rápido de la familia Flux 2 (1K o 2K).",
        aspect_ratios=FLUX_RATIOS,
        resolutions=("1k", "2k"),
    ),
    MagnificModel(
        id="seedream-v4-edit",
        label="Seedream 4 Edit",
        endpoint="/v1/ai/text-to-image/seedream-v4-edit",
        mode="references",
        description="Edición por instrucción que preserva bien texturas y color.",
        aspect_ratios=SEEDREAM_RATIOS,
    ),
    MagnificModel(
        id="seedream-v4-5-edit",
        label="Seedream 4.5 Edit",
        endpoint="/v1/ai/text-to-image/seedream-v4-5-edit",
        mode="references",
        description="Seedream 4.5: mejor consistencia de iluminación al editar.",
        aspect_ratios=SEEDREAM_45_RATIOS,
    ),
    MagnificModel(
        id="seedream-v5-lite-edit",
        label="Seedream 5 Lite Edit",
        endpoint="/v1/ai/text-to-image/seedream-v5-lite-edit",
        mode="references",
        description="Seedream 5 en su versión económica.",
        aspect_ratios=SEEDREAM_45_RATIOS,
    ),
    MagnificModel(
        id="seedream-v5-pro-edit",
        label="Seedream 5 Pro Edit",
        endpoint="/v1/ai/text-to-image/seedream-v5-pro-edit",
        mode="references",
        description="Seedream 5 Pro: máxima fidelidad de detalle al editar.",
        aspect_ratios=SEEDREAM_45_RATIOS,
    ),
    MagnificModel(
        id="gemini-2-5-flash-image-preview",
        label="Gemini 2.5 Flash Image (Nano Banana)",
        endpoint="/v1/ai/gemini-2-5-flash-image-preview",
        mode="references",
        description="Rápido y barato; entiende bien instrucciones en español.",
    ),
    MagnificModel(
        id="nano-banana-pro",
        label="Nano Banana Pro",
        endpoint="/v1/ai/text-to-image/nano-banana-pro",
        mode="reference_objects",
        description="Gemini 3 Pro Image hasta 4K. Requiere subir la referencia.",
        aspect_ratios=NANO_RATIOS,
        resolutions=("1K", "2K", "4K"),
        inline_ok=False,
    ),
    MagnificModel(
        id="nano-banana-pro-flash",
        label="Nano Banana Pro Flash",
        endpoint="/v1/ai/text-to-image/nano-banana-pro-flash",
        mode="reference_objects",
        description="Versión rápida de Nano Banana Pro.",
        aspect_ratios=NANO_RATIOS,
        resolutions=("1K", "2K", "4K"),
        inline_ok=False,
    ),
)

MODELS: dict[str, MagnificModel] = {model.id: model for model in CATALOG}
DEFAULT_MODEL = "ideogram-image-edit"

# Qué se le pide al modelo. El arte final no se delega: solo el fondo.
MASK_INSTRUCTION = (
    "Rellena la zona marcada con fondo publicitario limpio y continuo, "
    "coherente con los colores, la iluminación y las texturas del resto de la "
    "imagen. No dibujes productos, personas, logotipos, letras ni precios. "
    "Continúa de forma natural las formas, degradados y sombras que ya existen."
)
EDIT_INSTRUCTION = (
    "Limpia y reconstruye el fondo publicitario de esta imagen manteniendo su "
    "paleta, iluminación y estilo. Elimina cualquier resto de producto, "
    "persona, logotipo, texto o precio y deja una superficie continua y premium "
    "con espacio libre para componer después un único producto recortado. "
    "No añadas texto ni objetos protagonistas."
)


def model_catalog() -> list[dict[str, Any]]:
    """Catálogo legible para la API y la interfaz (sin exponer claves)."""
    return [
        {
            "id": model.id,
            "label": model.label,
            "description": model.description,
            "provider": "magnific",
            "supports_mask": model.supports_mask,
            "resolutions": list(model.resolutions),
        }
        for model in CATALOG
    ]


def resolve_model(model_id: str | None) -> MagnificModel:
    key = (model_id or settings.magnific_model or DEFAULT_MODEL).strip().lower()
    if key not in MODELS:
        raise ProviderUnavailableError(
            f"Modelo de Magnific desconocido: {model_id}. "
            f"Opciones: {', '.join(sorted(MODELS))}."
        )
    return MODELS[key]


def closest_ratio(width: int, height: int, choices: tuple[str, ...]) -> str:
    target = width / max(1, height)
    return min(choices, key=lambda name: abs(RATIO_VALUES[name] - target))


def _invert_mask_png(mask_path: str) -> bytes:
    """Ideogram edita el NEGRO; nuestra máscara marca en blanco lo que se borra."""
    with Image.open(mask_path) as source:
        gray = source.convert("L")
        inverted = gray.point(lambda value: 0 if value > 127 else 255)
        output = io.BytesIO()
        inverted.save(output, format="PNG")
        return output.getvalue()


def _data_uri(path: str | Path, data: bytes | None = None) -> str:
    raw = data if data is not None else Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


class MagnificClient:
    """Cliente HTTP mínimo: subir archivos, lanzar tarea y esperar resultado."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.api_key = settings.magnific_api_key if api_key is None else api_key
        self.base_url = (base_url or settings.magnific_base_url).rstrip("/")
        self.timeout = timeout or settings.request_timeout

    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def headers(self) -> dict[str, str]:
        return {"x-magnific-api-key": self.api_key or "", "Content-Type": "application/json"}

    # ------------------------------------------------------------------ subida
    def upload(self, client: httpx.Client, path: str | Path, data: bytes | None = None) -> str:
        """Sube un archivo y devuelve la `asset_url` pública temporal (~24 h)."""
        raw = data if data is not None else Path(path).read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            mime = "image/png"
        response = client.post(
            f"{self.base_url}/v1/ai/uploads/request-url",
            headers=self.headers,
            json={"files": [{"content_type": mime}]},
        )
        response.raise_for_status()
        files = (response.json() or {}).get("files") or []
        if not files:
            raise ProviderUnavailableError("Magnific no devolvió una URL de subida.")
        slot = files[0]
        put = client.put(
            slot["upload_url"],
            headers=slot.get("headers") or {"Content-Type": mime},
            content=raw,
        )
        put.raise_for_status()
        return slot["asset_url"]

    # ------------------------------------------------------------------- tarea
    def submit(self, client: httpx.Client, endpoint: str, payload: dict) -> dict:
        response = client.post(
            f"{self.base_url}{endpoint}", headers=self.headers, json=payload
        )
        response.raise_for_status()
        return (response.json() or {}).get("data") or {}

    def wait(self, client: httpx.Client, endpoint: str, task: dict) -> str:
        """Espera a COMPLETED y devuelve la URL de la primera imagen generada."""
        generated = task.get("generated") or []
        if generated:
            return generated[0]
        task_id = task.get("task_id")
        if not task_id:
            raise ProviderUnavailableError("Magnific respondió sin task_id.")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(2.0)
            poll = client.get(
                f"{self.base_url}{endpoint}/{task_id}", headers=self.headers
            )
            poll.raise_for_status()
            data = (poll.json() or {}).get("data") or {}
            status = str(data.get("status") or "").upper()
            if status == "COMPLETED":
                images = data.get("generated") or []
                if not images:
                    raise ProviderUnavailableError("Magnific terminó sin imagen.")
                return images[0]
            if status == "FAILED":
                raise ProviderUnavailableError("Magnific devolvió la tarea como FAILED.")
        raise ProviderUnavailableError(
            f"Tiempo de espera agotado en Magnific ({self.timeout}s)."
        )

    def download(self, client: httpx.Client, url: str) -> bytes:
        response = client.get(url, timeout=self.timeout, follow_redirects=True)
        response.raise_for_status()
        return response.content


class MagnificInpaintProvider:
    """Reconstruye el fondo con el modelo de Magnific que se haya elegido."""

    name = "magnific"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.client = MagnificClient(api_key=api_key)
        self.api_key = self.client.api_key
        self.model_id = (model or settings.magnific_model or DEFAULT_MODEL).strip().lower()
        self.timeout = self.client.timeout

    def available(self) -> bool:
        return self.client.available() and self.model_id in MODELS

    @property
    def model(self) -> MagnificModel:
        return resolve_model(self.model_id)

    # ---------------------------------------------------------------- payloads
    def _payload(
        self,
        model: MagnificModel,
        image_ref: str,
        mask_ref: str | None,
        prompt: str,
        size: tuple[int, int],
    ) -> dict[str, Any]:
        width, height = size
        payload: dict[str, Any] = {"prompt": prompt}
        if model.aspect_ratios:
            payload["aspect_ratio"] = closest_ratio(width, height, model.aspect_ratios)
        if model.resolutions:
            wanted = settings.magnific_resolution.strip()
            options = model.resolutions
            match = next((item for item in options if item.lower() == wanted.lower()), None)
            payload["resolution"] = match or options[-1]

        if model.mode == "mask":
            payload["image"] = image_ref
            payload["mask"] = mask_ref
            payload["rendering_speed"] = settings.magnific_rendering_speed
            payload["magic_prompt"] = "OFF"
            payload["style_type"] = "REALISTIC"
        elif model.mode == "structure":
            payload["structure_reference"] = image_ref
            payload["structure_strength"] = settings.magnific_structure_strength
            payload["style_reference"] = image_ref
            payload["adherence"] = settings.magnific_adherence
            payload["hdr"] = settings.magnific_hdr
            payload["creative_detailing"] = settings.magnific_creative_detailing
            payload["model"] = settings.magnific_mystic_model
            payload["engine"] = settings.magnific_engine
        elif model.mode == "image":
            payload["input_image"] = image_ref
            if model.extras.get("size_in_pixels"):
                payload["width"], payload["height"] = _clamp_pixels(width, height)
        elif model.mode == "references":
            payload["reference_images"] = [image_ref]
        elif model.mode == "reference_objects":
            payload["reference_images"] = [
                {"image": image_ref, "mime_type": "image/png"}
            ]
        else:  # pragma: no cover - el catálogo no define otros modos
            raise ProviderUnavailableError(f"Modo no soportado: {model.mode}")
        return payload

    # -------------------------------------------------------------------- fill
    def fill(
        self,
        image_path: str,
        mask_path: str,
        prompt: str | None = None,
        output_path: str | None = None,
    ) -> str:
        if not self.client.available():
            raise ProviderUnavailableError("MAGNIFIC_API_KEY no está configurada.")
        model = self.model

        with Image.open(image_path) as source:
            size = source.size

        target = (
            Path(output_path) if output_path else Path(image_path).with_name("background.png")
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        if model.supports_mask:
            reference_path = Path(image_path)
            reference_bytes: bytes | None = None
            instruction = MASK_INSTRUCTION
        else:
            # Sin máscara nativa: se manda el arte ya limpiado en local, así el
            # modelo no tiene que "adivinar" qué producto quitar.
            reference_path = target.with_name("magnific_reference.png")
            OpenCVInpaintProvider().fill(
                image_path, mask_path, prompt=None, output_path=str(reference_path)
            )
            reference_bytes = None
            instruction = EDIT_INSTRUCTION
        if prompt:
            instruction = f"{instruction} Dirección artística adicional: {prompt}"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                image_ref = self._image_ref(client, model, reference_path, reference_bytes)
                mask_ref = None
                if model.supports_mask:
                    mask_ref = self._image_ref(
                        client, model, Path(mask_path), _invert_mask_png(mask_path)
                    )
                payload = self._payload(model, image_ref, mask_ref, instruction, size)
                task = self.client.submit(client, model.endpoint, payload)
                result_url = self.client.wait(client, model.endpoint, task)
                image_bytes = self.client.download(client, result_url)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise ProviderUnavailableError(
                f"Magnific ({model.id}) devolvió {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderUnavailableError(f"Magnific ({model.id}) no respondió: {exc}") from exc
        finally:
            if not model.supports_mask:
                Path(reference_path).unlink(missing_ok=True)

        try:
            generated = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                f"La imagen devuelta por Magnific ({model.id}) no es válida."
            ) from exc

        composed = compose_inpaint(
            image_path, mask_path, generated, feather=settings.magnific_feather
        )
        composed.save(target, format="PNG", optimize=True)
        generated.close()
        composed.close()
        logger.info("Magnific: fondo reconstruido con %s", model.id)
        return str(target)

    def _image_ref(
        self,
        client: httpx.Client,
        model: MagnificModel,
        path: Path,
        data: bytes | None,
    ) -> str:
        """Base64 si el archivo es pequeño y el modelo lo admite; si no, subida."""
        raw = data if data is not None else path.read_bytes()
        limit = settings.magnific_inline_max_mb * 1024 * 1024
        if model.inline_ok and len(raw) <= limit:
            return _data_uri(path, raw)
        return self.client.upload(client, path, raw)


def _clamp_pixels(width: int, height: int, maximum: int = 2048) -> tuple[int, int]:
    """Flux 2 Pro pide ancho/alto explícitos: se respeta la proporción del arte."""
    scale = min(1.0, maximum / max(width, height))
    return max(64, int(width * scale)), max(64, int(height * scale))


def compose_inpaint(
    image_path: str,
    mask_path: str,
    generated: Image.Image,
    feather: int = 6,
) -> Image.Image:
    """Pega lo generado solo dentro de la máscara, con borde suavizado."""
    with Image.open(image_path) as source:
        original = source.convert("RGB")
        width, height = original.size
        if generated.size != (width, height):
            generated = resize_cover(generated, width, height)

        mask = read_mask(mask_path)
        if mask.shape[:2] != (height, width):
            mask = np.array(
                Image.fromarray(mask).resize((width, height), Image.NEAREST)
            )
        alpha = Image.fromarray(mask.astype("uint8"), mode="L")
        if feather > 0:
            alpha = blur(alpha, feather)

        composed = original.copy()
        composed.paste(generated, (0, 0), alpha)
        return composed


class MagnificCutoutProvider:
    """Recorta el sujeto de una foto con `remove-background` de Magnific.

    Resuelve el caso de los KV en los que el producto **no viene como capa**:
    sofás, mesas o zapatos aplanados dentro de una sola fotografía de ambiente.
    Sin esto no hay nada que mover ni que reemplazar.

    El endpoint es síncrono y solo acepta URL, así que la imagen se sube antes
    con la Upload Files API. Las URL que devuelve caducan a los 5 minutos.
    """

    name = "magnific-cutout"
    endpoint = "/v1/ai/beta/remove-background"
    #: Límites del endpoint: 20 MB de entrada y 25 megapíxeles de salida.
    MAX_BYTES = 20 * 1024 * 1024
    MAX_PIXELS = 25_000_000

    def __init__(self, api_key: str | None = None) -> None:
        self.client = MagnificClient(api_key=api_key)
        self.api_key = self.client.api_key
        self.timeout = self.client.timeout

    def available(self) -> bool:
        return self.client.available()

    def cutout(self, image_path: str, output_path: str | None = None) -> str:
        """Devuelve la ruta de un PNG RGBA con el sujeto y el resto transparente."""
        if not self.available():
            raise ProviderUnavailableError("MAGNIFIC_API_KEY no está configurada.")

        source = Path(image_path)
        with Image.open(source) as opened:
            size = opened.size
        payload, upload_name = self._prepared(source, size)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                # El nombre decide el content-type de la subida: tiene que
                # coincidir con los bytes que se envían.
                asset_url = self.client.upload(client, Path(upload_name), payload)
                response = client.post(
                    f"{self.client.base_url}{self.endpoint}",
                    headers={"x-magnific-api-key": self.api_key or ""},
                    data={"image_url": asset_url},
                )
                response.raise_for_status()
                body = response.json() or {}
                result_url = body.get("high_resolution") or body.get("url")
                if not result_url:
                    raise ProviderUnavailableError(
                        "Magnific no devolvió el recorte (respuesta sin URL)."
                    )
                image_bytes = self.client.download(client, result_url)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise ProviderUnavailableError(
                f"Magnific remove-background devolvió {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderUnavailableError(
                f"Magnific remove-background no respondió: {exc}"
            ) from exc

        try:
            cut = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                "El recorte devuelto por Magnific no es una imagen válida."
            ) from exc

        # El servicio puede reescalar: se devuelve al tamaño del arte para que la
        # máscara encaje pixel a pixel con el lienzo del proyecto.
        if cut.size != size:
            cut = cut.resize(size, Image.Resampling.LANCZOS)

        target = Path(output_path) if output_path else source.with_name("cutout.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        cut.save(target, format="PNG", optimize=True)
        cut.close()
        return str(target)

    def _prepared(self, source: Path, size: tuple[int, int]) -> tuple[bytes, str]:
        """(bytes, nombre) dentro de los límites del endpoint: 20 MB y 25 MP."""
        raw = source.read_bytes()
        width, height = size
        if len(raw) <= self.MAX_BYTES and width * height <= self.MAX_PIXELS:
            return raw, source.name

        with Image.open(source) as opened:
            image = opened.convert("RGB")
        scale = min(1.0, (self.MAX_PIXELS / max(1, width * height)) ** 0.5)
        if scale < 1.0:
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        if buffer.tell() <= self.MAX_BYTES:
            return buffer.getvalue(), "recorte-entrada.png"
        # Aún pesa demasiado en PNG: JPEG, y el nombre lo declara.
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92, optimize=True)
        return buffer.getvalue(), "recorte-entrada.jpg"


#: Aislar el producto de una foto de ambiente.
#:
#: `remove-background` no sirve aquí: en una habitación considera "fondo" solo lo
#: que hay detrás de la cámara, así que devuelve la escena entera —pared, piso y
#: muebles— y el producto sigue sin separarse. Un modelo de edición sí entiende
#: qué es mueble y qué es cuarto, y al dejarlo sobre un plano liso el recortador
#: ya puede hacer su trabajo.
ISOLATE_INSTRUCTION = (
    "Keep only the products shown in this advertising photograph — the furniture "
    "or merchandise being sold — with their exact shape, colour, materials, "
    "scale, position and perspective. Delete the surrounding scene: replace every "
    "wall, floor, ceiling, window, houseplant, shelf and piece of wall art with a "
    "flat, uniform pure white background. Keep no shadows on that background. "
    "Do not add any object, text, logo or watermark."
)

#: Vaciar la escena para que quede de plancha de fondo.
EMPTY_INSTRUCTION = (
    "Remove every product from this advertising photograph — all furniture and "
    "merchandise — and leave the empty setting behind. Keep the same walls, "
    "floor, ceiling, lighting, colour palette, camera angle and perspective, and "
    "reconstruct whatever the products were covering. Do not add any object, "
    "text, logo or watermark."
)


class MagnificSceneProvider:
    """Ediciones por instrucción sobre la foto completa, sin máscara.

    Se usa cuando el producto está aplanado dentro de una foto de ambiente y hay
    que separarlo del decorado. Son dos operaciones complementarias: `isolate`
    deja el producto sobre fondo plano y `empty` deja el decorado sin producto.

    El modelo **regenera** la imagen: el resultado es fiel al original en estilo,
    color y encuadre, pero no idéntico píxel a píxel. Para una plancha de fondo
    publicitaria es suficiente; quien necesite exactitud tiene la máscara a mano.
    """

    name = "magnific-scene"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.client = MagnificClient(api_key=api_key)
        self.model_id = (model or settings.magnific_scene_model).strip()

    def available(self) -> bool:
        return self.client.available() and self.model_id in MODELS

    @property
    def model(self) -> MagnificModel:
        return resolve_model(self.model_id)

    def isolate(self, image_path: str, output_path: str | None = None, prompt: str | None = None) -> str:
        """El producto sobre fondo blanco liso, listo para recortar."""
        return self._edit(image_path, prompt or ISOLATE_INSTRUCTION, output_path, "aislado")

    def empty(self, image_path: str, output_path: str | None = None, prompt: str | None = None) -> str:
        """El decorado sin el producto, listo para usarse de plancha."""
        return self._edit(image_path, prompt or EMPTY_INSTRUCTION, output_path, "vacio")

    # ------------------------------------------------------------------ interno
    def _edit(self, image_path: str, prompt: str, output_path: str | None, suffix: str) -> str:
        if not self.client.available():
            raise ProviderUnavailableError("MAGNIFIC_API_KEY no está configurada.")
        model = self.model
        if model.supports_mask:
            raise ProviderUnavailableError(
                f"{model.label} solo trabaja con máscara y aquí todavía no la hay. "
                "Elija otro modelo para las fotos de ambiente (MAGNIFIC_SCENE_MODEL)."
            )

        source = Path(image_path)
        with Image.open(source) as opened:
            size = opened.size

        try:
            with httpx.Client(timeout=self.client.timeout) as client:
                reference = self.client.upload(client, source)
                task = self.client.submit(
                    client, model.endpoint, self._payload(model, reference, prompt, size)
                )
                url = self.client.wait(client, model.endpoint, task)
                raw = self.client.download(client, url)
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailableError(
                f"Magnific {model.id} devolvió {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderUnavailableError(f"Magnific {model.id} no respondió: {exc}") from exc

        try:
            edited = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                f"Magnific {model.id} no devolvió una imagen válida."
            ) from exc
        # El modelo elige su propio tamaño: se devuelve al del arte para que todo
        # lo que venga después encaje con el lienzo del proyecto.
        if edited.size != size:
            edited = edited.resize(size, Image.Resampling.LANCZOS)

        target = Path(output_path) if output_path else source.with_name(f"{suffix}.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        edited.save(target, format="PNG", optimize=True)
        edited.close()
        return str(target)

    def _payload(
        self, model: MagnificModel, reference: str, prompt: str, size: tuple[int, int]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt}
        if model.aspect_ratios:
            payload["aspect_ratio"] = closest_ratio(*size, model.aspect_ratios)
        if model.resolutions:
            wanted = settings.magnific_resolution.strip().lower()
            match = next((item for item in model.resolutions if item.lower() == wanted), None)
            payload["resolution"] = match or model.resolutions[-1]
        if model.mode == "references":
            payload["reference_images"] = [reference]
        elif model.mode == "reference_objects":
            payload["reference_images"] = [{"image": reference, "mime_type": "image/png"}]
        elif model.mode == "image":
            payload["input_image"] = reference
            if model.extras.get("size_in_pixels"):
                payload["width"], payload["height"] = _clamp_pixels(*size)
        elif model.mode == "structure":
            payload["structure_reference"] = reference
            payload["structure_strength"] = settings.magnific_structure_strength
            payload["adherence"] = settings.magnific_adherence
            payload["model"] = settings.magnific_mystic_model
            payload["engine"] = settings.magnific_engine
        else:  # pragma: no cover - el catálogo no define otros modos
            raise ProviderUnavailableError(f"Modo no soportado: {model.mode}")
        return payload
