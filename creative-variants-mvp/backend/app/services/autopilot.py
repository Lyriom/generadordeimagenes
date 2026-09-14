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
from . import (
    analysis,
    art_text,
    inpainting,
    layer_extraction,
    layout_engine,
    storage,
    variants as variants_service,
)

logger = logging.getLogger(__name__)

# Formatos que se añaden siempre porque son los de mayor uso en redes.
SOCIAL_DEFAULTS = ("1080x1080", "1080x1350")

#: Tope de composiciones por tanda, el mismo que acepta `GenerateRequest`.
MAX_COMPOSITIONS = 30


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
    """Formato nativo del arte más los de redes que ese arte pueda llenar.

    Los de redes se añadían siempre, y con un banner de 1920x325 eso significaba
    prometer un 1080x1350 que solo se puede rellenar de color: el arte cubre el
    14% del lienzo. Si el arte trae capas el motor recompone y no hay problema;
    si llegó plano, se ofrece solo lo que de verdad se puede sacar de él.
    """
    aspect = project.canvas.width / max(1, project.canvas.height)
    native = min(
        LEGACY_FORMATS,
        key=lambda key: abs(LEGACY_FORMATS[key][0] / LEGACY_FORMATS[key][1] - aspect),
    )
    formats = [native]
    extras = [fmt for fmt in SOCIAL_DEFAULTS if fmt != native]
    viables, _ = layout_engine.viable_formats(project, extras, len(usable_layers(project)))
    formats.extend(viables)
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

    # 3.b · Copy de esta tanda. Cada producto de un catálogo lleva su nombre y su
    # precio: sin esto la fila entera salía con el copy del primero.
    overrides = getattr(request, "text_overrides", None)
    if overrides is not None:
        text_warnings = art_text.apply_batch(
            project, {item.layer_id: item.content for item in overrides}
        )
        warnings.extend(text_warnings)
        escritos = sum(1 for item in overrides if item.content.strip())
        steps.append(
            _step(
                "Escribir el copy",
                f"{escritos} texto(s) del arte reescritos para esta tanda",
                ok=True,
            )
        )

    # 4 · Componer.
    # Los formatos elegidos se respetan SIEMPRE, también en sustitución fiel. Que
    # ahí se forzara el tamaño nativo es lo que hacía que pedir cinco medidas
    # devolviera una sola pieza, en un tamaño que nadie había pedido. Conservar
    # el diseño no es conservar el lienzo: el motor sabe llevar la misma
    # composición a otra proporción, y para eso están los layouts anclados.
    formats = list(dict.fromkeys(request.formats or [])) or (
        [native_format(project)] if replacement_only else auto_formats(project)
    )
    # `count` son propuestas POR FORMATO. Repartir una cifra única entre las
    # medidas elegidas era la otra mitad del problema: pedir tres propuestas y
    # cuatro formatos devolvía tres piezas, no doce. En sustitución fiel solo
    # existe una composición por medida —el diseño del KV es el que es—, así que
    # ahí se entrega una y se explica por qué.
    per_format = 1 if replacement_only else max(1, request.count)
    if replacement_only and request.count > 1:
        warnings.append(
            f"Se pidieron {request.count} propuestas por formato con el diseño del "
            "KV conservado, y ahí solo hay una composición posible por medida: se "
            f"entrega una por formato ({len(formats)} en total). Para varias "
            "propuestas distintas, desmarque «conservar el diseño del KV»."
        )

    # Conservar el diseño apaga las indicaciones y el fondo nuevo. Antes era
    # imposible pedir las dos cosas a la vez —el modo se deducía de que no
    # hubiera ninguna—; ahora es una casilla, y lo que se descarta se dice.
    if replacement_only:
        ignorado = []
        if request.instruction:
            ignorado.append("las indicaciones de composición")
        if request.regenerate_background:
            ignorado.append("el fondo nuevo con IA")
        if ignorado:
            warnings.append(
                "Con el diseño del KV conservado no se aplican "
                + " ni ".join(ignorado)
                + ": el arte sale tal cual, solo con el producto cambiado. "
                "Desmarque «conservar el diseño del KV» para tenerlos en cuenta."
            )

    if len(formats) > MAX_COMPOSITIONS:
        # Una tanda no pasa de `MAX_COMPOSITIONS` composiciones, así que con más
        # formatos que ese tope los últimos se quedaban sin ninguna pieza y sin
        # que nadie lo dijera. Se recortan aquí, y se dice cuáles.
        sobran = formats[MAX_COMPOSITIONS:]
        formats = formats[:MAX_COMPOSITIONS]
        warnings.append(
            f"Se pidieron {len(formats) + len(sobran)} formatos y una tanda no pasa "
            f"de {MAX_COMPOSITIONS} composiciones: se generaron los primeros "
            f"{len(formats)}. Quedaron fuera {', '.join(sobran)}; pídalos en otra "
            "tanda."
        )

    # Cuántas piezas por formato caben de verdad en la tanda. Entregar menos de
    # lo pedido se puede; entregarlo en silencio, no.
    por_formato_entregadas = max(1, min(per_format, MAX_COMPOSITIONS // len(formats)))
    if por_formato_entregadas < per_format:
        warnings.append(
            f"Se pidieron {per_format} propuestas en {len(formats)} formatos "
            f"({per_format * len(formats)} piezas) y una tanda no pasa de "
            f"{MAX_COMPOSITIONS}: se entregan {por_formato_entregadas} por formato "
            f"({por_formato_entregadas * len(formats)} piezas). Pida el resto en "
            "otra tanda o elija menos formatos."
        )
    target_total = por_formato_entregadas * len(formats)
    # Se exploran más composiciones de las que se entregan y se queda la mejor de
    # cada formato: calidad sobre volumen. En sustitución fiel no hay nada que
    # explorar —el diseño es el del KV—, así que se compone justo lo que se
    # entrega y la tanda tarda la mitad.
    total = (
        target_total
        if replacement_only
        else min(MAX_COMPOSITIONS, max(target_total * 2, len(formats) * 2))
    )
    generate = GenerateRequest(
        count=total,
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
        anchored_layouts=replacement_only,
    )
    variants, generate_warnings = variants_service.generate_variants(project, generate)
    warnings.extend(generate_warnings)
    if variants:
        # Se componen de más y se entrega lo mejor de cada formato. No hay nota
        # mínima: descartar por puntaje dejaba sin ninguna pieza un formato que
        # el usuario había pedido, y eso es peor que entregarla marcada para
        # revisión —el aviso de qué le falta ya viaja en la propia variante—.
        # Lo que no se entrega se borra del disco.
        por_formato: dict[str, list[Variant]] = {}
        for variant in sorted(variants, key=lambda item: item.quality.score, reverse=True):
            por_formato.setdefault(variant.format, []).append(variant)

        formats_in_order = list(dict.fromkeys(formats))
        # Primero una salida por cada preset solicitado; pedir una medida y no
        # recibirla es el peor resultado posible.
        selected: list[Variant] = [
            por_formato[fmt].pop(0) for fmt in formats_in_order if por_formato.get(fmt)
        ]

        # Y con el cupo que quede, la siguiente mejor de cada formato por turnos.
        while len(selected) < target_total:
            added = False
            for fmt in formats_in_order:
                if len(selected) >= target_total:
                    break
                bucket = por_formato.get(fmt) or []
                if bucket:
                    selected.append(bucket.pop(0))
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
            peor = min(variant.quality.score for variant in selected)
            # Las medidas se cuentan sobre lo entregado, no sobre lo pedido: si
            # alguna se descartó por no poder sacarse del arte, ya lo dijo su
            # propio aviso y repetirla aquí sería contarla dos veces.
            medidas = len({variant.format for variant in selected})
            warnings.append(
                f"Se compusieron {len(selected) + len(rejected)} propuestas y se "
                f"entregaron las {len(selected)} de mejor puntaje, "
                f"{por_formato_entregadas} por formato en {medidas} medida(s) "
                f"(la más baja, {peor}/100). Revise los avisos de cada pieza "
                "antes de publicarla."
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
