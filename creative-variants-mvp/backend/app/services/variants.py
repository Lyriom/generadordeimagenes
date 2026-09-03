"""Orquestación de la generación de variantes: plan → render → puntaje → disco."""
from __future__ import annotations

import logging

from ..models import Project, Variant, VariantLayerPlacement, format_spec, new_id
from . import export as export_service
from . import layout_engine, predictor, quality, renderer, storage

logger = logging.getLogger(__name__)


def _to_placement_model(placement: layout_engine.Placement) -> VariantLayerPlacement:
    layer = placement.layer
    return VariantLayerPlacement(
        layer_id=layer.id,
        name=layer.name,
        category=layer.category,
        type=layer.type,
        x=placement.x,
        y=placement.y,
        width=placement.width,
        height=placement.height,
        z_index=placement.z_index,
        visible=layer.visible,
        locked=layer.locked,
        font_size=placement.font_size,
        color=placement.color,
        text_align=placement.align,
        content=layer.content,
        valign=placement.valign,
        pinned=placement.pinned,
        stretch=placement.stretch,
        rotation=layer.rotation,
        font_family=layer.font_family,
        font_weight=layer.font_weight,
        line_height=layer.line_height,
        export_as_text=layer.export_as_text,
        text_verified=layer.text_verified,
    )


def generate_variants(project: Project, request) -> tuple[list[Variant], list[str]]:
    """Genera todas las variantes solicitadas. Determinista para una misma semilla."""
    plans, warnings = layout_engine.plan_variants(project, request)
    if not plans:
        return [], warnings

    if getattr(request, "replace_existing", True):
        storage.clear_dir(project.project_id, "variants")
        project.variants = []

    engine = predictor.get_predictor()
    variants: list[Variant] = []
    for plan in plans:
        variant_id = new_id()
        image, render_warnings = renderer.render_variant(project, plan)
        report = quality.evaluate_variant(project, plan, image, extra_warnings=render_warnings)
        rel_png, rel_thumb = renderer.save_variant_image(project, variant_id, image)
        prediction: dict = {}
        try:
            prediction = engine.predict(project, plan, image, report)
        except Exception as exc:  # noqa: BLE001 - el predictor no debe romper el flujo
            logger.warning("El predictor falló: %s", exc)

        variant = Variant(
                id=variant_id,
                index=plan.index,
                layout=plan.layout,
                layout_label=plan.layout_label,
                format=plan.format,
                width=plan.width,
                height=plan.height,
                seed=plan.seed,
                intensity=plan.intensity,
                background_style=plan.background_style,
                image=rel_png,
                thumbnail=rel_thumb,
                quality=report,
                prediction=prediction,
                placements=[_to_placement_model(p) for p in plan.placements],
                meta={
                    "format": format_spec(plan.format),
                    "product_arrangement": getattr(request, "product_arrangement", "auto"),
                    # Trazabilidad exacta del producto o combo usado en esta
                    # salida. Los assets de reemplazo son inmutables, por lo que
                    # esta lista también permite auditar que no hubo cruces.
                    "product_assets": [
                        {
                            "layer_id": placement.layer.id,
                            "src": placement.layer.src,
                            "source_name": placement.layer.meta.get("replaced_from"),
                            "group_id": placement.layer.meta.get("product_group_id"),
                        }
                        for placement in plan.placements
                        if placement.layer.category.value == "product"
                    ],
                    **(
                        {"product_label": request.product_label}
                        if getattr(request, "product_label", None)
                        else {}
                    ),
                },
            )
        # Congela las capas ahora, antes de que la siguiente tanda sustituya los
        # productos activos del proyecto.
        psd_path = export_service.build_psd(project, variant)
        variant.meta["psd"] = str(psd_path.relative_to(storage.project_dir(project.project_id)))
        svg_path = export_service.build_svg(project, variant)
        variant.meta["svg"] = str(svg_path.relative_to(storage.project_dir(project.project_id)))
        variants.append(variant)
        image.close()

    project.variants.extend(variants)
    return variants, warnings
