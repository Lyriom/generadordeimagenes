"""Descomposición asistida del arte aplanado en capas.

Un JPG/PNG aplanado no contiene capas: esto es una aproximación. El resultado
siempre trae confianza y advertencias para que el usuario corrija en Ajustes finos.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from ..models import (
    CATEGORY_LABELS_ES,
    AnalysisInfo,
    Layer,
    LayerCategory,
    LayerType,
    Project,
    utcnow,
)
from . import layer_extraction, ocr as ocr_service, segmentation as seg_service, storage
from .imaging import box_mask, load_alpha, mask_bbox, read_bgr_flat

logger = logging.getLogger(__name__)

#: Orden vertical por defecto (abajo → arriba).
Z_ORDER: dict[LayerCategory, int] = {
    LayerCategory.BACKGROUND: 0,
    LayerCategory.DECORATION: 1,
    LayerCategory.PERSON: 2,
    LayerCategory.PRODUCT: 3,
    LayerCategory.LEGAL: 4,
    LayerCategory.SUBHEADLINE: 5,
    LayerCategory.HEADLINE: 6,
    LayerCategory.PRICE: 7,
    LayerCategory.CTA: 8,
    LayerCategory.LOGO: 9,
}

DEFAULT_LOCKS = {LayerCategory.LOGO, LayerCategory.PRODUCT, LayerCategory.PERSON}

LOW_CONFIDENCE = 0.55


def _overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Fracción del área de `a` cubierta por `b`."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / float(max(1, aw * ah))


def detect_faces(image_path: str) -> list[tuple[int, int, int, int]]:
    """Detección de rostros con Haar cascade (incluida en OpenCV, sin descargas)."""
    try:
        # `cv2.data` existe en tiempo de ejecución; las anotaciones de OpenCV no
        # lo declaran.
        cascade_path = f"{cv2.data.haarcascades}haarcascade_frontalface_default.xml"  # type: ignore[attr-defined]
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            return []
        bgr, _ = read_bgr_flat(image_path)  # aplanado sobre blanco, no sobre negro
        image = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        if image is None:
            return []
        scale = 1.0
        if max(image.shape[:2]) > 1200:
            scale = 1200 / max(image.shape[:2])
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        faces = cascade.detectMultiScale(image, scaleFactor=1.15, minNeighbors=5, minSize=(28, 28))
        return [
            (
                int(x / scale),
                int(y / scale),
                int(w / scale),
                int(h / scale),
            )
            for (x, y, w, h) in faces
        ]
    except Exception as exc:  # noqa: BLE001 - la detección de caras es opcional
        logger.info("Detección de rostros no disponible: %s", exc)
        return []


def _classify_visual(
    det_box: tuple[int, int, int, int],
    image_size: tuple[int, int],
    faces: list[tuple[int, int, int, int]],
    assigned: dict[LayerCategory, int],
) -> tuple[LayerCategory, float]:
    width, height = image_size
    x, y, w, h = det_box
    area_ratio = (w * h) / float(max(1, width * height))
    cx = (x + w / 2) / width
    cy = (y + h / 2) / height
    aspect = w / max(1, h)

    for fx, fy, fw, fh in faces:
        face_center = (fx + fw / 2, fy + fh / 2)
        if x <= face_center[0] <= x + w and y <= face_center[1] <= y + h:
            return LayerCategory.PERSON, 0.7

    corner_zone = (cy < 0.18 or cy > 0.88) and (cx < 0.3 or cx > 0.7)
    if (
        LayerCategory.LOGO not in assigned
        and area_ratio < 0.06
        and corner_zone
        and 0.25 <= aspect <= 6.0
    ):
        return LayerCategory.LOGO, 0.5

    if LayerCategory.PRODUCT not in assigned and area_ratio >= 0.05:
        return LayerCategory.PRODUCT, 0.6

    return LayerCategory.DECORATION, 0.4


def _unique_name(base: str, used: dict[str, int]) -> str:
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base} {used[base]}"


def _uploaded_logo_layer(project: Project, layers: list[Layer]) -> Layer | None:
    """Convierte el logo subido en una capa bloqueada si no se detectó ninguno."""
    reference = project.references.logo
    if reference is None:
        return None
    if any(layer.category == LayerCategory.LOGO for layer in layers):
        return None

    target_w = max(40, int(project.canvas.width * 0.18))
    aspect = reference.width / max(1, reference.height)
    target_h = max(20, int(target_w / max(aspect, 0.01)))
    layer = Layer(
        name=CATEGORY_LABELS_ES[LayerCategory.LOGO],
        type=LayerType.IMAGE,
        category=LayerCategory.LOGO,
        src=reference.path,
        x=int(project.canvas.width * 0.05),
        y=int(project.canvas.height * 0.04),
        width=target_w,
        height=target_h,
        z_index=Z_ORDER[LayerCategory.LOGO],
        locked=True,
        preserve_aspect_ratio=True,
        confidence=1.0,
        source="upload",
        extracted=True,
    )
    # No pertenece al arte original: no debe borrarse del fondo reconstruido.
    layer.meta["external"] = True
    return layer


def analyze_project(
    project: Project,
    *,
    run_segmentation: bool = True,
    run_ocr: bool = True,
    max_regions: int = 12,
    extract: bool = True,
) -> tuple[list[Layer], list[str], str | None, str | None]:
    """Genera las capas propuestas. Reemplaza el análisis anterior del proyecto."""
    image_rel = project.source.path
    image_path = str(storage.abs_path(project.project_id, image_rel))
    image_size = (project.source.width, project.source.height)
    shape = (project.source.height, project.source.width)

    warnings: list[str] = []
    layers: list[Layer] = []
    used_names: dict[str, int] = {}
    seg_provider_name: str | None = None
    ocr_provider_name: str | None = None

    # ------------------------------------------------------------------ textos
    text_payloads: list[dict] = []
    if run_ocr:
        result = ocr_service.run_ocr(image_path)
        ocr_provider_name = result.provider if result.available else None
        warnings.extend(result.warnings)
        if result.available:
            text_payloads = ocr_service.build_text_layer_payloads(result, image_size)
            if not text_payloads:
                warnings.append("El OCR no encontró texto. Puede añadir capas de texto a mano.")
    else:
        warnings.append("OCR omitido por configuración de la petición.")

    text_boxes = [
        (p["x"], p["y"], p["width"], p["height"]) for p in text_payloads
    ]

    # -------------------------------------------------------------- detecciones
    detections = []
    if run_segmentation:
        detections, seg_warnings = seg_service.detect_regions(image_path, max_regions=max_regions)
        warnings.extend(seg_warnings)
        seg_provider_name = seg_service.active_provider_name()
    faces = detect_faces(image_path) if run_segmentation else []

    # ------------------------------------------------------------ capa de fondo
    background_layer = Layer(
        name=CATEGORY_LABELS_ES[LayerCategory.BACKGROUND],
        type=LayerType.IMAGE,
        category=LayerCategory.BACKGROUND,
        x=0,
        y=0,
        width=project.canvas.width,
        height=project.canvas.height,
        z_index=Z_ORDER[LayerCategory.BACKGROUND],
        movable=False,
        resizable=False,
        reorderable=False,
        locked=False,
        confidence=1.0,
        source="auto",
    )
    layers.append(background_layer)

    # ------------------------------------------- recorte con fondo transparente
    # Si la imagen trae alfa, ese canal ES la máscara del sujeto: no hay que
    # adivinar nada. Este es el caso de los PNG de producto ya recortados.
    source_alpha = load_alpha(image_path)
    cutout_ratio = float((source_alpha < 10).mean()) if source_alpha is not None else 0.0
    # El alfa recortado se guarda en su propia variable en vez de en un sí/no:
    # dentro de la rama ya no hay que volver a preguntarse si existe.
    cutout_alpha = (
        source_alpha if (source_alpha is not None and cutout_ratio > 0.12) else None
    )
    if cutout_alpha is not None:
        bbox = mask_bbox(cutout_alpha, threshold=8)
        if bbox is not None:
            cutout_layer = Layer(
                name=_unique_name(CATEGORY_LABELS_ES[LayerCategory.PRODUCT], used_names),
                type=LayerType.IMAGE,
                category=LayerCategory.PRODUCT,
                x=bbox[0],
                y=bbox[1],
                width=bbox[2],
                height=bbox[3],
                z_index=Z_ORDER[LayerCategory.PRODUCT],
                locked=True,
                preserve_aspect_ratio=True,
                confidence=0.97,
                source="auto",
            )
            cutout_layer.meta["from_alpha"] = True
            layer_extraction.write_mask(project, cutout_layer, cutout_alpha)
            layers.append(cutout_layer)
            assigned_cutout = True
        else:
            assigned_cutout = False
        warnings.append(
            f"Esto parece un producto recortado, no un arte publicitario "
            f"({cutout_ratio * 100:.0f}% del área es transparente). Se usó el canal alfa "
            "como máscara exacta del producto, pero aquí no hay titulares, precio ni logo "
            "que recomponer. Si lo que quiere es meter este producto en un KV de campaña: "
            "abra el KV como arte y use «Cambiar el producto del KV»."
        )
        # Con el alfa disponible, las detecciones por contraste solo añaden ruido.
        detections = []
    else:
        assigned_cutout = False

    # --------------------------------------------------------- capas visuales
    assigned: dict[LayerCategory, int] = {}
    if assigned_cutout:
        assigned[LayerCategory.PRODUCT] = 1
    detections = sorted(detections, key=lambda det: det.area, reverse=True)
    for det in detections:
        box = (det.x, det.y, det.width, det.height)
        if any(_overlap_ratio(box, tbox) > 0.55 for tbox in text_boxes):
            continue  # la región es texto: la maneja el OCR
        if det.width < 20 or det.height < 20:
            continue
        category, heuristic_confidence = _classify_visual(box, image_size, faces, assigned)
        assigned[category] = assigned.get(category, 0) + 1
        confidence = round(min(0.95, (det.score * 0.5) + (heuristic_confidence * 0.5)), 3)

        layer = Layer(
            name=_unique_name(CATEGORY_LABELS_ES[category], used_names),
            type=LayerType.IMAGE,
            category=category,
            x=det.x,
            y=det.y,
            width=max(1, det.width),
            height=max(1, det.height),
            z_index=Z_ORDER[category],
            locked=category in DEFAULT_LOCKS,
            preserve_aspect_ratio=True,
            confidence=confidence,
            source="auto",
        )
        # La zona la propuso OpenCV por contraste; si SAM está, se le pide la
        # silueta de lo que hay dentro de ese rectángulo. Es la diferencia entre
        # un recorte con fondo alrededor —y con un pedazo del producto de al
        # lado— y el objeto solo. Si SAM no está o no ve nada, se queda la de
        # OpenCV, que es lo que había antes.
        mask = seg_service.refine_box(image_path, box)
        if mask is not None:
            layer.meta["segmented_with"] = "sam"
        else:
            mask = det.mask if det.mask is not None else box_mask(shape, box)
        if mask.shape[:2] != shape:  # pragma: no cover - defensivo
            mask = cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
        layer_extraction.write_mask(project, layer, mask.astype(np.uint8))

        if confidence < LOW_CONFIDENCE:
            layer.warnings.append(
                "Detección de baja confianza: revise la máscara y la categoría."
            )
        if category == LayerCategory.PRODUCT and det.meta.get("area_ratio", 0) > 0.7:
            layer.warnings.append(
                "La región cubre casi todo el arte: probablemente incluya el fondo."
            )
        layers.append(layer)

    # ----------------------------------------------------------- capas de texto
    for payload in text_payloads:
        category: LayerCategory = payload["category"]
        layer = Layer(
            name=_unique_name(CATEGORY_LABELS_ES[category], used_names),
            type=LayerType.TEXT,
            category=category,
            content=payload["content"],
            x=payload["x"],
            y=payload["y"],
            width=max(1, payload["width"]),
            height=max(1, payload["height"]),
            font_size=payload["font_size"],
            font_weight="bold"
            if category in {LayerCategory.HEADLINE, LayerCategory.PRICE, LayerCategory.CTA}
            else "normal",
            color=payload["color"],
            z_index=Z_ORDER[category],
            confidence=payload["confidence"],
            source="ocr",
            warnings=list(payload["warnings"]),
            movable=True,
            resizable=True,
            reorderable=True,
        )
        layer.meta["ocr_angle"] = payload["angle"]
        mask = box_mask(shape, (layer.x, layer.y, layer.width, layer.height))
        layer_extraction.write_mask(project, layer, mask)
        layers.append(layer)

    # ------------------------------------- logo subido aparte (si no se detectó)
    logo_layer = _uploaded_logo_layer(project, layers)
    if logo_layer is not None:
        layers.append(logo_layer)
        warnings.append(
            "Se añadió el logo subido como capa bloqueada (píxeles originales)."
        )

    # ------------------------------------------------------------- advertencias
    if not any(layer.category == LayerCategory.PRODUCT for layer in layers):
        warnings.append(
            "No se identificó un producto. Créelo a mano en Ajustes finos "
            "para obtener mejores composiciones."
        )
    if not any(layer.category == LayerCategory.LOGO for layer in layers):
        warnings.append(
            "No se identificó un logo. Puede subirlo aparte o crear la capa a mano."
        )
    if not any(layer.type == LayerType.TEXT for layer in layers):
        warnings.append(
            "No hay capas de texto: las variantes saldrán sin titular, precio ni CTA. "
            "Escriba los textos en Ajustes finos (o habilite el OCR)."
        )
    warnings.append(
        "La separación desde un arte aplanado es aproximada: revise máscaras y "
        "categorías antes de generar variantes."
    )

    project.layers = layers

    # Se extraen los PNG en el mismo paso: una capa imagen sin PNG no sirve para
    # generar variantes, y olvidarlo era la principal fuente de confusión.
    if extract:
        _, _, extract_warnings = layer_extraction.extract_layers(
            project,
            [layer.id for layer in layers if layer.type == LayerType.IMAGE],
            feather=2,
        )
        warnings.extend(extract_warnings)

    project.analysis = AnalysisInfo(
        ran_at=utcnow(),
        segmentation_provider=seg_provider_name,
        ocr_provider=ocr_provider_name,
        warnings=warnings,
        detections=sum(1 for layer in layers if layer.type == LayerType.IMAGE) - 1,
        text_regions=sum(1 for layer in layers if layer.type == LayerType.TEXT),
    )
    return layers, warnings, seg_provider_name, ocr_provider_name
