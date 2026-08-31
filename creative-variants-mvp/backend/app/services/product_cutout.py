"""Rescate del producto cuando el PSD no lo trae como capa.

Muchos KV llegan con el producto **aplanado dentro de una fotografía**: la sala
completa, el mueble en ambiente, el zapato sobre una mesa. El importador ve una
capa que cubre todo el lienzo y es opaca, así que la toma como fondo — que es lo
correcto para un fondo, pero deja la pieza sin nada que reemplazar.

Aquí se recupera: se recorta el sujeto de esa foto, se convierte en una capa
Producto reemplazable y se rellena el hueco que deja para que el fondo quede
limpio. A partir de ahí la pieza funciona como cualquier otra plantilla.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

from ..models import (
    BackgroundInfo,
    CATEGORY_LABELS_ES,
    Layer,
    LayerCategory,
    LayerType,
    Project,
    utcnow,
)
from ..providers import ProviderUnavailableError, get_inpainting_provider
from ..providers.magnific import MagnificCutoutProvider, MagnificSceneProvider
from . import layer_extraction, storage
from .imaging import dilate_mask, mask_bbox, save_mask

logger = logging.getLogger(__name__)

#: Por debajo de esto el recorte no es un producto, es ruido.
MIN_AREA_RATIO = 0.015
#: Por encima de esto el recorte trajo la escena entera, no el producto.
#:
#: Medido con KV reales: una mesa de centro sobre fondo liso ocupa ~12 %; una foto
#: de ambiente devuelve ~75 %, porque `remove-background` aísla "todo lo que no es
#: pared" y entrega la sala completa —piso y alfombra incluidos—. No es un fallo
#: del recortador: en una habitación no hay fondo que quitar. Cuando pasa de este
#: límite se cambia de estrategia y entra `_scene_pass`.
MAX_AREA_RATIO = 0.55
#: A partir de aquí el relleno deja de ser fiable y conviene avisarlo.
RISKY_FILL_RATIO = 0.35

class NoProductFoundError(RuntimeError):
    """El recorte no encontró un sujeto utilizable en la imagen."""


def has_product(project: Project) -> bool:
    return any(
        layer.category == LayerCategory.PRODUCT and layer.visible
        for layer in project.layers
    )


BACKGROUND_REL = "backgrounds/background.png"


def _plate_path(project: Project) -> Path:
    """La foto de donde sale el producto.

    Se prefiere la plancha de fondo del PSD: ya está limpia de logo, precio y
    legales, así que el recortador no se distrae con ellos. Si no hay plancha se
    lee el arte original, que **nunca** se modifica.
    """
    if project.background.path:
        plate = storage.abs_path(project.project_id, project.background.path)
        if plate.exists():
            return plate
    source = storage.abs_path(project.project_id, project.source.path)
    if not source.exists():
        raise FileNotFoundError("No se encuentra la imagen del proyecto.")
    return source


def detect_product(
    project: Project,
    *,
    inpaint_provider: str | None = None,
    inpaint_model: str | None = None,
    prompt: str | None = None,
    dilate: int = 4,
    scene_model: str | None = None,
) -> tuple[Layer, list[str]]:
    """Recorta el producto de la foto y limpia el fondo. Devuelve (capa, avisos).

    Hay dos caminos. En una foto de producto —el mueble sobre un ciclorama— basta
    con recortar el sujeto y rellenar el hueco. En una foto de ambiente el
    recortador devuelve la habitación entera, así que se pasa a `_scene_pass`:
    un modelo de edición separa el producto del decorado.

    Lanza `NoProductFoundError` si no sale nada aprovechable, para no ensuciar el
    proyecto con una capa inútil.
    """
    warnings: list[str] = []
    cutter = MagnificCutoutProvider()
    if not cutter.available():
        raise ProviderUnavailableError(
            "MAGNIFIC_API_KEY no está configurada: no se puede detectar el producto."
        )

    plate = _plate_path(project)
    canvas_h, canvas_w = layer_extraction.canvas_shape(project)
    size = (canvas_w, canvas_h)
    workdir = storage.abs_path(project.project_id, "tmp")
    workdir.mkdir(parents=True, exist_ok=True)

    alpha, rgba = _cut(cutter, plate, size, workdir)
    coverage = float((alpha > 24).mean())
    emptied: Path | None = None

    if coverage > MAX_AREA_RATIO:
        # La foto es un ambiente: el recorte trajo el cuarto. Se separa producto
        # y decorado con un modelo de edición, y el decorado ya sirve de plancha.
        rgba.close()
        alpha, rgba, emptied, avisos = _scene_pass(
            cutter, plate, size, workdir, scene_model, alpha
        )
        warnings.extend(avisos)
        coverage = float((alpha > 24).mean())

    if coverage < MIN_AREA_RATIO:
        rgba.close()
        raise NoProductFoundError(
            "El recorte no encontró un producto en la imagen "
            f"(solo {coverage:.1%} del arte). Márquelo a mano en Ajustes finos."
        )
    if coverage > MAX_AREA_RATIO:
        rgba.close()
        raise NoProductFoundError(
            f"Lo recortado ocupa el {coverage:.0%} del arte: sigue siendo la escena "
            "completa, no un producto. Márquelo a mano en Ajustes finos."
        )

    box = mask_bbox(alpha, threshold=24)
    if box is None:
        rgba.close()
        raise NoProductFoundError("El recorte quedó vacío.")
    x, y, width, height = box

    layer = Layer(
        name=CATEGORY_LABELS_ES[LayerCategory.PRODUCT],
        type=LayerType.IMAGE,
        category=LayerCategory.PRODUCT,
        x=x,
        y=y,
        width=width,
        height=height,
        z_index=max((item.z_index for item in project.layers), default=0) + 1,
        locked=True,
        # Un mueble en ambiente está apoyado en el suelo: si el motor lo recoloca
        # en cada variante, queda flotando. En foto de producto sí puede moverse.
        movable=emptied is None,
        replaceable=True,
        preserve_aspect_ratio=True,
        confidence=0.7,
        source="auto",
        extracted=True,
    )
    layer.meta.update(
        {
            "detected_by": cutter.name if emptied is None else "magnific-scene",
            "coverage": round(coverage, 4),
            "from_background_plate": project.background.path or None,
        }
    )
    layer.warnings.append(
        "Producto recortado automáticamente de la fotografía. Revise el borde en "
        "Ajustes finos antes de producir."
    )

    # PNG de la capa: el recorte real, sin reescalar.
    layer.src = layer_extraction.layer_rel_path(layer)
    crop = rgba.crop((x, y, x + width, y + height))
    target = storage.abs_path(project.project_id, layer.src)
    target.parent.mkdir(parents=True, exist_ok=True)
    crop.save(target, format="PNG", optimize=True)
    crop.close()
    rgba.close()

    layer.mask = layer_extraction.mask_rel_path(layer)
    save_mask(storage.abs_path(project.project_id, layer.mask), alpha)

    if emptied is not None:
        # El decorado sin producto ya está generado: no hace falta rellenar nada.
        warnings.extend(_adopt_plate(project, emptied, "magnific-scene"))
    else:
        warnings.extend(
            _clean_plate(
                project, plate, alpha, dilate, inpaint_provider, inpaint_model, prompt
            )
        )

    project.layers.append(layer)
    logger.info(
        "producto detectado en %s: %sx%s (%.1f%% del arte, %s)",
        project.project_id, width, height, coverage * 100,
        "ambiente" if emptied is not None else "recorte directo",
    )
    return layer, warnings


def _cut(
    cutter: MagnificCutoutProvider, source: Path, size: tuple[int, int], workdir: Path
) -> tuple[np.ndarray, Image.Image]:
    """Recorta `source` y devuelve (alfa, RGBA) al tamaño del lienzo."""
    raw = workdir / "cutout_raw.png"
    cutter.cutout(str(source), output_path=str(raw))
    try:
        with Image.open(raw) as opened:
            cut = opened.convert("RGBA")
            if cut.size != size:
                cut = cut.resize(size, Image.Resampling.LANCZOS)
            alpha = np.asarray(cut.getchannel("A"), dtype=np.uint8)
            return alpha, cut.copy()
    finally:
        raw.unlink(missing_ok=True)


def _scene_pass(
    cutter: MagnificCutoutProvider,
    plate: Path,
    size: tuple[int, int],
    workdir: Path,
    scene_model: str | None,
    photo_alpha: np.ndarray,
) -> tuple[np.ndarray, Image.Image, Path, list[str]]:
    """Separa producto y decorado en una foto de ambiente.

    Dos ediciones sobre la misma foto. Una la vacía: comparándola con la original
    se sabe, píxel a píxel, dónde estaba el mueble, y ese trozo —solo ese— se
    sustituye por el cuarto vacío. Fuera de la máscara no se toca nada, así que
    la perspectiva, la línea del piso y el gráfico del KV quedan intactos; si se
    adoptara el cuarto regenerado entero, el encuadre cambiaría y el producto
    acabaría flotando.

    La otra edición deja el mueble sobre fondo blanco, que es donde el recortador
    sí sabe trabajar. Ese recorte se encaja en el hueco que ocupaba el original,
    apoyado abajo, para que siga pisando el suelo.

    Devuelve (alfa, RGBA, plancha limpia, avisos).
    """
    scene = MagnificSceneProvider(model=scene_model)
    if not scene.available():
        raise NoProductFoundError(
            "El recorte trajo la escena completa, no un producto. Para separarlos "
            "hace falta un modelo de edición de Magnific (MAGNIFIC_SCENE_MODEL); "
            f"'{scene.model_id}' no está disponible. Márquelo a mano en Ajustes finos."
        )

    emptied = Path(scene.empty(str(plate), output_path=str(workdir / "vacio.png")))
    hueco = _subject_mask(plate, emptied, photo_alpha)
    if hueco is None:
        emptied.unlink(missing_ok=True)
        raise NoProductFoundError(
            "Al vaciar la escena no cambió nada reconocible: no se pudo aislar un "
            "producto. Márquelo a mano en Ajustes finos."
        )

    avisos = [
        "La foto era de ambiente: el producto se separó del decorado con "
        f"{scene.model_id}. El producto se regenera, así que es fiel al original en "
        "estilo y color, pero no idéntico píxel a píxel."
    ]
    # Cuánto del decorado dice el modelo que era producto. Si es poco, se cambia
    # solo ese trozo y el resto del cuarto queda intacto —perspectiva incluida—.
    # Si es casi todo, el vaciado se desvió tanto que mezclarlo dejaría costuras:
    # se adopta entero y se avisa, porque el encuadre habrá cambiado.
    foto = max(float((photo_alpha > 24).mean()), 1e-6)
    parte = float((hueco > 24).mean()) / foto
    fiel = parte <= SCENE_BLEND_MAX
    if fiel:
        limpia = _blend_hole(plate, emptied, hueco, workdir)
        emptied.unlink(missing_ok=True)
        avisos.append("El fondo solo cambia donde estaba el producto.")
    else:
        _restore_graphics(plate, emptied, photo_alpha)
        limpia = emptied
        avisos.append(
            f"El vaciado rehizo el {parte:.0%} de la fotografía, así que se usa "
            "entera: el decorado cambia de encuadre. Revise el fondo y la posición "
            "del producto antes de producir."
        )

    isolated = Path(scene.isolate(str(plate), output_path=str(workdir / "aislado.png")))
    try:
        alpha, rgba = _cut(cutter, isolated, size, workdir)
    finally:
        isolated.unlink(missing_ok=True)

    # Con la mezcla, el hueco está en las coordenadas del arte original y ahí es
    # donde el producto pisaba el suelo. Si se adoptó el vaciado entero, ese hueco
    # ya no describe la escena nueva: se respeta la posición que el propio recorte
    # trae, que al menos viene de una vista regenerada como la del fondo.
    if fiel:
        alpha, rgba = _fit_into_hole(rgba, hueco, size)
    return alpha, rgba, limpia, avisos


#: Umbral de color a partir del cual se considera que el vaciado cambió el píxel.
SCENE_DIFF_THRESHOLD = 40
#: Bloques más pequeños que esto son ruido del regenerado, no el mueble.
SCENE_MIN_BLOB = 0.005
#: Hasta aquí se cambia solo el hueco; por encima el vaciado se desvió demasiado.
SCENE_BLEND_MAX = 0.45


def _subject_mask(
    plate: Path, emptied: Path, photo_alpha: np.ndarray
) -> np.ndarray | None:
    """Dónde estaba el producto, en las coordenadas del arte original.

    Sale de comparar la foto con su versión vaciada. Dos cuidados aprendidos a
    base de fantasmas: una apertura grande se come las patas metálicas y luego
    reaparecen en el fondo, así que la apertura es mínima y cada bloque se cierra
    por su envolvente convexa —un sofá es un cuerpo, no un encaje—; y todo se
    limita a la zona de fotografía, para que el panel del titular nunca entre.
    """
    with Image.open(plate) as opened:
        original = np.asarray(opened.convert("RGB"), dtype=np.int16)
    with Image.open(emptied) as opened:
        vacia = opened.convert("RGB")
        if vacia.size != (original.shape[1], original.shape[0]):
            vacia = vacia.resize((original.shape[1], original.shape[0]))
        vacia = np.asarray(vacia, dtype=np.int16)

    diff = np.abs(original - vacia).sum(axis=2)
    raw = ((diff > SCENE_DIFF_THRESHOLD) * 255).astype(np.uint8)
    raw[photo_alpha <= 24] = 0
    raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    raw = cv2.morphologyEx(
        raw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))
    )

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (raw > 0).astype(np.uint8), 8
    )
    mask = np.zeros_like(raw)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] / raw.size < SCENE_MIN_BLOB:
            continue
        blob = (labels == index).astype(np.uint8)
        contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            cv2.fillConvexPoly(mask, cv2.convexHull(contour), 255)
    if not mask.any():
        return None
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)))
    # El cierre y la envolvente se salen de la foto; recortarlo evita además que
    # la proporción que se le enseña al usuario pase del 100 %.
    mask[photo_alpha <= 24] = 0
    return mask if mask.any() else None


def _restore_graphics(plate: Path, generated: Path, photo_alpha: np.ndarray) -> None:
    """Devuelve al decorado regenerado los gráficos del KV que no son foto.

    Solo hace falta cuando se adopta el vaciado entero: el modelo rehace la
    imagen y se lleva por delante el panel del titular, la franja de color o el
    degradado. El primer recorte ya dijo dónde está la fotografía; fuera de ahí
    mandan los píxeles originales.
    """
    with Image.open(plate) as opened:
        original = opened.convert("RGB")
    with Image.open(generated) as opened:
        nueva = opened.convert("RGB")
    if nueva.size != original.size:
        nueva = nueva.resize(original.size, Image.Resampling.LANCZOS)
    dentro = Image.fromarray(((photo_alpha > 24) * 255).astype(np.uint8), mode="L")
    if dentro.size != original.size:
        dentro = dentro.resize(original.size, Image.Resampling.NEAREST)
    dentro = dentro.filter(ImageFilter.GaussianBlur(2))
    Image.composite(nueva, original, dentro).save(generated, format="PNG", optimize=True)


def _blend_hole(plate: Path, emptied: Path, hole: np.ndarray, workdir: Path) -> Path:
    """Plancha limpia: el cuarto vacío solo dentro del hueco, el resto intacto."""
    with Image.open(plate) as opened:
        original = opened.convert("RGB")
    with Image.open(emptied) as opened:
        vacia = opened.convert("RGB")
        if vacia.size != original.size:
            vacia = vacia.resize(original.size, Image.Resampling.LANCZOS)
    borde = Image.fromarray(hole, mode="L").filter(ImageFilter.GaussianBlur(9))
    target = workdir / "plancha_limpia.png"
    Image.composite(vacia, original, borde).save(target, format="PNG", optimize=True)
    return target


def _fit_into_hole(
    cut: Image.Image, hole: np.ndarray, size: tuple[int, int]
) -> tuple[np.ndarray, Image.Image]:
    """Coloca el recorte regenerado en el hueco que ocupaba el producto.

    Se apoya en el borde inferior del hueco: un mueble tiene que pisar el suelo,
    y centrarlo verticalmente lo dejaría flotando.
    """
    ys, xs = np.nonzero(hole > 24)
    hx0, hx1 = int(xs.min()), int(xs.max())
    hy0, hy1 = int(ys.min()), int(ys.max())
    box_w, box_h = hx1 - hx0 + 1, hy1 - hy0 + 1

    recorte = cut.crop(cut.getchannel("A").getbbox() or (0, 0, *cut.size))
    escala = min(box_w / recorte.width, box_h / recorte.height)
    nuevo = recorte.resize(
        (max(1, round(recorte.width * escala)), max(1, round(recorte.height * escala))),
        Image.Resampling.LANCZOS,
    )
    lienzo = Image.new("RGBA", size, (0, 0, 0, 0))
    lienzo.paste(nuevo, (hx0 + (box_w - nuevo.width) // 2, hy1 - nuevo.height + 1))
    recorte.close()
    nuevo.close()
    cut.close()
    return np.asarray(lienzo.getchannel("A"), dtype=np.uint8), lienzo


def _adopt_plate(project: Project, generated: Path, used: str) -> list[str]:
    """Deja una imagen ya generada como plancha de fondo del proyecto."""
    target = storage.abs_path(project.project_id, BACKGROUND_REL)
    target.parent.mkdir(parents=True, exist_ok=True)
    generated.replace(target)
    project.background = BackgroundInfo(
        path=BACKGROUND_REL,
        provider=f"{project.background.provider}+{used}" if project.background.path else used,
        generated_at=utcnow(),
        warnings=["Decorado regenerado sin el producto."],
    )
    return [f"Fondo rehecho sin el producto, con {used}."]


def _clean_plate(
    project: Project,
    plate: Path,
    alpha: np.ndarray,
    dilate: int,
    provider_name: str | None,
    model: str | None,
    prompt: str | None,
) -> list[str]:
    """Rellena el hueco que deja el producto y lo deja como plancha de fondo.

    El resultado siempre va a `backgrounds/background.png`, nunca sobre el arte
    original: el archivo que subió el usuario no se toca. Si el proyecto aún no
    tenía plancha (un JPG plano, sin PSD), esta pasa a serlo.
    """
    warnings: list[str] = []
    coverage = float((alpha > 24).mean())
    if coverage > RISKY_FILL_RATIO:
        warnings.append(
            f"El producto ocupa el {coverage:.0%} del arte: reconstruir un hueco tan "
            "grande da resultados poco fiables. Revise el fondo antes de producir."
        )
    mask = dilate_mask((alpha > 24).astype(np.uint8) * 255, dilate)
    mask_path = storage.abs_path(project.project_id, "backgrounds/erase_producto.png")
    save_mask(mask_path, mask)

    target = storage.abs_path(project.project_id, BACKGROUND_REL)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Se rellena en un archivo aparte y solo se mueve si sale bien: si el
    # proveedor falla a medias, la plancha anterior sigue intacta.
    staged = target.with_name("plate_sin_producto.png")
    provider = get_inpainting_provider(provider_name, model)
    used = getattr(provider, "name", "opencv")
    try:
        provider.fill(str(plate), str(mask_path), prompt=prompt, output_path=str(staged))
    except Exception as exc:  # noqa: BLE001 - el fondo no debe bloquear el recorte
        staged.unlink(missing_ok=True)
        warnings.append(
            f"El producto se recortó, pero el fondo no se pudo limpiar con {used} "
            f"({exc}). Queda la foto original detrás."
        )
        return warnings

    staged.replace(target)
    project.background = BackgroundInfo(
        path=BACKGROUND_REL,
        provider=f"{project.background.provider}+{used}" if project.background.path else used,
        generated_at=utcnow(),
        warnings=[f"Se borró de la foto el producto recortado, con {used}."],
    )
    warnings.append(f"Fondo limpiado con {used} tras recortar el producto.")
    return warnings
