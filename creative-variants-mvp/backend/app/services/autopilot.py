"""Modo automático: un solo paso de principio a fin.

El flujo completo (detectar → recortar → rellenar fondo → componer) existe como
endpoints separados para quien necesite control fino. Este servicio los encadena
con valores por defecto sensatos, para que la interfaz simple sea un botón.
"""
from __future__ import annotations

import logging

from ..models import (
    GenerateRequest,
    LEGACY_FORMATS,
    LayerCategory,
    LayerType,
    Project,
    Variant,
)
from . import analysis, inpainting, layer_extraction, storage, variants as variants_service
from .layout_engine import FAITHFUL_LAYOUT

logger = logging.getLogger(__name__)

# Formatos que se añaden siempre porque son los de mayor uso en redes.
SOCIAL_DEFAULTS = ("1080x1080", "1080x1350")

def usable_layers(project: Project) -> list:
    """Capas que el motor de composición puede usar tal como están."""
    ready = []
    for layer in project.layers:
        if layer.category == LayerCategory.BACKGROUND or not layer.visible:
            continue
        if layer.type == LayerType.TEXT and (layer.content or "").strip():
            ready.append(layer)
        elif layer.type == LayerType.IMAGE and layer.src:
            ready.append(layer)
    return ready


def auto_formats(project: Project) -> list[str]:
    """Formato nativo del arte (el más cercano por proporción) más los de redes."""
    aspect = project.canvas.width / max(1, project.canvas.height)
    native = min(
        LEGACY_FORMATS,
        key=lambda key: abs(LEGACY_FORMATS[key][0] / LEGACY_FORMATS[key][1] - aspect),
    )
    formats = [native]
    formats.extend(fmt for fmt in SOCIAL_DEFAULTS if fmt != native)
    return formats


def native_format(project: Project) -> str:
    """Preset con tamaño/proporción más cercanos al lienzo original."""
    exact = next(
        (
            key
            for key, size in LEGACY_FORMATS.items()
            if size == (project.canvas.width, project.canvas.height)
        ),
        None,
    )
    if exact:
        return exact
    aspect = project.canvas.width / max(1, project.canvas.height)
    return min(
        LEGACY_FORMATS,
        key=lambda key: (
            abs(LEGACY_FORMATS[key][0] / LEGACY_FORMATS[key][1] - aspect),
            abs(LEGACY_FORMATS[key][0] - project.canvas.width)
            + abs(LEGACY_FORMATS[key][1] - project.canvas.height),
        ),
    )


def _step(name: str, detail: str, ok: bool = True) -> dict:
    return {"name": name, "detail": detail, "ok": ok}


def run(project: Project, request) -> tuple[list[dict], list[Variant], list[str]]:
    """Ejecuta el flujo completo. Devuelve (pasos, variantes, advertencias)."""
    steps: list[dict] = []
    warnings: list[str] = []

    # 1 · Detectar. Un PSD ya trae sus capas: no se vuelve a adivinar.
    ready = usable_layers(project)
    from_psd = (project.analysis.segmentation_provider or "") == "psd"
    if from_psd:
        steps.append(_step("Leer el PSD", f"{len(ready)} elementos con su recorte real"))
    elif len(ready) >= 2:
        steps.append(_step("Detectar elementos", f"{len(ready)} elementos ya estaban listos"))
    else:
        layers, analyze_warnings, seg_provider, ocr_provider = analysis.analyze_project(
            project,
            run_segmentation=True,
            run_ocr=True,
            max_regions=12,
            extract=True,
        )
        warnings.extend(analyze_warnings)
        texts = sum(1 for layer in layers if layer.type == LayerType.TEXT)
        detail = f"{len(layers)} elementos ({texts} de texto)"
        if not ocr_provider:
            detail += " · sin lector de texto instalado"
        steps.append(_step("Detectar elementos", detail, ok=bool(layers)))
        logger.info("autopilot: análisis con %s / %s", seg_provider, ocr_provider)

    # 2 · Recortar lo que falte como PNG transparente.
    extracted, skipped, extract_warnings = layer_extraction.extract_layers(
        project, None, feather=2, force=False
    )
    warnings.extend(extract_warnings)
    if extracted:
        steps.append(_step("Recortar elementos", f"{len(extracted)} recortes nuevos"))
    else:
        steps.append(
            _step("Recortar elementos", f"nada nuevo por recortar ({len(skipped)} omitidos)")
        )

    # 3 · Fondo. El PSD suele traerlo; si no, se reconstruye.
    replacement_only = bool(request.template_mode)
    if project.background.path and (replacement_only or not request.regenerate_background):
        origin = "el archivo ya traía el fondo" if from_psd else "ya estaba reconstruido"
        steps.append(_step("Preparar el fondo", origin))
    else:
        try:
            _, provider_name, background_warnings = inpainting.reconstruct_background(
                project,
                prompt=None if replacement_only else request.background_prompt,
                preferred_provider=(
                    "opencv" if replacement_only else request.background_provider
                ),
                model=(
                    None if replacement_only else getattr(request, "background_model", None)
                ),
            )
            warnings.extend(background_warnings)
            steps.append(_step("Preparar el fondo", f"reconstruido con {provider_name}"))
        except Exception as exc:  # noqa: BLE001 - el fondo no debe bloquear la generación
            logger.warning("autopilot: fondo no reconstruido: %s", exc)
            steps.append(
                _step(
                    "Preparar el fondo",
                    "no se pudo reconstruir: se usa el arte original",
                    ok=False,
                )
            )
            warnings.append(
                "No se pudo reconstruir el fondo; las variantes usan el arte original y "
                "pueden mostrar restos de los elementos movidos."
            )

    # 4 · Componer.
    formats = (
        [native_format(project)]
        if replacement_only
        else (list(request.formats or []) or auto_formats(project))
    )
    requested_count = 1 if replacement_only else request.count
    # Se exploran varias composiciones y se entregan pocas: calidad sobre volumen.
    generate = GenerateRequest(
        # Debe existir al menos un candidato por formato. Antes, seleccionar más
        # formatos que el valor del slider dejaba algunos sin producir.
        count=(
            1
            if replacement_only
            else min(30, max(requested_count * 3, len(formats) * 2, requested_count))
        ),
        seed=request.seed,
        formats=formats,
        intensity="conservative" if replacement_only else request.intensity,
        instruction=None if replacement_only else request.instruction,
        product_position_instruction=(
            None if replacement_only else request.product_position_instruction
        ),
        replace_existing=request.replace_existing,
        product_label=request.product_label,
        product_arrangement=request.product_arrangement,
        layouts=[FAITHFUL_LAYOUT] if replacement_only else None,
    )
    variants, generate_warnings = variants_service.generate_variants(project, generate)
    warnings.extend(generate_warnings)
    if variants:
        quality_first = False
        minimum_score = 80 if quality_first else 0
        ranked_by_format: dict[str, list[Variant]] = {}
        eligible = [
            variant
            for variant in variants
            if variant.quality.score >= minimum_score
            and (
                not quality_first
                or variant.quality.metrics.get("severe_overlaps", 0) == 0
            )
        ]
        for variant in sorted(eligible, key=lambda item: item.quality.score, reverse=True):
            ranked_by_format.setdefault(variant.format, []).append(variant)
        selected: list[Variant] = []
        formats_in_order = list(dict.fromkeys(formats))
        # Primero una salida por cada preset solicitado. Si ninguna supera el
        # umbral se conserva la mejor de ese formato con una advertencia: pedir
        # una medida y no recibirla es peor que verla marcada para revisión.
        all_by_format: dict[str, list[Variant]] = {}
        for variant in sorted(variants, key=lambda item: item.quality.score, reverse=True):
            all_by_format.setdefault(variant.format, []).append(variant)
        for fmt in formats_in_order:
            bucket = ranked_by_format.get(fmt) or []
            fallback = all_by_format.get(fmt) or []
            if bucket:
                selected.append(bucket.pop(0))
            elif fallback:
                selected.append(fallback[0])
                warnings.append(
                    f"La mejor propuesta de {fmt} no superó el control de "
                    f"{minimum_score}/100; se incluye para no omitir el formato."
                )

        target_total = max(requested_count, len(formats_in_order))
        while len(selected) < target_total:
            added = False
            for fmt in formats_in_order:
                bucket = ranked_by_format.get(fmt) or []
                while bucket and bucket[0] in selected:
                    bucket.pop(0)
                if bucket and len(selected) < target_total:
                    candidate = bucket.pop(0)
                    if candidate not in selected:
                        selected.append(candidate)
                    added = True
            if not added:
                break
        keep = {variant.id for variant in selected}
        rejected = [variant for variant in variants if variant.id not in keep]
        for variant in rejected:
            for relative in (
                variant.image,
                variant.thumbnail,
                variant.meta.get("psd"),
                variant.meta.get("svg"),
            ):
                if relative:
                    storage.abs_path(project.project_id, relative).unlink(missing_ok=True)
        project.variants = [
            variant for variant in project.variants if variant.id not in {item.id for item in rejected}
        ]
        variants = selected
        if selected:
            warnings.append(
                f"Se evaluaron {len(selected) + len(rejected)} composiciones y se "
                f"entregaron {len(selected)} que superaron {minimum_score}/100 sin "
                "solapamientos graves."
            )
        else:
            warnings.append(
                f"Ninguna composición superó el control mínimo de {minimum_score}/100 "
                "sin solapamientos graves. No se entregaron artes defectuosos."
            )
    if variants:
        average = sum(variant.quality.score for variant in variants) / len(variants)
        steps.append(
            _step(
                "Componer variantes",
                f"{len(variants)} propuestas en {len(formats)} tamaños · "
                f"puntaje promedio {average:.0f}/100",
            )
        )
    else:
        steps.append(_step("Componer variantes", "no se pudo componer ninguna", ok=False))

    # Advertencias repetidas: una sola vez, conservando el orden.
    return steps, variants, list(dict.fromkeys(warnings))
