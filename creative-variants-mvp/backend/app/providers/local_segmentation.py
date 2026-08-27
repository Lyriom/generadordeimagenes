"""Proveedor de segmentación local basado en OpenCV (siempre disponible, sin GPU).

Capacidades:
- Detección de regiones candidatas por contraste contra el fondo dominante.
- Segmentación del sujeto principal.
- Máscaras a partir de rectángulos (GrabCut con fallback por color).
- Refinamiento morfológico.
"""
from __future__ import annotations

import cv2
import numpy as np

from .base import Detection, ProviderUnavailableError


def _read_bgra(image_path: str) -> tuple[np.ndarray, np.ndarray | None]:
    """Lee la imagen aplanando el alfa sobre BLANCO y devuelve (bgr, alfa|None).

    Aplanar sobre negro (lo que hace `IMREAD_COLOR`) rompe la estimación del color
    de fondo y produce máscaras inservibles en PNG con transparencia.
    """
    raw = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ProviderUnavailableError(f"No se pudo leer la imagen: {image_path}")
    if raw.ndim == 2:
        return cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR), None
    if raw.shape[2] == 4:
        alpha = raw[:, :, 3]
        if alpha.min() >= 250:
            return raw[:, :, :3].copy(), None
        weight = (alpha.astype(np.float32) / 255.0)[..., None]
        blended = raw[:, :, :3].astype(np.float32) * weight + 255.0 * (1 - weight)
        return blended.astype(np.uint8), alpha
    return raw[:, :, :3].copy(), None


def _read_bgr(image_path: str) -> np.ndarray:
    return _read_bgra(image_path)[0]


def refine_mask(mask: np.ndarray, close: int = 5, open_: int = 3) -> np.ndarray:
    """Limpieza morfológica: cierra huecos y elimina ruido puntual."""
    binary = (mask > 127).astype(np.uint8) * 255
    if close > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    if open_ > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_, open_))
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return binary


def keep_largest_component(mask: np.ndarray, min_ratio: float = 0.02) -> np.ndarray:
    """Conserva la componente conexa dominante (evita restos dispersos)."""
    binary = (mask > 127).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if count <= 1:
        return (binary * 255).astype(np.uint8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    order = np.argsort(areas)[::-1]
    total = float(binary.size)
    keep = np.zeros_like(binary)
    biggest = areas[order[0]]
    for idx in order:
        if areas[idx] < max(min_ratio * biggest, 0.0005 * total):
            break
        keep[labels == idx + 1] = 1
    return (keep * 255).astype(np.uint8)


def feather_mask(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    """Suaviza los bordes de la máscara para evitar dientes de sierra."""
    if radius <= 0:
        return mask
    size = radius * 2 + 1
    return cv2.GaussianBlur(mask, (size, size), 0)


def _background_color(image: np.ndarray, border: int = 12) -> np.ndarray:
    h, w = image.shape[:2]
    border = max(2, min(border, h // 8 or 2, w // 8 or 2))
    strips = [
        image[:border, :].reshape(-1, 3),
        image[-border:, :].reshape(-1, 3),
        image[:, :border].reshape(-1, 3),
        image[:, -border:].reshape(-1, 3),
    ]
    samples = np.concatenate(strips, axis=0).astype(np.float32)
    return np.median(samples, axis=0)


class LocalSegmentationProvider:
    """Segmentación heurística con OpenCV. Funciona sin GPU ni descargas."""

    name = "opencv-local"

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------------ detect
    def detect(self, image_path: str, max_regions: int = 12) -> list[Detection]:
        image, alpha = _read_bgra(image_path)
        h, w = image.shape[:2]
        # Un PNG con transparencia ya trae la máscara perfecta: se usa tal cual.
        fg = refine_mask(alpha) if alpha is not None else self._foreground_mask(image)
        detections: list[Detection] = []

        subject = keep_largest_component(fg, min_ratio=0.35)
        ys, xs = np.where(subject > 127)
        if len(xs) > 0:
            x0, x1 = int(xs.min()), int(xs.max())
            y0, y1 = int(ys.min()), int(ys.max())
            area_ratio = float((subject > 127).sum()) / float(h * w)
            if 0.01 <= area_ratio <= 0.92:
                detections.append(
                    Detection(
                        x=x0,
                        y=y0,
                        width=x1 - x0 + 1,
                        height=y1 - y0 + 1,
                        score=round(min(0.9, 0.45 + area_ratio), 3),
                        label="subject",
                        kind="subject",
                        mask=subject,
                        meta={"area_ratio": round(area_ratio, 4)},
                    )
                )

        components = self._component_boxes(fg, max_regions=max_regions)
        for det in components:
            if any(_iou(det.box, existing.box) > 0.7 for existing in detections):
                continue
            detections.append(det)
        return detections[:max_regions]

    # ----------------------------------------------------------------- segment
    def segment(
        self,
        image_path: str,
        box: tuple[int, int, int, int] | None = None,
        points: list[tuple[int, int, int]] | None = None,
        text_prompt: str | None = None,
    ) -> np.ndarray:
        image, alpha = _read_bgra(image_path)
        h, w = image.shape[:2]

        if box is None and not points:
            base = refine_mask(alpha) if alpha is not None else self._foreground_mask(image)
            return refine_mask(keep_largest_component(base, min_ratio=0.3))

        if box is not None:
            x, y, bw, bh = _clamp_box(box, w, h)
            if bw < 8 or bh < 8:
                mask = np.zeros((h, w), np.uint8)
                mask[y : y + max(bh, 1), x : x + max(bw, 1)] = 255
                return mask
            if alpha is not None:
                # Con alfa disponible, el recorte exacto es alfa ∩ rectángulo.
                inside = np.zeros((h, w), np.uint8)
                inside[y : y + bh, x : x + bw] = 255
                cropped = cv2.bitwise_and(refine_mask(alpha), inside)
                if int((cropped > 127).sum()) > 0:
                    return cropped
            mask = self._grabcut(image, (x, y, bw, bh), points)
            inside = np.zeros((h, w), np.uint8)
            inside[y : y + bh, x : x + bw] = 255
            mask = cv2.bitwise_and(mask, inside)
            coverage = float((mask > 127).sum()) / float(max(1, bw * bh))
            if coverage < 0.05 or coverage > 0.995:
                # GrabCut degeneró: se usa distancia al color de fondo del recorte.
                mask = self._color_distance_mask(image, (x, y, bw, bh))
            return refine_mask(mask)

        # Solo puntos: región alrededor de los puntos positivos.
        mask = np.zeros((h, w), np.uint8)
        for px, py, label in points or []:
            if label <= 0:
                continue
            cv2.circle(mask, (int(px), int(py)), max(6, min(h, w) // 40), 255, -1)
        return refine_mask(mask)

    # ---------------------------------------------------------------- internals
    def _foreground_mask(self, image: np.ndarray) -> np.ndarray:
        h, w = image.shape[:2]
        blurred = cv2.GaussianBlur(image, (5, 5), 0)
        bg = _background_color(blurred)
        diff = np.linalg.norm(blurred.astype(np.float32) - bg[None, None, :], axis=2)
        diff_norm = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        _, color_mask = cv2.threshold(diff_norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        gray = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 160)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        edges = cv2.dilate(edges, kernel, iterations=2)

        combined = cv2.bitwise_or(color_mask, edges)
        combined = refine_mask(combined, close=max(5, min(h, w) // 120 | 1), open_=3)
        if (combined > 127).mean() > 0.9:  # fondo detectado como sujeto
            combined = cv2.bitwise_not(combined)
        return combined

    def _component_boxes(self, mask: np.ndarray, max_regions: int = 12) -> list[Detection]:
        h, w = mask.shape[:2]
        total = float(h * w)
        binary = (mask > 127).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        results: list[Detection] = []
        for idx in range(1, count):
            x, y, bw, bh, area = stats[idx]
            ratio = area / total
            if ratio < 0.004 or ratio > 0.85:
                continue
            if bw < 16 or bh < 16:
                continue
            comp = np.zeros((h, w), np.uint8)
            comp[labels == idx] = 255
            results.append(
                Detection(
                    x=int(x),
                    y=int(y),
                    width=int(bw),
                    height=int(bh),
                    score=round(min(0.85, 0.35 + ratio * 2), 3),
                    label="region",
                    kind="region",
                    mask=comp,
                    meta={"area_ratio": round(float(ratio), 4)},
                )
            )
        results.sort(key=lambda det: det.area, reverse=True)
        return results[:max_regions]

    def _grabcut(
        self,
        image: np.ndarray,
        box: tuple[int, int, int, int],
        points: list[tuple[int, int, int]] | None = None,
    ) -> np.ndarray:
        h, w = image.shape[:2]
        x, y, bw, bh = box
        gc_mask = np.full((h, w), cv2.GC_BGD, np.uint8)
        gc_mask[y : y + bh, x : x + bw] = cv2.GC_PR_FGD
        pad_x = max(2, bw // 12)
        pad_y = max(2, bh // 12)
        gc_mask[
            y + pad_y : max(y + pad_y + 1, y + bh - pad_y),
            x + pad_x : max(x + pad_x + 1, x + bw - pad_x),
        ] = cv2.GC_PR_FGD

        radius = max(4, min(h, w) // 60)
        for px, py, label in points or []:
            value = cv2.GC_FGD if label > 0 else cv2.GC_BGD
            cv2.circle(gc_mask, (int(px), int(py)), radius, int(value), -1)

        bgd_model = np.zeros((1, 65), np.float64)
        fgd_model = np.zeros((1, 65), np.float64)
        work = image
        scale = 1.0
        if max(h, w) > 1400:  # GrabCut es costoso: se trabaja a escala reducida
            scale = 1400 / max(h, w)
            work = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            gc_mask = cv2.resize(
                gc_mask, (work.shape[1], work.shape[0]), interpolation=cv2.INTER_NEAREST
            )
        try:
            cv2.grabCut(work, gc_mask, None, bgd_model, fgd_model, 4, cv2.GC_INIT_WITH_MASK)
        except cv2.error:
            return self._color_distance_mask(image, box)
        result = np.where(
            (gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD), 255, 0
        ).astype(np.uint8)
        if scale != 1.0:
            result = cv2.resize(result, (w, h), interpolation=cv2.INTER_NEAREST)
        return result

    def _color_distance_mask(
        self, image: np.ndarray, box: tuple[int, int, int, int]
    ) -> np.ndarray:
        h, w = image.shape[:2]
        x, y, bw, bh = box
        crop = image[y : y + bh, x : x + bw]
        if crop.size == 0:
            return np.zeros((h, w), np.uint8)
        bg = _background_color(crop, border=max(2, min(bw, bh) // 10))
        diff = np.linalg.norm(crop.astype(np.float32) - bg[None, None, :], axis=2)
        diff_norm = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        _, local = cv2.threshold(diff_norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if (local > 127).mean() > 0.85:
            local = cv2.bitwise_not(local)
        mask = np.zeros((h, w), np.uint8)
        mask[y : y + bh, x : x + bw] = local
        return mask


def _clamp_box(box: tuple[int, int, int, int], w: int, h: int) -> tuple[int, int, int, int]:
    x, y, bw, bh = (int(round(v)) for v in box)
    x = max(0, min(x, max(0, w - 1)))
    y = max(0, min(y, max(0, h - 1)))
    bw = max(1, min(bw, w - x))
    bh = max(1, min(bh, h - y))
    return x, y, bw, bh


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0 = max(ax, bx)
    y0 = max(ay, by)
    x1 = min(ax + aw, bx + bw)
    y1 = min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0
