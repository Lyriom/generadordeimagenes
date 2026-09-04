"""Proveedor SAM 2 / SAM.

No se descarga ningún modelo al iniciar la aplicación: la carga es diferida y
solo ocurre si `SEGMENTATION_PROVIDER=sam` (o `auto` con checkpoint presente) y
existe el archivo indicado en `SAM_CHECKPOINT`.

En la imagen de este proyecto viene instalado y con el checkpoint dentro
(`/models/sam2.1_hiera_small.pt`), así que `auto` lo elige solo. Para montarlo
por fuera (ver README):
    pip install -r requirements-sam.txt
    export SAM_CHECKPOINT=/models/sam2.1_hiera_small.pt
    export SAM_VARIANT=sam2          # o "sam" para segment-anything v1
    export SEGMENTATION_PROVIDER=sam
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from ..config import settings
from .base import Detection, ProviderUnavailableError

logger = logging.getLogger(__name__)

# Cada checkpoint de SAM 2.1 tiene su archivo de configuración; el nombre del
# archivo de pesos dice cuál. Sin este mapa habría que acertar a mano con
# SAM_MODEL_TYPE, y el valor por omisión ("vit_b") es de SAM 1: pasárselo a
# SAM 2 lo hace fallar al construir el modelo.
_SAM2_CONFIGS = {
    "tiny": "configs/sam2.1/sam2.1_hiera_t.yaml",
    "small": "configs/sam2.1/sam2.1_hiera_s.yaml",
    "base_plus": "configs/sam2.1/sam2.1_hiera_b+.yaml",
    "large": "configs/sam2.1/sam2.1_hiera_l.yaml",
}
_SAM2_CONFIG_POR_DEFECTO = _SAM2_CONFIGS["small"]


def _sam2_config(model_type: str | None, checkpoint: str | None) -> str:
    """Configuración de SAM 2: la indicada, o la que diga el nombre del checkpoint."""
    if model_type and model_type.endswith((".yaml", ".yml")):
        return model_type
    nombre = Path(checkpoint or "").stem.lower()
    for tamano, config in _SAM2_CONFIGS.items():
        if tamano in nombre:
            return config
    return _SAM2_CONFIG_POR_DEFECTO


class SamSegmentationProvider:
    name = "sam"

    def __init__(
        self,
        checkpoint: str | None = None,
        model_type: str | None = None,
        variant: str | None = None,
    ) -> None:
        self.checkpoint = checkpoint or settings.sam_checkpoint
        self.model_type = model_type or settings.sam_model_type
        self.variant = (variant or settings.sam_variant).lower()
        self._predictor = None
        self._load_error: str | None = None
        # Codificar la imagen es lo que cuesta (unos 2 s); sacar cada máscara son
        # milisegundos. Recortar varias capas del mismo arte es lo normal, así
        # que se guarda la última codificada.
        self._image_key: tuple[str, int, int] | None = None
        self._image_shape: tuple[int, int] | None = None

    # --------------------------------------------------------------- lifecycle
    def available(self) -> bool:
        """Comprueba requisitos sin cargar pesos (evita descargas silenciosas)."""
        if not self.checkpoint:
            self._load_error = "SAM_CHECKPOINT no está configurado."
            return False
        if not Path(self.checkpoint).exists():
            self._load_error = f"No existe el checkpoint {self.checkpoint}."
            return False
        try:
            if self.variant == "sam2":
                import importlib.util

                if importlib.util.find_spec("sam2") is None:
                    raise ImportError("paquete sam2 no instalado")
            else:
                import importlib.util

                if importlib.util.find_spec("segment_anything") is None:
                    raise ImportError("paquete segment_anything no instalado")
        except ImportError as exc:
            self._load_error = f"Librería SAM no disponible: {exc}"
            return False
        return True

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def _ensure_predictor(self):
        if self._predictor is not None:
            return self._predictor
        if not self.available():
            raise ProviderUnavailableError(self._load_error or "SAM no disponible.")
        try:
            if self.variant == "sam2":
                from sam2.build_sam import build_sam2  # type: ignore
                from sam2.sam2_image_predictor import SAM2ImagePredictor  # type: ignore

                config = _sam2_config(self.model_type, self.checkpoint)
                model = build_sam2(config, self.checkpoint, device="cpu")
                self._predictor = SAM2ImagePredictor(model)
            else:
                from segment_anything import SamPredictor, sam_model_registry  # type: ignore

                model = sam_model_registry[self.model_type](checkpoint=self.checkpoint)
                model.to("cpu")
                self._predictor = SamPredictor(model)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"No se pudo cargar SAM: {exc}"
            raise ProviderUnavailableError(self._load_error) from exc
        return self._predictor

    # ------------------------------------------------------------------ public
    def detect(self, image_path: str) -> list[Detection]:
        """SAM no clasifica: sin prompts no proponemos regiones."""
        return []

    def _encode(self, predictor, image_path: str) -> tuple[int, int]:
        """Codifica la imagen si no es la misma de la llamada anterior."""
        try:
            info = Path(image_path).stat()
            key = (str(image_path), info.st_mtime_ns, info.st_size)
        except OSError:
            key = None
        if key is not None and key == self._image_key and self._image_shape is not None:
            return self._image_shape

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise ProviderUnavailableError(f"No se pudo leer la imagen: {image_path}")
        predictor.set_image(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        self._image_key = key
        self._image_shape = (int(image.shape[0]), int(image.shape[1]))
        return self._image_shape

    def segment(
        self,
        image_path: str,
        box: tuple[int, int, int, int] | None = None,
        points: list[tuple[int, int, int]] | None = None,
        text_prompt: str | None = None,
    ) -> np.ndarray:
        predictor = self._ensure_predictor()
        alto, ancho = self._encode(predictor, image_path)

        box_arr = None
        if box is not None:
            x, y, bw, bh = box
            box_arr = np.array([x, y, x + bw, y + bh], dtype=np.float32)
        point_coords = None
        point_labels = None
        if points:
            point_coords = np.array([[p[0], p[1]] for p in points], dtype=np.float32)
            point_labels = np.array([p[2] for p in points], dtype=np.int32)

        masks, scores, _ = predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            box=box_arr,
            multimask_output=False,
        )
        best = int(np.argmax(scores)) if scores is not None and len(scores) else 0
        mask = (np.asarray(masks[best]) > 0).astype(np.uint8) * 255
        if mask.shape[:2] != (alto, ancho):  # pragma: no cover - defensivo
            mask = cv2.resize(mask, (ancho, alto), interpolation=cv2.INTER_NEAREST)
        return mask
