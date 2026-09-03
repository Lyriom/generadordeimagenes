"""Utilidades compartidas de imagen (Pillow + OpenCV + NumPy)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..config import settings

#: Guarda contra "bombas de descompresión". Sale de la configuración para que
#: sea el mismo número que rechaza la subida: si fueran dos, el archivo pasaría
#: la puerta y reventaría al abrirlo, que es peor que no dejarlo entrar.
Image.MAX_IMAGE_PIXELS = settings.max_image_pixels

#: Color con el que se aplanan los PNG con transparencia. NUNCA usar negro:
#: `Image.convert("RGB")` rellena el alfa con (0,0,0) y ensucia todo el pipeline
#: (fondos negros, paletas negras, segmentación inservible).
FLATTEN_BACKGROUND = (255, 255, 255)


# ----------------------------------------------------------------- carga segura
def load_rgba(path: str | Path) -> Image.Image:
    """Abre cualquier imagen como RGBA (el alfa queda disponible como máscara)."""
    with Image.open(path) as image:
        return image.convert("RGBA").copy()


def load_alpha(path: str | Path) -> np.ndarray | None:
    """Canal alfa de la imagen, o None si es completamente opaca."""
    with Image.open(path) as image:
        if image.mode not in {"RGBA", "LA", "PA"} and "transparency" not in image.info:
            return None
        alpha = image.convert("RGBA").split()[-1]
        array = np.asarray(alpha, dtype=np.uint8)
    return None if array.min() >= 250 else array


def load_flat_rgb(
    path: str | Path, background: tuple[int, int, int] = FLATTEN_BACKGROUND
) -> Image.Image:
    """Abre la imagen aplanando la transparencia sobre `background` (blanco)."""
    with Image.open(path) as image:
        if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, background)
            canvas.paste(rgba, (0, 0), rgba)
            return canvas
        return image.convert("RGB").copy()


def read_bgr_flat(path: str | Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Lee con OpenCV aplanando el alfa sobre blanco. Devuelve (bgr, alfa|None)."""
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise FileNotFoundError(f"No se pudo leer la imagen: {path}")
    if raw.ndim == 2:
        return cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR), None
    if raw.shape[2] == 4:
        alpha = raw[:, :, 3]
        if alpha.min() >= 250:
            return raw[:, :, :3].copy(), None
        weight = (alpha.astype(np.float32) / 255.0)[..., None]
        background = np.array(FLATTEN_BACKGROUND[::-1], dtype=np.float32)  # BGR
        blended = raw[:, :, :3].astype(np.float32) * weight + background * (1 - weight)
        return blended.astype(np.uint8), alpha
    return raw[:, :, :3].copy(), None


# --------------------------------------------------------------------- colores
def hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = (value or "#000000").lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        raw = "000000"
    return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[int, int, int] | np.ndarray) -> str:
    r, g, b = (int(max(0, min(255, round(float(c))))) for c in tuple(rgb)[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """Luminancia relativa WCAG."""
    channels = []
    for value in rgb[:3]:
        c = value / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def best_text_color(bg_rgb: tuple[int, int, int], preferred: str | None = None) -> str:
    """Elige el color de texto con mejor contraste (respeta `preferred` si cumple)."""
    candidates = ["#FFFFFF", "#111111"]
    if preferred:
        pref_rgb = hex_to_rgb(preferred)
        if contrast_ratio(pref_rgb, bg_rgb) >= 4.5:
            return preferred
    best = max(candidates, key=lambda c: contrast_ratio(hex_to_rgb(c), bg_rgb))
    return best


def dominant_colors(
    image: Image.Image, count: int = 3, alpha: np.ndarray | None = None
) -> list[str]:
    """Colores dominantes con k-means de OpenCV (determinista con criterios fijos).

    Si se pasa `alpha`, los píxeles transparentes se ignoran: en un recorte de
    producto el fondo aplanado no forma parte de la paleta de marca.
    """
    small = image.convert("RGB").resize((80, 80), Image.Resampling.BILINEAR)
    data = np.asarray(small, dtype=np.float32).reshape(-1, 3)
    if alpha is not None:
        mask = np.asarray(
            Image.fromarray(alpha).resize((80, 80), Image.Resampling.NEAREST)
        ).reshape(-1)
        opaque = data[mask > 128]
        if len(opaque) >= 64:  # solo si queda muestra suficiente
            data = opaque
    count = max(1, min(count, 8))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    try:
        # `bestLabels=None` es la forma documentada de pedirle a OpenCV que las
        # cree; sus anotaciones lo declaran obligatorio.
        _, labels, centers = cv2.kmeans(
            data, count, None, criteria, 3, cv2.KMEANS_PP_CENTERS  # type: ignore[arg-type]
        )
    except cv2.error:  # pragma: no cover - defensivo
        return [rgb_to_hex(tuple(np.mean(data, axis=0)))]
    counts = np.bincount(labels.flatten(), minlength=count)
    order = np.argsort(counts)[::-1]
    return [rgb_to_hex(tuple(centers[idx])) for idx in order]


def saturation(rgb: tuple[int, int, int]) -> float:
    """Saturación HSV aproximada (0..1)."""
    channels = [max(0, min(255, int(c))) for c in rgb[:3]]
    high, low = max(channels), min(channels)
    return 0.0 if high == 0 else (high - low) / high


def style_palette(
    image: Image.Image, alpha: np.ndarray | None = None
) -> tuple[str, str]:
    """(color de marca, color de apoyo) para fondos sólidos, degradados y duotonos.

    Prefiere un color saturado como protagonista: si solo se toman los dominantes
    por frecuencia, los fondos beige/gris producen variantes deslavadas.
    """
    palette = dominant_colors(image, 5, alpha=alpha)
    if not palette:
        return "#1A1A1A", "#4A4A4A"
    by_saturation = sorted(palette, key=lambda value: saturation(hex_to_rgb(value)), reverse=True)
    primary = (
        by_saturation[0]
        if saturation(hex_to_rgb(by_saturation[0])) >= 0.22
        else palette[0]
    )
    rest = [value for value in palette if value != primary] or palette
    secondary = min(rest, key=lambda value: relative_luminance(hex_to_rgb(value)))
    return primary, secondary


def region_average_color(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    x, y, w, h = box
    x0 = max(0, min(int(x), image.width - 1))
    y0 = max(0, min(int(y), image.height - 1))
    x1 = max(x0 + 1, min(int(x + w), image.width))
    y1 = max(y0 + 1, min(int(y + h), image.height))
    crop = image.convert("RGB").crop((x0, y0, x1, y1))
    arr = np.asarray(crop, dtype=np.float32).reshape(-1, 3)
    if arr.size == 0:
        return (0, 0, 0)
    return tuple(int(round(v)) for v in arr.mean(axis=0))  # type: ignore[return-value]


def region_std(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Desviación estándar de luminancia: mide 'ruido' del fondo bajo un texto."""
    x, y, w, h = box
    x0 = max(0, min(int(x), image.width - 1))
    y0 = max(0, min(int(y), image.height - 1))
    x1 = max(x0 + 1, min(int(x + w), image.width))
    y1 = max(y0 + 1, min(int(y + h), image.height))
    crop = image.convert("L").crop((x0, y0, x1, y1))
    arr = np.asarray(crop, dtype=np.float32)
    return float(arr.std()) if arr.size else 0.0


# --------------------------------------------------------------------- máscaras
def read_mask(path: str | Path) -> np.ndarray:
    mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise FileNotFoundError(f"Máscara no encontrada: {path}")
    return mask


def save_mask(path: str | Path, mask: np.ndarray) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(target), mask)
    return str(target)


def mask_bbox(mask: np.ndarray, threshold: int = 8) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask > threshold)
    if len(xs) == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def box_mask(shape: tuple[int, int], box: tuple[int, int, int, int]) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), np.uint8)
    x, y, bw, bh = (int(round(v)) for v in box)
    x0 = max(0, min(x, w - 1))
    y0 = max(0, min(y, h - 1))
    x1 = max(x0 + 1, min(x + bw, w))
    y1 = max(y0 + 1, min(y + bh, h))
    mask[y0:y1, x0:x1] = 255
    return mask


def dilate_mask(mask: np.ndarray, amount: int) -> np.ndarray:
    if amount <= 0:
        return mask
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (amount * 2 + 1, amount * 2 + 1))
    return cv2.dilate(mask, kernel, iterations=1)


# ---------------------------------------------------------------- composición
def fit_contain(
    src_w: int, src_h: int, box_w: int, box_h: int, allow_upscale: bool = True
) -> tuple[int, int]:
    """Escala manteniendo relación de aspecto para caber dentro de la caja."""
    if src_w <= 0 or src_h <= 0:
        return max(1, box_w), max(1, box_h)
    scale = min(box_w / src_w, box_h / src_h)
    if not allow_upscale:
        scale = min(scale, 1.0)
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def resize_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Escala y recorta al centro para cubrir el lienzo sin deformar."""
    if image.width == 0 or image.height == 0:
        return Image.new("RGB", (width, height), (20, 20, 20))
    scale = max(width / image.width, height / image.height)
    new_size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    resized = image.resize(new_size, Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def resize_contain_canvas(
    image: Image.Image,
    width: int,
    height: int,
    background: tuple[int, int, int] | None = None,
) -> Image.Image:
    """Encaja el arte completo en el lienzo sin recortarlo ni deformarlo."""
    source = image.convert("RGB")
    scale = min(width / max(1, source.width), height / max(1, source.height))
    new_w = max(1, int(round(source.width * scale)))
    new_h = max(1, int(round(source.height * scale)))
    resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if background is None:
        # El color de las esquinas integra mejor el espacio sobrante que una
        # franja blanca cuando la proporción de salida cambia ligeramente.
        corners = np.asarray(
            [
                source.getpixel((0, 0)),
                source.getpixel((source.width - 1, 0)),
                source.getpixel((0, source.height - 1)),
                source.getpixel((source.width - 1, source.height - 1)),
            ],
            dtype=np.uint8,
        )
        red, green, blue = (int(value) for value in np.median(corners, axis=0))
        background = (red, green, blue)
    canvas = Image.new("RGB", (width, height), background)
    canvas.paste(resized, ((width - new_w) // 2, (height - new_h) // 2))
    return canvas


def make_gradient(
    width: int, height: int, start: str, end: str, direction: str = "vertical"
) -> Image.Image:
    """Degradado lineal simple (vertical, horizontal o diagonal)."""
    c0 = np.array(hex_to_rgb(start), dtype=np.float32)
    c1 = np.array(hex_to_rgb(end), dtype=np.float32)
    if direction == "horizontal":
        ramp = np.linspace(0, 1, width, dtype=np.float32)[None, :, None]
        ramp = np.repeat(ramp, height, axis=0)
    elif direction == "diagonal":
        gx = np.linspace(0, 1, width, dtype=np.float32)[None, :]
        gy = np.linspace(0, 1, height, dtype=np.float32)[:, None]
        ramp = ((gx + gy) / 2.0)[..., None]
    else:
        ramp = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
        ramp = np.repeat(ramp, width, axis=1)
    grad = c0[None, None, :] * (1 - ramp) + c1[None, None, :] * ramp
    return Image.fromarray(grad.astype(np.uint8), mode="RGB")


def blur(image: Image.Image, radius: float) -> Image.Image:
    return image.filter(ImageFilter.GaussianBlur(radius=radius))


def rounded_rect(
    size: tuple[int, int], radius: int, fill: tuple[int, int, int, int]
) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([(0, 0), (size[0] - 1, size[1] - 1)], radius=radius, fill=fill)
    return layer


def pil_to_bgr(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)


def bgr_to_pil(image: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


#: Detalle medio alrededor del hueco por debajo del cual el fondo es liso.
#:
#: Medido con KV reales: un ciclorama en degradado da 0,85; la fotografía de una
#: habitación, con muebles, cuadros y textura de piso, está un orden por encima.
PLAIN_BACKDROP_DETAIL = 2.5

#: Proporción de bordes duros por debajo de la cual la plancha no tiene nada
#: dibujado y se puede rehacer entera.
#:
#: Separa con holgura los dos casos reales: un ciclorama de estudio da 0,000 y
#: una plancha con panel de titular, cuadro y rodapié da 0,028.
PLAIN_BACKDROP_EDGES = 0.005


def surrounding_detail(image: np.ndarray, mask: np.ndarray) -> float:
    """Cuánta textura hay alrededor del hueco. Bajo = ciclorama o pared lisa.

    Sirve para decidir quién rellena. En un fondo liso no hay nada que inventar y
    pedírselo a un modelo generativo termina mal: en un KV real devolvió un rollo
    de cartón con tipografía falsa donde solo hacía falta continuar el degradado.
    """
    binaria = ((mask > 24) * 255).astype(np.uint8)
    anillo = cv2.dilate(
        binaria, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (121, 121))
    ) - cv2.dilate(binaria, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21)))
    if not (anillo > 0).any():
        return float("inf")
    gris = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    bordes = cv2.Laplacian(cv2.GaussianBlur(gris, (5, 5), 0), cv2.CV_32F)
    return float(np.abs(bordes)[anillo > 0].mean())


def flat_backdrop_fill(
    image: np.ndarray, mask: np.ndarray, grid: int = 28, iterations: int = 6000
) -> np.ndarray:
    """Rehace el barrido de estudio completo, en vez de parchear el hueco.

    Un ciclorama es una superficie continua: degradado de la pared al piso más la
    caída de luz. Se modela a partir de los píxeles limpios —descartando el
    producto y el margen donde cae su sombra— y se aplica a **toda** la foto. Al
    no haber parche no hay costura, y la sombra proyectada, que no cabe en un
    modelo suave, desaparece sola.

    Rellenar solo el hueco fue el primer intento y no vale: cualquier método
    —`cv2.inpaint`, promediado por pirámide, membrana armónica, ajuste
    polinómico— interpola desde un borde que incluye la sombra, así que la
    hereda y deja un bulto con la silueta del producto.

    Se le devuelve el grano de la foto original: un degradado perfectamente liso
    canta al lado de una fotografía.
    """
    alto, ancho = image.shape[:2]
    sucio = mask > 24
    if not sucio.any():
        return image.copy()

    valido = (~sucio).astype(np.float32)
    acumulado = cv2.resize(
        image.astype(np.float32) * valido[..., None], (grid, grid),
        interpolation=cv2.INTER_AREA,
    )
    cobertura = cv2.resize(valido, (grid, grid), interpolation=cv2.INTER_AREA)
    libre = cobertura > 0.6
    if not libre.any():
        return image.copy()

    rejilla = np.zeros_like(acumulado)
    rejilla[libre] = acumulado[libre] / cobertura[libre][..., None]
    rejilla[~libre] = rejilla[libre].mean(axis=0)
    for _ in range(iterations):
        vecinos = (
            np.roll(rejilla, 1, 0) + np.roll(rejilla, -1, 0)
            + np.roll(rejilla, 1, 1) + np.roll(rejilla, -1, 1)
        ) / 4.0
        rejilla[~libre] = vecinos[~libre]

    fondo = cv2.resize(rejilla, (ancho, alto), interpolation=cv2.INTER_CUBIC)
    gris = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY).astype(np.float32)
    sigma = float((gris - cv2.GaussianBlur(gris, (0, 0), 2))[~sucio].std())
    grano = np.random.default_rng(7).normal(0, max(sigma, 0.0), (alto, ancho))
    return np.clip(fondo + grano.astype(np.float32)[..., None], 0, 255).astype(np.uint8)


def overall_detail(image: np.ndarray, mask: np.ndarray) -> float:
    """Textura de la plancha entera, sin contar el producto.

    Decide si se puede rehacer el fondo completo o solo el hueco: una plancha con
    una franja de color o un panel de titular no es un barrido de estudio, y
    modelarla entera se la llevaría por delante.
    """
    fuera = mask <= 24
    if not fuera.any():
        return float("inf")
    gris = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    bordes = cv2.Laplacian(cv2.GaussianBlur(gris, (5, 5), 0), cv2.CV_32F)
    return float(np.abs(bordes)[fuera].mean())


def hard_edge_ratio(image: np.ndarray, mask: np.ndarray, threshold: float = 8.0) -> float:
    """Qué parte de la plancha tiene bordes marcados, sin contar el producto.

    Un barrido de estudio no tiene ninguno; un panel de titular, un marco o un
    rodapié sí. Es lo que distingue "aquí no hay nada dibujado, se puede rehacer
    entero" de "aquí hay diseño que no se puede tocar".
    """
    fuera = mask <= 24
    if not fuera.any():
        return float("inf")
    gris = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    bordes = np.abs(cv2.Laplacian(cv2.GaussianBlur(gris, (5, 5), 0), cv2.CV_32F))
    return float((bordes[fuera] > threshold).mean())
