"""Exportación: ZIP con las variantes seleccionadas y su manifiesto."""
from __future__ import annotations

import base64
import io
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw
from psd_tools import PSDImage

from ..models import LayerCategory, LayerType, Project
from . import renderer, storage
from .layout_engine import Placement, VariantPlan
from .security import slugify


#: Codificación del campo heredado de nombre de capa en el formato PSD.
PSD_NAME_ENCODING = "macroman"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def psd_layer_name(raw: str) -> str:
    """Nombre de capa que psd-tools pueda escribir sin reventar.

    Photoshop entrega los acentos **descompuestos** —"Promocio" más un acento
    combinante— y el campo heredado del PSD se escribe en MacRoman, que no sabe
    representar combinantes: guardar el PSD moría con UnicodeEncodeError y se
    perdía la tanda entera. Se recomponen (NFC), que además es lo que MacRoman sí
    tiene; lo que aun así no quepa se translitera, porque exportar con el nombre
    sin tilde es mejor que no exportar.
    """
    name = unicodedata.normalize("NFC", raw or "")
    try:
        name.encode(PSD_NAME_ENCODING)
    except UnicodeEncodeError:
        name = (
            unicodedata.normalize("NFKD", name)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
    return name.strip() or "capa"


def _placements_for_export(project: Project, variant) -> list[Placement]:
    """Reconstruye el snapshot exacto guardado al renderizar una variante."""
    placements: list[Placement] = []
    for saved in variant.placements:
        original = project.layer_by_id(saved.layer_id)
        if original is None:
            continue
        layer = original.model_copy(deep=True)
        if saved.content is not None:
            layer.content = saved.content
        if saved.color:
            layer.color = saved.color
            # El color ya fue resuelto sobre el fondo durante el render original.
            layer.auto_contrast = False
        if saved.font_family:
            layer.font_family = saved.font_family
        if saved.font_weight in {"normal", "bold"}:
            layer.font_weight = saved.font_weight
        if saved.line_height:
            layer.line_height = saved.line_height
        layer.rotation = saved.rotation
        layer.export_as_text = saved.export_as_text
        layer.text_verified = saved.text_verified
        placements.append(
            Placement(
                layer=layer,
                x=saved.x,
                y=saved.y,
                width=saved.width,
                height=saved.height,
                z_index=saved.z_index,
                align=saved.text_align or "left",
                valign=saved.valign,
                pinned=saved.pinned,
                stretch=saved.stretch,
                font_size=saved.font_size,
                color=saved.color,
            )
        )
    return placements


def _variant_plan(variant, placements: list[Placement]) -> VariantPlan:
    return VariantPlan(
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


def build_psd(project: Project, variant) -> Path:
    """Crea un PSD válido con una capa raster por elemento de la variante."""
    placements = _placements_for_export(project, variant)
    plan = _variant_plan(variant, placements)
    document = PSDImage.new("RGB", (variant.width, variant.height), color=(0, 0, 0))
    background = renderer.build_background(project, plan).convert("RGBA")
    document.create_pixel_layer(
        background, name=psd_layer_name("00 · Fondo"), top=0, left=0
    )
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
                name=psd_layer_name(f"{index:02d} · {placement.layer.name}")[:255],
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


def _svg_id(raw: str, fallback: str = "capa") -> str:
    normalized = unicodedata.normalize("NFKD", raw or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", ascii_only).strip("-")
    return cleaned or fallback


def _png_data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _svg_image(
    parent,
    image: Image.Image,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    element_id: str,
    hidden: bool = False,
) -> None:
    attrs = {
        "id": _svg_id(element_id),
        "x": str(x),
        "y": str(y),
        "width": str(width),
        "height": str(height),
        "preserveAspectRatio": "none",
        f"{{{XLINK_NS}}}href": _png_data_uri(image),
    }
    if hidden:
        attrs["style"] = "display:none"
    ET.SubElement(parent, f"{{{SVG_NS}}}image", attrs)


def _raster_surface(
    project: Project,
    placement: Placement,
    canvas_size: tuple[int, int],
) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    """Superficie transparente de una capa, usando el mismo renderer del PNG."""
    surface = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    if placement.layer.type == LayerType.TEXT:
        renderer.draw_text_layer(surface, placement, project)
    else:
        renderer.draw_image_layer(surface, placement, project)
    bbox = surface.getchannel("A").getbbox()
    if not bbox:
        surface.close()
        return None
    crop = surface.crop(bbox)
    surface.close()
    return crop, bbox


def _append_editable_text(parent, project: Project, placement: Placement) -> None:
    """Legal como texto SVG real, con el mismo wrap/autofit que el renderer."""
    layer = placement.layer
    dummy = Image.new("RGB", (max(1, placement.width), max(1, placement.height)), "white")
    draw = ImageDraw.Draw(dummy)
    contenido = (layer.content or "").strip()
    font_path = renderer.resolve_font_path(project, layer.font_weight)
    start_size = placement.font_size or layer.font_size
    if layer.meta.get("art_text"):
        # El copy del arte ya viene medido y su caja es el bloque exacto. Volver
        # a ajustarlo aquí lo partiría en líneas que el PNG no tiene, y el SVG
        # dejaría de coincidir con la imagen que acompaña.
        font = renderer.load_font(font_path, start_size)
        lines = contenido.split("\n")
        _block_w, block_h = renderer._text_block_size(lines, font, draw, layer.line_height)
    else:
        font, lines, (_block_w, block_h) = renderer.fit_text(
            draw,
            contenido,
            font_path,
            placement.width,
            placement.height,
            start_size,
            layer.line_height,
            max_lines=20,
        )
    font_size = int(getattr(font, "size", placement.font_size or layer.font_size))
    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font_size, 0)
    line_px = int((ascent + descent) * layer.line_height)
    if placement.valign == "top":
        block_y = placement.y
    elif placement.valign == "bottom":
        block_y = placement.y + max(0, placement.height - block_h)
    else:
        block_y = placement.y + max(0, (placement.height - block_h) // 2)

    text = ET.SubElement(
        parent,
        f"{{{SVG_NS}}}text",
        {
            "id": _svg_id(f"texto-{layer.name}"),
            "font-family": layer.font_family or "sans-serif",
            "font-size": str(font_size),
            "font-weight": layer.font_weight,
            "fill": placement.color or layer.color,
            "data-editable": "true",
            "data-text-verified": str(bool(layer.text_verified)).lower(),
        },
    )
    for index, line in enumerate(lines):
        line_w = draw.textlength(line, font=font)
        if placement.align == "center":
            line_x = placement.x + (placement.width - line_w) / 2
        elif placement.align == "right":
            line_x = placement.x + placement.width - line_w
        else:
            line_x = placement.x
        tspan = ET.SubElement(
            text,
            f"{{{SVG_NS}}}tspan",
            {"x": f"{line_x:.2f}", "y": str(block_y + ascent + index * line_px)},
        )
        tspan.text = line
    dummy.close()


def _append_svg_layer(
    parent,
    project: Project,
    placement: Placement,
    canvas_size: tuple[int, int],
) -> None:
    layer = placement.layer
    # Un copy reescrito en «Textos y logos del arte» se conoce exacto: contenido,
    # tipografía, cuerpo, color y sitio los puso el sistema. Eso es justo lo que
    # el diseñador quiere retocar en Illustrator, así que sale como texto y no
    # como píxeles. El resto del copy sigue rasterizado, porque de un objeto
    # inteligente de Photoshop no se sabe con qué estaba escrito.
    editable_text = (
        bool((layer.content or "").strip())
        and layer.text_verified
        and (layer.type == LayerType.TEXT or layer.export_as_text)
        and (
            layer.category == LayerCategory.LEGAL
            or bool(layer.meta.get("art_text"))
        )
    )
    if editable_text:
        # Referencia exacta oculta para poder comparar/recuperar el arte original.
        if layer.src:
            reference = placement.layer.model_copy(deep=True)
            reference.type = LayerType.IMAGE
            ref_placement = Placement(
                layer=reference,
                x=placement.x,
                y=placement.y,
                width=placement.width,
                height=placement.height,
                z_index=placement.z_index,
                align=placement.align,
                valign=placement.valign,
                pinned=placement.pinned,
                stretch=placement.stretch,
            )
            rendered = _raster_surface(project, ref_placement, canvas_size)
            if rendered:
                crop, bbox = rendered
                _svg_image(
                    parent, crop, x=bbox[0], y=bbox[1], width=crop.width,
                    height=crop.height, element_id=f"referencia-{layer.name}", hidden=True,
                )
                crop.close()
        _append_editable_text(parent, project, placement)
        return

    rendered = _raster_surface(project, placement, canvas_size)
    if not rendered:
        return
    crop, bbox = rendered
    _svg_image(
        parent,
        crop,
        x=bbox[0],
        y=bbox[1],
        width=crop.width,
        height=crop.height,
        element_id=layer.name,
    )
    crop.close()


def build_svg(project: Project, variant) -> Path:
    """SVG autocontenido, editable en Illustrator y sin enlaces externos."""
    placements = _placements_for_export(project, variant)
    plan = _variant_plan(variant, placements)
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": str(variant.width),
            "height": str(variant.height),
            "viewBox": f"0 0 {variant.width} {variant.height}",
            "version": "1.1",
        },
    )
    title = ET.SubElement(root, f"{{{SVG_NS}}}title")
    title.text = f"{project.name} · {variant.format} · editable para Illustrator"

    background_group = ET.SubElement(
        root, f"{{{SVG_NS}}}g", {"id": "00-fondo", "data-name": "00 · Fondo"}
    )
    background = renderer.build_background(project, plan).convert("RGBA")
    _svg_image(
        background_group, background, x=0, y=0, width=variant.width,
        height=variant.height, element_id="fondo",
    )
    background.close()

    ordered = sorted(placements, key=lambda item: item.z_index)
    consumed: set[str] = set()
    for index, placement in enumerate(ordered, 1):
        if placement.layer.id in consumed:
            continue
        group_id = str(placement.layer.meta.get("product_group_id") or "")
        members = (
            [item for item in ordered if item.layer.meta.get("product_group_id") == group_id]
            if group_id
            else [placement]
        )
        consumed.update(item.layer.id for item in members)
        group_name = str(
            placement.layer.meta.get("product_group_name")
            or ("Productos juntos" if group_id else placement.layer.name)
        )
        attrs = {
            "id": _svg_id(f"{index:02d}-{group_name}"),
            "data-name": f"{index:02d} · {group_name}",
            "data-category": placement.layer.category.value,
        }
        if group_id:
            attrs["data-group-id"] = group_id
            attrs["data-arrangement"] = str(
                placement.layer.meta.get("product_arrangement") or "auto"
            )
        group = ET.SubElement(root, f"{{{SVG_NS}}}g", attrs)
        for member in members:
            _append_svg_layer(
                group, project, member, (variant.width, variant.height)
            )

    product = slugify(str(variant.meta.get("product_label") or "producto"), "producto")
    rel = f"exports/{product}_{variant.index:02d}_{variant.format}_{variant.id[:8]}.svg"
    target = storage.abs_path(project.project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
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
            saved_svg = variant.meta.get("svg")
            svg_path = (
                storage.abs_path(project.project_id, saved_svg)
                if saved_svg
                else build_svg(project, variant)
            )
            svg_arcname = str(Path(arcname).with_suffix(".svg"))
            archive.write(svg_path, svg_arcname)
            editable_texts = [
                placement.name
                for placement in variant.placements
                if bool((placement.content or "").strip())
                and placement.text_verified
                and (placement.type == LayerType.TEXT or placement.export_as_text)
            ]
            editable_legals = [
                placement.name
                for placement in variant.placements
                if placement.category == LayerCategory.LEGAL
                and placement.name in set(editable_texts)
            ]
            manifest["variants"].append(
                {
                    "id": variant.id,
                    "file": arcname,
                    "psd": psd_arcname,
                    "svg_illustrator": svg_arcname,
                    # Se conserva la clave anterior: hay ZIP ya entregados que la leen.
                    "editable_legal_layers": editable_legals,
                    "editable_text_layers": editable_texts,
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

        if project.references.font:
            font_path = storage.abs_path(project.project_id, project.references.font)
            if font_path.exists():
                archive.write(font_path, f"fuentes/{font_path.name}")

        archive.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        archive.writestr(
            "LEEME.txt",
            (
                "Variantes generadas con Creative Variants MVP.\n"
                "Cada pieza incluye PNG final, PSD por capas y SVG editable.\n"
                "Para Illustrator: abra el SVG y guárdelo como .ai. El grupo de "
                "legales contiene texto editable cuando el PSD aportó una capa de "
                "texto o el usuario lo confirmó; revise tipografía y saltos antes "
                "de publicar. La referencia raster original del legal queda oculta "
                "dentro del mismo grupo.\n"
                "Las capas bloqueadas conservan sus píxeles originales y su relación "
                "de aspecto. El fondo puede ser una reconstrucción aproximada del "
                "arte original.\n"
            ),
        )
    return target
