"""Proveedor OCR con RapidOCR (PP-OCR sobre onnxruntime), de carga diferida.

Son los mismos modelos que PaddleOCR pero ejecutados con onnxruntime, así que
no hace falta paddlepaddle: 125 MB en vez de 1,1 GB y menos de un segundo de
arranque en vez de medio minuto.

El reconocedor es el latino (`latin_PP-OCRv5_rec_mobile`), no el chino/inglés
que viene por omisión. Importa: sobre un arte real, el de por omisión leía
"Camara Ecuatoriana", "Electronico" y "VALIDO", y devolvía la línea legal con
las palabras pegadas. El latino devuelve "Cámara Ecuatoriana de", "Comercio
Electrónico" y "VÁLIDO", con sus espacios.

El import y la instanciación son diferidos: nunca al arrancar la aplicación.
Si el paquete no está, `available()` es False y la API avisa con claridad para
que las capas de texto se creen a mano.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import cv2
import numpy as np

from ..config import settings
from .base import OcrResult, TextRegion

logger = logging.getLogger(__name__)

# Códigos de idioma → reconocedor de RapidOCR. El latino cubre castellano,
# portugués, inglés, francés, italiano y alemán con sus tildes y diéresis.
_LATIN_LANGS = {
    "es", "spa", "pt", "por", "fr", "fra", "it", "ita", "de", "deu",
    "ca", "gl", "eu", "nl", "en", "eng", "latin",
}


def _dominant_text_color(image: np.ndarray, box: tuple[int, int, int, int]) -> str:
    """Color aproximado del texto: píxeles más alejados del fondo del recorte."""
    x, y, w, h = box
    crop = image[max(0, y) : y + h, max(0, x) : x + w]
    if crop.size == 0:
        return "#FFFFFF"
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    thresh = int(np.median(gray))
    dark = crop[gray <= thresh]
    light = crop[gray > thresh]
    # El texto suele ocupar menos área que su fondo inmediato.
    if len(dark) == 0 or len(light) == 0:
        pick = crop.reshape(-1, 3)
    else:
        pick = dark if len(dark) <= len(light) else light
    bgr = np.median(pick.reshape(-1, 3), axis=0).astype(int)
    return "#{:02X}{:02X}{:02X}".format(int(bgr[2]), int(bgr[1]), int(bgr[0]))


def _carpeta_modelos() -> Path:
    import rapidocr  # type: ignore

    # `__file__` es `str | None` para el verificador de tipos; en un paquete
    # normal nunca es None, pero el `or ""` evita el falso positivo.
    return Path(rapidocr.__file__ or "").parent / "models"


def _sha_esperados() -> dict[str, str]:
    """SHA256 que RapidOCR espera de cada modelo, leídos de su propio registro.

    Si el formato del registro cambiara, esto devuelve un diccionario vacío y la
    verificación se vuelve un no-op: preferible a reventar por un archivo de
    configuración de una dependencia.
    """
    import rapidocr  # type: ignore
    import yaml  # type: ignore

    registro = Path(rapidocr.__file__ or "").parent / "default_models.yaml"
    if not registro.exists():
        return {}
    try:
        datos = yaml.safe_load(registro.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}

    esperados: dict[str, str] = {}

    def recorrer(nodo) -> None:
        if isinstance(nodo, dict):
            url, sha = nodo.get("model_dir"), nodo.get("SHA256")
            if isinstance(url, str) and isinstance(sha, str) and url.endswith(".onnx"):
                esperados[url.rsplit("/", 1)[-1]] = sha
            for hijo in nodo.values():
                recorrer(hijo)
        elif isinstance(nodo, list):
            for hijo in nodo:
                recorrer(hijo)

    recorrer(datos)
    return esperados


def modelos_invalidos() -> list[str]:
    """Modelos descargados cuyo contenido no cuadra con el SHA que declaran.

    Existe porque pasó: la descarga se cortó a la mitad, el archivo quedó con
    3 MB de los 7,9 que son, y RapidOCR lo daba por bueno hasta que alguien
    analizaba un arte —y entonces se lo volvía a bajar, con la espera y la
    dependencia de tener internet que precargarlo pretendía evitar.
    """
    from rapidocr.utils.utils import get_file_sha256  # type: ignore

    esperados = _sha_esperados()
    if not esperados:
        return []
    malos = []
    for archivo in sorted(_carpeta_modelos().glob("*.onnx")):
        sha = esperados.get(archivo.name)
        if sha and get_file_sha256(archivo) != sha:
            malos.append(archivo.name)
    return malos


def precargar_modelos(intentos: int = 3) -> tuple[bool, str]:
    """Deja los modelos dentro de la imagen y comprobados. Nunca lanza.

    Se usa al construir la imagen. Que falle no debe cortar la construcción —un
    corte de red no justifica quedarse sin backend—, pero sí tiene que decirlo:
    sin modelos, el primer análisis espera la descarga, y en un servidor sin
    salida a internet no llega nunca.
    """
    proveedor = RapidOcrProvider()
    if not proveedor.available():
        return False, proveedor.error or "OCR no disponible."

    ultimo = ""
    for intento in range(1, max(1, intentos) + 1):
        try:
            proveedor._engine = None
            proveedor._ensure_engine()
        except Exception as exc:  # noqa: BLE001
            ultimo = f"no se pudo crear el motor: {exc}"
            continue
        malos = modelos_invalidos()
        if not malos:
            nombres = sorted(p.name for p in _carpeta_modelos().glob("*.onnx"))
            return True, "modelos listos: " + ", ".join(nombres)
        ultimo = f"descarga incompleta de {', '.join(malos)} (intento {intento})"
        for nombre in malos:
            (_carpeta_modelos() / nombre).unlink(missing_ok=True)
    return False, ultimo or "no se pudieron precargar los modelos."


class RapidOcrProvider:
    name = "rapidocr"

    def __init__(self, lang: str | None = None) -> None:
        self.lang = (lang or settings.ocr_lang or "es").lower()
        self._engine = None
        self._error: str | None = None

    # --------------------------------------------------------------- lifecycle
    def available(self) -> bool:
        if not settings.enable_ocr:
            self._error = "OCR desactivado por configuración (ENABLE_OCR=false)."
            return False
        import importlib.util

        if importlib.util.find_spec("rapidocr") is None:
            self._error = (
                "RapidOCR no está instalado. Instale requirements-ocr.txt o cree "
                "las capas de texto manualmente."
            )
            return False
        if importlib.util.find_spec("onnxruntime") is None:
            self._error = (
                "Falta onnxruntime, que es quien ejecuta los modelos de RapidOCR."
            )
            return False
        return True

    @property
    def error(self) -> str | None:
        return self._error

    def _rec_lang(self):
        """Reconocedor para el idioma configurado, con el latino por defecto."""
        from rapidocr import LangRec  # type: ignore

        conocidos = {item.value: item for item in LangRec}
        if self.lang in _LATIN_LANGS:
            return conocidos.get("latin", LangRec.LATIN)
        return conocidos.get(self.lang, conocidos.get("latin", LangRec.LATIN))

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR  # type: ignore

        self._engine = RapidOCR(
            params={
                "Rec.lang_type": self._rec_lang(),
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
            }
        )
        return self._engine

    # ------------------------------------------------------------------ public
    def read(self, image_path: str) -> OcrResult:
        if not self.available():
            return OcrResult(
                regions=[],
                provider=self.name,
                available=False,
                warnings=[self._error or "OCR no disponible."],
            )
        try:
            engine = self._ensure_engine()
        except Exception as exc:  # noqa: BLE001
            # Aquí cae también el primer arranque sin red: el reconocedor latino
            # se descarga la primera vez si no vino dentro de la imagen.
            return OcrResult(
                regions=[],
                provider=self.name,
                available=False,
                warnings=[f"No se pudo inicializar RapidOCR: {exc}"],
            )

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            return OcrResult(
                regions=[], provider=self.name, available=False, warnings=["Imagen ilegible."]
            )

        try:
            raw = engine(str(image_path))
        except Exception as exc:  # noqa: BLE001
            return OcrResult(
                regions=[],
                provider=self.name,
                available=False,
                warnings=[f"RapidOCR falló: {exc}"],
            )

        return OcrResult(regions=self._parse(raw, image), provider=self.name, available=True)

    # ------------------------------------------------------------------ parsing
    def _parse(self, raw, image: np.ndarray) -> list[TextRegion]:
        """Normaliza la salida de RapidOCR a regiones con caja, ángulo y color."""
        regions: list[TextRegion] = []
        if raw is None:
            return regions

        boxes = getattr(raw, "boxes", None)
        texts = getattr(raw, "txts", None)
        scores = getattr(raw, "scores", None)
        if boxes is None or texts is None:
            return regions

        for index, text in enumerate(texts):
            text = re.sub(r"\s+", " ", str(text or "")).strip()
            if not text:
                continue
            if index >= len(boxes):
                continue
            pts = np.array(boxes[index], dtype=np.float32).reshape(-1, 2)
            if len(pts) < 3:
                continue
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            box = (int(x0), int(y0), int(max(1, x1 - x0)), int(max(1, y1 - y0)))
            # Ángulo del lado superior: RapidOCR devuelve el polígono en orden.
            angle = float(
                np.degrees(np.arctan2(pts[1][1] - pts[0][1], max(1e-6, pts[1][0] - pts[0][0])))
            )
            score = float(scores[index]) if scores is not None and index < len(scores) else 0.0
            regions.append(
                TextRegion(
                    text=text,
                    x=box[0],
                    y=box[1],
                    width=box[2],
                    height=box[3],
                    confidence=round(min(1.0, max(0.0, score)), 3),
                    angle=round(angle, 2),
                    color=_dominant_text_color(image, box),
                )
            )
        regions.sort(key=lambda region: (region.y, region.x))
        return regions
