"""Proveedor OCR con PaddleOCR (opcional, carga diferida).

PaddleOCR es pesado y descarga modelos la primera vez que se instancia. Por eso:
- El import y la instanciación son diferidos (nunca al arrancar la app).
- Si no está instalado, `available()` es False y la API devuelve una advertencia
  clara para que el usuario cree las capas de texto manualmente.
"""
from __future__ import annotations

import logging
import re

import cv2
import numpy as np

from ..config import settings
from .base import OcrResult, TextRegion

logger = logging.getLogger(__name__)


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


class PaddleOcrProvider:
    name = "paddleocr"

    def __init__(self, lang: str | None = None) -> None:
        self.lang = lang or settings.ocr_lang
        self._engine = None
        self._error: str | None = None

    def available(self) -> bool:
        if not settings.enable_ocr:
            self._error = "OCR desactivado por configuración (ENABLE_OCR=false)."
            return False
        import importlib.util

        if importlib.util.find_spec("paddleocr") is None:
            self._error = (
                "PaddleOCR no está instalado. Instale requirements-ocr.txt o cree "
                "las capas de texto manualmente."
            )
            return False
        return True

    @property
    def error(self) -> str | None:
        return self._error

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        from paddleocr import PaddleOCR  # type: ignore

        try:  # PaddleOCR 3.x
            self._engine = PaddleOCR(lang=self.lang, use_textline_orientation=True)
        except TypeError:  # PaddleOCR 2.x
            self._engine = PaddleOCR(lang=self.lang, use_angle_cls=True, show_log=False)
        return self._engine

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
            return OcrResult(
                regions=[],
                provider=self.name,
                available=False,
                warnings=[f"No se pudo inicializar PaddleOCR: {exc}"],
            )

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            return OcrResult(
                regions=[], provider=self.name, available=False, warnings=["Imagen ilegible."]
            )

        try:
            raw = engine.ocr(str(image_path))
        except Exception as exc:  # noqa: BLE001
            return OcrResult(
                regions=[],
                provider=self.name,
                available=False,
                warnings=[f"PaddleOCR falló: {exc}"],
            )

        regions = self._parse(raw, image)
        return OcrResult(regions=regions, provider=self.name, available=True)

    # ------------------------------------------------------------------ parsing
    def _parse(self, raw, image: np.ndarray) -> list[TextRegion]:
        """Normaliza las distintas formas de salida de PaddleOCR 2.x y 3.x."""
        regions: list[TextRegion] = []
        entries: list[tuple[list, str, float]] = []

        if isinstance(raw, dict):  # PaddleOCR 3.x predict()
            raw = [raw]
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            for page in raw:
                polys = page.get("dt_polys") or page.get("rec_polys") or []
                texts = page.get("rec_texts") or []
                scores = page.get("rec_scores") or []
                for idx, text in enumerate(texts):
                    poly = polys[idx] if idx < len(polys) else []
                    score = float(scores[idx]) if idx < len(scores) else 0.0
                    entries.append((list(poly), str(text), score))
        else:
            pages = raw or []
            if pages and not isinstance(pages[0], list):
                pages = [pages]
            for page in pages:
                for item in page or []:
                    try:
                        poly, (text, score) = item[0], item[1]
                    except (IndexError, TypeError, ValueError):
                        continue
                    entries.append((list(poly), str(text), float(score)))

        for poly, text, score in entries:
            text = (text or "").strip()
            if not text:
                continue
            pts = np.array(poly, dtype=np.float32).reshape(-1, 2) if len(poly) else None
            if pts is None or len(pts) < 3:
                continue
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            box = (int(x0), int(y0), int(max(1, x1 - x0)), int(max(1, y1 - y0)))
            angle = float(
                np.degrees(np.arctan2(pts[1][1] - pts[0][1], max(1e-6, pts[1][0] - pts[0][0])))
            )
            regions.append(
                TextRegion(
                    text=re.sub(r"\s+", " ", text),
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
