"""Exportación: ZIP con las variantes seleccionadas y su manifiesto."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from PIL import Image
from psd_tools import PSDImage

from ..models import Project
from . import renderer, storage
from .layout_engine import Placement, VariantPlan
from .security import slugify


def build_psd(project: Project, variant) -> Path:
    """Crea un PSD válido con una capa raster por elemento de la variante."""
    placements: list[Placement] = []
    for saved in variant.placements:
        original = project.layer_by_id(saved.layer_id)
        if original is None:
            continue
        layer = original.model_copy(deep=True)
        if saved.content is not None:
            layer.content = saved.content
        placements.append(
            Placement(
                layer=layer,
                x=saved.x,
                y=saved.y,
                width=saved.width,
                height=saved.height,
                z_index=saved.z_index,
                align=saved.text_align or "left",
                font_size=saved.font_size,
                color=saved.color,
            )
        )
    plan = VariantPlan(
        index=variant.index,
        layout=variant.layout,
        layout_label=variant.layout_label,
        format=variant.format,
        width=variant.width,
        height=variant.height,
        seed=variant.seed,
        intensity=variant.intensity,
        background_style=variant.background_style,
        placements=placements,
    )
    document = PSDImage.new("RGB", (variant.width, variant.height), color=(0, 0, 0))
    background = renderer.build_background(project, plan).convert("RGBA")
    document.create_pixel_layer(background, name="00 · Fondo", top=0, left=0)
    background.close()

    for index, placement in enumerate(sorted(placements, key=lambda item: item.z_index), 1):
        surface = Image.new("RGBA", (variant.width, variant.height), (0, 0, 0, 0))
        if placement.layer.is_text:
            renderer.draw_text_layer(surface, placement, project)
        else:
            renderer.draw_image_layer(surface, placement, project)
        bbox = surface.getchannel("A").getbbox()
        if bbox:
            crop = surface.crop(bbox)
            document.create_pixel_layer(
                crop,
                name=f"{index:02d} · {placement.layer.name}"[:255],
                top=bbox[1],
                left=bbox[0],
            )
            crop.close()
        surface.close()

    product = slugify(str(variant.meta.get("product_label") or "producto"), "producto")
    rel = f"exports/{product}_{variant.index:02d}_{variant.format}_{variant.id[:8]}.psd"
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(target)
    return target


def build_zip(
    project: Project,
    variant_ids: list[str] | None = None,
    include_layers: bool = False,
) -> Path:
    """Crea (o sobrescribe) el ZIP de exportación y devuelve su ruta absoluta."""
    selected = [
        variant
        for variant in project.variants
        if variant_ids is None or variant.id in set(variant_ids)
    ]
    if not selected:
        raise ValueError("No hay variantes para exportar.")

    slug = slugify(project.name, fallback="proyecto")
    rel = f"exports/{slug}_variantes.zip"
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "project_id": project.project_id,
        "project_name": project.name,
        "generated_at": project.updated_at,
        "canvas": project.canvas.model_dump(),
        "background_provider": project.background.provider,
        "variants": [],
    }

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for variant in selected:
            image_path = storage.abs_path(project.project_id, variant.image)
            if not image_path.exists():
                continue
            product = slugify(
                str(variant.meta.get("product_label") or "producto"), fallback="producto"
            )
            arcname = (
                f"{product}/{variant.format}/"
                f"{variant.index:02d}_{variant.layout}_{variant.id[:8]}.png"
            )
            archive.write(image_path, arcname)
            saved_psd = variant.meta.get("psd")
            psd_path = (
                storage.abs_path(project.project_id, saved_psd)
                if saved_psd
                else build_psd(project, variant)
            )
            psd_arcname = str(Path(arcname).with_suffix(".psd"))
            archive.write(psd_path, psd_arcname)
            manifest["variants"].append(
                {
                    "id": variant.id,
                    "file": arcname,
                    "psd": psd_arcname,
                    "layout": variant.layout,
                    "layout_label": variant.layout_label,
                    "format": variant.format,
                    "seed": variant.seed,
                    "intensity": variant.intensity,
                    "background_style": variant.background_style,
                    "score": variant.quality.score,
                    "warnings": variant.quality.warnings,
                    "product_label": variant.meta.get("product_label"),
                }
            )

        if include_layers:
            for layer in project.layers:
                if not layer.src:
                    continue
                layer_path = storage.abs_path(project.project_id, layer.src)
                if layer_path.exists():
                    archive.write(layer_path, f"capas/{Path(layer.src).name}")

        archive.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        archive.writestr(
            "LEEME.txt",
            (
                "Variantes generadas con Creative Variants MVP.\n"
                "Las capas bloqueadas conservan sus píxeles originales y su relación "
                "de aspecto.\nEl fondo es una reconstrucción aproximada del arte "
                "original.\n"
            ),
        )
    return target
