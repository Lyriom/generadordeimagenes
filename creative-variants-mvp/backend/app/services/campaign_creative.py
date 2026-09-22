"""Plantillas visuales y produccion estatica de una campana.

El analisis decide *que* sistemas hacen falta. Este modulo los vuelve visibles y
los rellena desde la matriz sin gastar una generacion de imagen por pieza. La IA
se usa, como maximo, una vez por tanda para completar copy faltante; composicion,
formatos, producto y exportables son deterministas y reproducibles.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import shutil
import unicodedata
import warnings
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Iterable

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from psd_tools import PSDImage

from ..config import settings
from ..models.campaign import Campaign, CampaignBrief, TemplateCandidate
from ..models.campaign_production import ProductionBatch, ProductionPiece
from ..models.formats import DEFAULT_SAFE_AREA, FORMAT_PRESETS, SUPPORTED_FORMATS
from . import campaign_store, client_fonts, template_store
from .production_matrix import MatrixRow, ai_fillable_fields, product_count, select_template
from .security import slugify


class CampaignProductionError(ValueError):
    pass


def _representative_paths(paths: list[str], limit: int) -> list[str]:
    """Evita que un documento largo aporte siempre solo sus primeras paginas."""

    if limit <= 0 or not paths:
        return []
    if len(paths) <= limit:
        return list(paths)
    if limit == 1:
        return [paths[0]]
    indices = {
        round(position * (len(paths) - 1) / (limit - 1))
        for position in range(limit)
    }
    return [paths[index] for index in sorted(indices)]


FORMAT_ALIASES = {
    "feed": "meta_feed_4_5",
    "feed_vertical": "meta_feed_4_5",
    "instagram_feed": "meta_instagram_feed_3_4",
    "post": "meta_feed_4_5",
    "post_vertical": "meta_feed_4_5",
    "cuadrado": "meta_feed_square",
    "square": "meta_feed_square",
    "story": "meta_stories",
    "stories": "meta_stories",
    "historia": "meta_stories",
    "historias": "meta_stories",
    "reel": "meta_reels",
    "reels": "meta_reels",
    "horizontal": "meta_feed_landscape",
    "landscape": "meta_feed_landscape",
}


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def resolve_format(value: str) -> tuple[str, int, int, dict[str, float]]:
    raw = (value or "").strip()
    key = raw if raw in SUPPORTED_FORMATS else FORMAT_ALIASES.get(_normalise(raw), "")
    if key:
        width, height = SUPPORTED_FORMATS[key]
        if width * height > settings.campaign_max_output_pixels:
            raise CampaignProductionError(
                f"El formato {width}x{height} excede el limite de salida de campaña."
            )
        safe = FORMAT_PRESETS.get(key, {}).get("safe_area", DEFAULT_SAFE_AREA)
        return key, width, height, {name: float(amount) for name, amount in safe.items()}
    match = re.fullmatch(r"\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*", raw)
    if not match:
        raise CampaignProductionError(
            f"Formato '{raw}' desconocido. Usa un preset, feed/story o una medida como 1080x1350."
        )
    width, height = int(match.group(1)), int(match.group(2))
    if min(width, height) < 100 or max(width, height) > 8000:
        raise CampaignProductionError(f"El formato {width}x{height} queda fuera de 100–8000 px.")
    if width * height > settings.campaign_max_output_pixels:
        raise CampaignProductionError(f"El formato {width}x{height} excede el limite de pixeles.")
    return f"{width}x{height}", width, height, dict(DEFAULT_SAFE_AREA)


def _colour(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    try:
        if len(raw) == 6:
            return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        pass
    return fallback


def _palette(brief: CampaignBrief | None) -> list[tuple[int, int, int]]:
    defaults = [(28, 25, 78), (77, 63, 205), (192, 56, 164), (255, 210, 67)]
    colours = [_colour(value, defaults[0]) for value in (brief.palette if brief else [])]
    for item in defaults:
        if item not in colours:
            colours.append(item)
    return colours[:6]


def _catalogue_font_path(campaign: Campaign, *, bold: bool) -> str | None:
    """Busca la fuente permanente del cliente antes de caer a una genérica."""

    try:
        brand = template_store.load_brand(campaign.client_id)
        catalogues = client_fonts.catalog()
        catalogue_id = brand.fonts.client_id or brand.slug
        catalogue = next((item for item in catalogues if item["id"] == catalogue_id), None)
        if catalogue is None:
            return None
        wanted = brand.fonts.bold if bold else brand.fonts.regular
        if wanted:
            return str(client_fonts.font_path(catalogue_id, wanted))
        fonts = list(catalogue.get("fonts", []))
        # El catálogo de Marcimex, por ejemplo, ya vive en el servidor. Si no
        # se eligió una cara explícita, seleccionamos una cara sensata de esa
        # familia; no volvemos a DejaVu por accidente.
        preferred = (
            ("black", "bold", "semibold", "heavy", "extrabold")
            if bold else ("regular", "medium", "book", "normal")
        )
        chosen = next(
            (
                item for item in fonts
                if any(token in str(item.get("name", "")).casefold() for token in preferred)
                and "italic" not in str(item.get("name", "")).casefold()
            ),
            fonts[0] if fonts else None,
        )
        if chosen and chosen.get("id"):
            return str(client_fonts.font_path(catalogue_id, str(chosen["id"])))
    except Exception:  # noqa: BLE001 - una fuente opcional nunca bloquea un arte
        return None
    return None


def _font_path(campaign: Campaign, *, bold: bool = False) -> str:
    font_candidates: list[tuple[str, Path]] = []
    for source in campaign.sources:
        if source.kind.value == "font":
            try:
                font_candidates.append((source.filename, campaign_store.campaign_path(
                    campaign.client_id, campaign.campaign_id, source.stored_path
                )))
            except Exception:  # noqa: BLE001
                continue
        for asset in source.meta.get("font_assets", []):
            if not isinstance(asset, dict) or not isinstance(asset.get("path"), str):
                continue
            try:
                font_candidates.append((str(asset.get("name") or asset["path"]), campaign_store.campaign_path(
                    campaign.client_id, campaign.campaign_id, asset["path"]
                )))
            except Exception:  # noqa: BLE001
                continue
    if font_candidates:
        preference = ("bold", "black", "semi", "heavy") if bold else ("regular", "medium", "book", "light")
        ranked = sorted(
            font_candidates,
            key=lambda item: not any(token in item[0].casefold() for token in preference),
        )
        for _name, path in ranked:
            if path.is_file():
                return str(path)
    catalogue_font = _catalogue_font_path(campaign, bold=bold)
    if catalogue_font:
        return catalogue_font
    configured = settings.default_font_bold if bold else settings.default_font
    if configured and Path(configured).exists():
        return configured
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        if Path(path).exists():
            return path
    return "DejaVuSans.ttf"


def _gradient(width: int, height: int, colours: list[tuple[int, int, int]], seed: int) -> Image.Image:
    first = np.array(colours[seed % len(colours)], dtype=np.float32)
    second = np.array(colours[(seed + 1) % len(colours)], dtype=np.float32)
    yy, xx = np.mgrid[0:height, 0:width]
    diagonal = np.clip((xx / max(1, width - 1)) * .62 + (yy / max(1, height - 1)) * .38, 0, 1)
    array = first[None, None, :] * (1 - diagonal[:, :, None]) + second[None, None, :] * diagonal[:, :, None]
    # Un foco suave evita el degradado generico sin copiar ningun arte fuente.
    cx = width * (.25 + .5 * ((seed * 37) % 100) / 100)
    cy = height * (.2 + .6 * ((seed * 61) % 100) / 100)
    distance = np.sqrt(((xx - cx) / max(width, 1)) ** 2 + ((yy - cy) / max(height, 1)) ** 2)
    glow = np.clip(1 - distance * 2.2, 0, 1)[:, :, None] * 24
    array = np.clip(array + glow, 0, 255).astype(np.uint8)
    return Image.fromarray(array, "RGB").convert("RGBA")


def _reference_texture(
    campaign: Campaign,
    candidate: TemplateCandidate,
    width: int,
    height: int,
    seed: int,
) -> Image.Image | None:
    """Traslada la atmosfera de los artes fuente sin hornear su copy/producto.

    La referencia se reduce y desenfoca de forma deliberada: conserva masas,
    contraste y ritmo cromatico, pero no deja reutilizable un precio, titular o
    producto que estuviera en el KV original.
    """

    preferred = set(candidate.source_ids)
    sources = sorted(
        campaign.sources,
        key=lambda source: (source.source_id not in preferred, not source.preview_files),
    )
    paths: list[Path] = []
    for source in sources:
        role_names = {role.value for role in source.roles}
        if not role_names.intersection(
            {"background", "key_visual", "visual_reference", "final_art"}
        ):
            continue
        for relative in _representative_paths(source.preview_files, 4):
            try:
                path = campaign_store.campaign_path(
                    campaign.client_id, campaign.campaign_id, relative
                )
            except Exception:  # noqa: BLE001
                continue
            if path.exists():
                paths.append(path)
    if not paths:
        return None
    path = paths[seed % len(paths)]
    try:
        with Image.open(path) as image:
            fitted = ImageOps.fit(
                ImageOps.exif_transpose(image).convert("RGB"),
                (width, height),
                method=Image.Resampling.LANCZOS,
            )
        # Pixelar antes de desenfocar borra copy y objetos concretos incluso
        # cuando la referencia era una pieza pequeña.
        tiny = fitted.resize(
            (max(36, width // 24), max(36, height // 24)),
            Image.Resampling.BILINEAR,
        )
        texture = tiny.resize((width, height), Image.Resampling.BICUBIC)
        texture = texture.filter(ImageFilter.GaussianBlur(max(8, min(width, height) // 34)))
        texture = ImageEnhance.Color(texture).enhance(.72)
        texture = ImageEnhance.Contrast(texture).enhance(.88).convert("RGBA")
        texture.putalpha(72)
        fitted.close()
        tiny.close()
        return texture
    except Exception:  # noqa: BLE001 - una referencia rota no bloquea produccion
        return None


def _fixed_brand_background(
    campaign: Campaign,
    canvas: tuple[int, int],
) -> tuple[str, Image.Image] | None:
    """Compone un fondo que la persona marcó explícitamente como fijo.

    Un key visual o post entero se usa solo como textura deliberadamente
    difusa: contiene producto y copy que no se deben reciclar. En cambio, un
    PNG/JPG limpio marcado como ``background`` es una decisión humana de
    branding y debe llegar nítido a todas las adaptaciones.
    """

    for source in campaign.sources:
        if source.kind.value != "image" or "background" not in {
            role.value for role in source.roles
        }:
            continue
        try:
            path = campaign_store.campaign_path(
                campaign.client_id, campaign.campaign_id, source.stored_path
            )
        except Exception:  # noqa: BLE001 - manifiesto antiguo o dañado
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            with Image.open(path) as probe:
                probe.verify()
            with Image.open(path) as image:
                layer = ImageOps.fit(
                    ImageOps.exif_transpose(image).convert("RGBA"),
                    canvas,
                    method=Image.Resampling.LANCZOS,
                ).copy()
            return f"Fondo fijo · {source.filename}", layer
        except Exception:  # noqa: BLE001 - no se cae una tanda por un asset roto
            continue
    return None


def _decorations(
    width: int,
    height: int,
    colours: list[tuple[int, int, int]],
    seed: int,
    style: str = "frame",
) -> Image.Image:
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    scale = min(width, height)
    accent = colours[(seed + 2) % len(colours)]
    if style == "orbs":
        for index in range(3):
            radius = int(scale * (.11 + index * .08))
            x = int(width * (.78 + .08 * math.sin(seed + index)))
            y = int(height * (.10 + .29 * index))
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=(*accent, 22 + index * 7),
            )
    elif style == "diagonal":
        draw.polygon(
            [
                (int(width * .56), 0), (width, 0),
                (width, int(height * .78)), (int(width * .34), height),
            ],
            fill=(*accent, 38),
        )
        draw.line(
            (int(width * .42), 0, int(width * .18), height),
            fill=(255, 255, 255, 32),
            width=max(2, int(scale * .006)),
        )
    elif style == "cards":
        margin = int(scale * .035)
        draw.rounded_rectangle(
            (margin, int(height * .42), int(width * .55), int(height * .88)),
            radius=max(14, int(scale * .035)),
            fill=(8, 8, 24, 38),
            outline=(255, 255, 255, 28),
            width=max(2, int(scale * .003)),
        )
    if style in {"frame", "orbs", "cards"}:
        draw.rounded_rectangle(
            (int(width * .025), int(height * .025), int(width * .975), int(height * .975)),
            radius=max(12, int(scale * .025)),
            outline=(255, 255, 255, 35),
            width=max(2, int(scale * .003)),
        )
    return layer


def _background(
    width: int,
    height: int,
    colours: list[tuple[int, int, int]],
    seed: int,
    style: str,
) -> Image.Image:
    if style == "solid":
        return Image.new("RGBA", (width, height), (*colours[seed % len(colours)], 255))
    if style == "light":
        light = [
            tuple(min(255, round(channel * .22 + 255 * .78)) for channel in colour)
            for colour in colours
        ]
        return _gradient(width, height, light, seed)
    return _gradient(width, height, colours, seed)


def _box(width: int, height: int, values: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    x, y, w, h = values
    return int(x * width), int(y * height), max(1, int(w * width)), max(1, int(h * height))


def _layout(
    candidate: TemplateCandidate,
    width: int,
    height: int,
    safe: dict[str, float],
    proposal: int,
    visible_fields: set[str] | None = None,
) -> dict[str, tuple[int, int, int, int]]:
    left, top = safe.get("left", .04), safe.get("top", .04)
    right, bottom = safe.get("right", .04), safe.get("bottom", .04)
    usable_w, usable_h = 1 - left - right, 1 - top - bottom
    portrait = height / width > 1.2
    landscape = width / height > 1.35
    blueprint = candidate.blueprint
    mirror = blueprint.mirror_variants and proposal % 2 == 0
    archetype = blueprint.archetype
    if (
        not candidate.revision_hash
        and candidate.category != "single_product"
        and archetype == "hero_center"
        and not blueprint.placements
    ):
        archetype = {
            "price_promotion": "price_focus",
            "combo": "product_grid",
            "product_benefit": "split_right",
            "institutional": "editorial",
        }.get(candidate.category, archetype)
    layout_category = {
        "hero_center": "single_product",
        "price_focus": "price_promotion",
        "product_grid": "combo",
        "split_left": "product_benefit",
        "split_right": "product_benefit",
        "editorial": "institutional",
    }.get(archetype, candidate.category)

    # Coordenadas relativas al area segura. Cada familia tiene una reticula
    # distinta; antes todas las propuestas terminaban siendo el mismo layout
    # con otro nombre.
    if portrait:
        regions = {
            "logo": (0, 0, .28, .065),
            "headline": (0, .09, 1, .14),
            "subheadline": (0, .235, .80, .065),
            "product": (.04, .30, .92, .43),
            "product_name": (0, .74, .58, .055),
            "price": (0, .79, .60, .105),
            "previous_price": (.62, .80, .30, .045),
            "installment": (.62, .85, .30, .045),
            "discount": (.70, .66, .25, .07),
            "cta": (0, .90, .38, .06),
            "validity": (.42, .91, .52, .04),
            "legal": (0, .965, 1, .032),
        }
        if layout_category == "price_promotion":
            regions.update({
                "product": (.17, .27, .78, .37),
                "product_name": (0, .655, .55, .045),
                "price": (0, .71, .56, .12),
                "previous_price": (.60, .72, .32, .04),
                "installment": (.60, .78, .32, .045),
                "discount": (.70, .57, .26, .075),
            })
        elif layout_category == "combo":
            regions.update({
                "product": (.01, .27, .98, .42),
                "product_name": (0, .71, .60, .05),
                "price": (0, .77, .56, .10),
                "previous_price": (.60, .78, .32, .04),
                "installment": (.60, .83, .32, .04),
                "discount": (.73, .63, .24, .07),
                "cta": (0, .89, .38, .055),
            })
        elif layout_category == "product_benefit":
            # Editorial partido: argumento a la izquierda y producto alto a la
            # derecha. No es la misma tarjeta apilada del producto simple.
            regions.update({
                "headline": (0, .15, .43, .20),
                "subheadline": (0, .37, .42, .12),
                "product": (.47, .17, .52, .59),
                "product_name": (0, .54, .41, .075),
                "cta": (0, .65, .35, .065),
            })
        elif layout_category == "institutional":
            # Composición tipo portada: mensaje ancho, producto centrado y una
            # franja inferior limpia para nombre/CTA/legal.
            regions.update({
                "headline": (.08, .13, .84, .17),
                "subheadline": (.18, .31, .64, .09),
                "product": (.13, .43, .74, .34),
                "product_name": (.18, .79, .64, .055),
                "cta": (.31, .87, .38, .06),
            })
    elif landscape:
        product_x = .48 if not mirror else 0
        copy_x = 0 if not mirror else .57
        regions = {
            "logo": (copy_x, 0, .25, .09),
            "headline": (copy_x, .14, .42, .22),
            "subheadline": (copy_x, .38, .40, .10),
            "product": (product_x, .08, .52, .77),
            "product_name": (copy_x, .50, .40, .07),
            "price": (copy_x, .58, .32, .15),
            "previous_price": (copy_x + .30, .60, .14, .06),
            "installment": (copy_x, .74, .35, .07),
            "discount": (product_x + .34, .08, .16, .12),
            "cta": (copy_x, .84, .25, .09),
            "validity": (copy_x + .27, .86, .18, .05),
            "legal": (0, .955, 1, .04),
        }
        if layout_category == "price_promotion":
            regions.update({"product": (product_x, .04, .52, .84), "product_name": (copy_x, .49, .40, .055), "price": (copy_x, .56, .39, .18), "previous_price": (copy_x + .29, .58, .14, .055), "discount": (product_x + .34, .05, .17, .14)})
        elif layout_category == "combo":
            combo_x = .40 if not mirror else 0
            combo_copy = 0 if not mirror else .62
            regions.update({"product": (combo_x, .06, .60, .82), "headline": (combo_copy, .16, .36, .20), "subheadline": (combo_copy, .38, .34, .11), "product_name": (combo_copy, .52, .35, .08)})
        elif layout_category == "product_benefit":
            benefit_product_x = .55 if not mirror else 0
            benefit_copy_x = 0 if not mirror else .53
            regions.update({
                "headline": (benefit_copy_x, .18, .43, .25),
                "subheadline": (benefit_copy_x, .47, .40, .14),
                "product": (benefit_product_x, .08, .45, .80),
                "product_name": (benefit_copy_x, .64, .40, .07),
                "cta": (benefit_copy_x, .79, .27, .09),
            })
        elif layout_category == "institutional":
            regions.update({
                "headline": (copy_x, .20, .40, .24),
                "subheadline": (copy_x, .48, .36, .13),
                "product": (product_x + .08, .18, .38, .62),
                "product_name": (copy_x, .65, .38, .07),
            })
    else:
        product_x = .38 if not mirror else 0
        copy_x = 0 if not mirror else .61
        regions = {
            "logo": (copy_x, 0, .27, .075),
            "headline": (copy_x, .11, .56 if not mirror else .39, .17),
            "subheadline": (copy_x, .285, .48 if not mirror else .38, .075),
            "product": (product_x, .29, .62, .52),
            "product_name": (copy_x, .39, .34, .07),
            "price": (copy_x, .48, .36, .13),
            "previous_price": (copy_x, .62, .25, .05),
            "installment": (copy_x, .68, .30, .06),
            "discount": (product_x + .40, .27, .20, .09),
            "cta": (copy_x, .82, .32, .075),
            "validity": (copy_x + .35 if not mirror else .18, .84, .42, .05),
            "legal": (0, .94, 1, .045),
        }
        if layout_category == "price_promotion":
            regions.update({"product": (product_x + .05, .23, .57, .56), "price": (copy_x, .46, .40, .16), "discount": (product_x + .39, .22, .21, .10)})
        elif layout_category == "combo":
            combo_x = .31 if not mirror else 0
            combo_copy = 0 if not mirror else .69
            regions.update({"product": (combo_x, .25, .69, .57), "headline": (combo_copy, .12, .29, .18), "subheadline": (combo_copy, .32, .28, .10), "product_name": (combo_copy, .45, .28, .065), "price": (combo_copy, .53, .29, .12)})
        elif layout_category == "product_benefit":
            benefit_product_x = .53 if not mirror else 0
            benefit_copy_x = 0 if not mirror else .56
            regions.update({
                "headline": (benefit_copy_x, .16, .44 if not mirror else .40, .20),
                "subheadline": (benefit_copy_x, .38, .42 if not mirror else .39, .12),
                "product": (benefit_product_x, .24, .47, .58),
                "product_name": (benefit_copy_x, .54, .41, .07),
                "cta": (benefit_copy_x, .70, .29, .075),
            })
        elif layout_category == "institutional":
            regions.update({
                "headline": (.10, .15, .80, .19),
                "subheadline": (.20, .36, .60, .10),
                "product": (.18, .48, .64, .34),
                "product_name": (.22, .83, .56, .06),
                "cta": (.34, .89, .32, .05),
            })

    aspect = "story" if height / width >= 1.62 else "portrait" if portrait else "landscape" if landscape else "square"
    explicit = blueprint.placements.get(aspect, {})
    for name, placement in explicit.items():
        if name not in regions:
            continue
        x = min(float(placement.x), .99)
        y = min(float(placement.y), .99)
        region_width = min(float(placement.width), 1 - x)
        region_height = min(float(placement.height), 1 - y)
        if region_width > 0 and region_height > 0:
            regions[name] = (x, y, region_width, region_height)

    # Una corrección humana como “producto a la izquierda” debe verse en el
    # preview aunque el modelo no haya devuelto coordenadas detalladas.
    if blueprint.archetype == "split_left" and not explicit:
        regions = {
            name: (1 - x - region_width, y, region_width, region_height)
            for name, (x, y, region_width, region_height) in regions.items()
        }

    visible = visible_fields or set(regions)
    product_x, product_y, product_w, product_h = regions["product"]
    if not ({"headline", "subheadline"} & visible):
        reclaimed = min(.13, max(0, product_y - .10))
        product_y -= reclaimed
        product_h += reclaimed
    if (
        not ({"price", "previous_price", "installment", "discount"} & visible)
        and "product_name" not in visible
        and not landscape
    ):
        product_h = min(product_h + .08, .90 - product_y)
    regions["product"] = (product_x, product_y, product_w, product_h)

    def safe_box(values: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        x, y, w, h = values
        return _box(
            width,
            height,
            (left + x * usable_w, top + y * usable_h, w * usable_w, h * usable_h),
        )

    return {name: safe_box(region) for name, region in regions.items()}


def _font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, max(8, size))
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words.pop(0)
    for word in words:
        trial = current + " " + word
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines - 1:
                break
    if len(lines) < max_lines:
        remaining = [current]
        consumed = sum(len(line.split()) for line in lines)
        rest = (text or "").split()[consumed:]
        if rest:
            remaining = [" ".join(rest)]
        lines.extend(remaining)
    return lines[:max_lines]


def _text_layer(
    canvas: tuple[int, int],
    box: tuple[int, int, int, int],
    text: str,
    font_path: str,
    *,
    colour: tuple[int, int, int, int] = (255, 255, 255, 255),
    bold: bool = False,
    max_lines: int = 2,
    align: str = "left",
    badge: bool = False,
    strike: bool = False,
) -> Image.Image:
    width, height = canvas
    x, y, w, h = box
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    if badge:
        draw.rounded_rectangle(
            (x, y, x + w, y + h), radius=max(8, int(h * .32)), fill=(255, 255, 255, 230)
        )
        colour = (35, 29, 92, 255)
        x += int(w * .08)
        w = int(w * .84)
    start = max(10, int(h * (.72 if max_lines == 1 else .53)))
    font = _font(font_path, start)
    lines = _wrap(draw, text, font, w, max_lines)
    while start > 8:
        bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font, spacing=int(start * .12))
        if bbox[2] <= w and bbox[3] <= h:
            break
        start -= max(1, int(start * .06))
        font = _font(font_path, start)
        lines = _wrap(draw, text, font, w, max_lines)
    rendered = "\n".join(lines)
    anchor = "la"
    tx = x
    if align == "center":
        anchor, tx = "ma", x + w // 2
    elif align == "right":
        anchor, tx = "ra", x + w
    draw.multiline_text((tx, y), rendered, font=font, fill=colour, spacing=int(start * .12), anchor=anchor, align=align)
    if strike:
        draw.line((x, y + h // 2, x + min(w, int(draw.textlength(text, font=font))), y + h // 2), fill=colour, width=max(2, h // 18))
    return layer


def _placeholder(canvas: tuple[int, int], box: tuple[int, int, int, int], label: str) -> Image.Image:
    width, height = canvas
    x, y, w, h = box
    layer = Image.new("RGBA", canvas, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    stroke = max(2, min(width, height) // 300)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=max(12, min(w, h) // 12), fill=(255, 255, 255, 18), outline=(255, 255, 255, 110), width=stroke)
    font = _font(settings.default_font_bold, max(11, min(w, h) // 11))
    draw.text((x + w // 2, y + h // 2), label, fill=(255, 255, 255, 170), font=font, anchor="mm")
    return layer


def _remove_simple_background(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
    if np.count_nonzero(alpha < 250) > alpha.size * .015:
        return rgba
    try:
        # El mismo recortador geométrico probado por el flujo clásico: conserva
        # huecos internos y descarta marcas de agua pequeñas del catálogo.
        from .product_alpha import flat_backdrop_alpha

        robust_alpha, _reason = flat_backdrop_alpha(rgba)
        if robust_alpha is not None:
            rgba.putalpha(Image.fromarray(robust_alpha, mode="L"))
            return rgba
    except Exception:  # noqa: BLE001 - queda el método liviano de respaldo
        pass
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.int16)
    corners = np.array([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]])
    background = np.median(corners, axis=0)
    spread = float(np.max(np.linalg.norm(corners - background, axis=1)))
    if spread > 32:
        return rgba
    distance = np.linalg.norm(rgb - background, axis=2)
    new_alpha = np.clip((distance - 12) * 7, 0, 255).astype(np.uint8)
    # Solo se quita si el borde parece realmente fondo; fotos ambientales se conservan.
    border = np.concatenate((new_alpha[0], new_alpha[-1], new_alpha[:, 0], new_alpha[:, -1]))
    if float(np.mean(border < 40)) < .55:
        return rgba
    array = np.asarray(rgba).copy()
    array[:, :, 3] = new_alpha
    return Image.fromarray(array, "RGBA")


def _fit_product(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    x, y, w, h = box
    # Las fotos de produccion ya se orientan y reducen al abrirse. Evitar otra
    # transposicion aqui es importante: una foto con EXIF puede duplicar su
    # buffer completo justo antes del recorte de fondo.
    product = _remove_simple_background(image)
    bbox = product.getchannel("A").getbbox()
    if bbox:
        product = product.crop(bbox)
    product = ImageEnhance.Contrast(product).enhance(1.04)
    product = ImageEnhance.Sharpness(product).enhance(1.08)
    scale = min(w / max(1, product.width), h / max(1, product.height))
    # Los catálogos suelen traer miniaturas. Se amplían con Lanczos hasta 4×:
    # suficiente para ocupar el slot sin prometer detalle que no existe.
    scale = min(scale, 4.0)
    target = (
        max(1, int(round(product.width * scale))),
        max(1, int(round(product.height * scale))),
    )
    if target != product.size:
        product = product.resize(target, Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", (x + w, y + h), (0, 0, 0, 0))
    px = x + (w - product.width) // 2
    py = y + (h - product.height) // 2
    shadow_alpha = product.getchannel("A").filter(ImageFilter.GaussianBlur(max(4, min(w, h) // 40)))
    shadow = Image.new("RGBA", product.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha.point(lambda value: int(value * .34)))
    layer.alpha_composite(shadow, (px + max(3, w // 80), py + max(5, h // 50)))
    layer.alpha_composite(product, (px, py))
    return layer


def _full_canvas(layer: Image.Image, canvas: tuple[int, int]) -> Image.Image:
    if layer.size == canvas:
        return layer
    target = Image.new("RGBA", canvas, (0, 0, 0, 0))
    target.alpha_composite(layer)
    return target


def _product_layers(
    canvas: tuple[int, int],
    box: tuple[int, int, int, int],
    products: list[Image.Image],
) -> list[tuple[str, Image.Image]]:
    if not products:
        return [("Producto · espacio variable", _placeholder(canvas, box, "PRODUCTO"))]
    x, y, w, h = box
    count = len(products)
    columns = count if count <= 2 else 2
    rows = math.ceil(count / columns)
    gap = max(4, min(w, h) // 50)
    cell_w = max(1, (w - gap * (columns - 1)) // columns)
    cell_h = max(1, (h - gap * (rows - 1)) // rows)
    result: list[tuple[str, Image.Image]] = []
    for index, product in enumerate(products):
        column, row = index % columns, index // columns
        cell = (x + column * (cell_w + gap), y + row * (cell_h + gap), cell_w, cell_h)
        fitted = _fit_product(product, cell)
        result.append((f"Producto {index + 1}", _full_canvas(fitted, canvas)))
    return result


def _candidate_keys(candidate: TemplateCandidate) -> set[str]:
    return {slot.key for slot in candidate.slots}


_COPY_FIELDS = frozenset({"titular", "subtitulo", "cta"})


def _values(row: MatrixRow, campaign: Campaign, candidate: TemplateCandidate) -> dict[str, str]:
    brief = campaign.brief or CampaignBrief(objective=campaign.objective)
    values = {
        "product_name": row.producto,
        "headline": row.titular or "",
        "subheadline": row.subtitulo or "",
        "price": row.precio_actual or "",
        "previous_price": row.precio_anterior or "",
        "installment": row.cuota or "",
        "discount": row.descuento or "",
        "cta": row.cta or "",
        "legal": row.legal or "",
        "validity": row.vigencia or "",
    }
    suppressed = set(row.suppressed_fields)
    alias = {"titular": "headline", "subtitulo": "subheadline", "precio_actual": "price", "precio_anterior": "previous_price", "cuota": "installment", "descuento": "discount", "vigencia": "validity"}
    suppressed = {alias.get(item, item) for item in suppressed}
    for key in suppressed:
        values[key] = ""
    allowed = _candidate_keys(candidate)
    # Solo los slots marcados para autocompletar reciben un fallback. Precio,
    # descuento, vigencia y legales nunca se inventan.
    generatable = ai_fillable_fields(candidate)
    if not values["headline"] and "titular" in generatable and "headline" not in suppressed:
        values["headline"] = brief.primary_message or brief.objective or campaign.objective
    if not values["subheadline"] and "subtitulo" in generatable and "subheadline" not in suppressed:
        values["subheadline"] = brief.creative_concept
    # Los legales sí pueden venir del brief aprobado: no se inventan ni se
    # completan con una frase genérica. La advertencia local que solo pide
    # "conservar los legales" reserva el espacio, pero no es copy publicable.
    if not values["legal"] and "legal" not in suppressed:
        legal_copy = [
            item.strip()
            for item in brief.legal_requirements
            if item.strip()
            and not item.casefold().startswith("conservar los legales")
        ]
        if legal_copy:
            values["legal"] = " · ".join(legal_copy)[:700]
    # El CTA no tiene una frase de respaldo segura: si no llega en la matriz o
    # desde el completado de copy con contexto real, se oculta. Inventar
    # "Conoce más" hace que una pieza institucional parezca una promoción.
    key_aliases = {
        "product_name": "nombre_producto", "headline": "titular", "subheadline": "subtitulo",
        "price": "precio", "previous_price": "precio_anterior", "installment": "cuota",
        "discount": "descuento", "validity": "vigencia",
    }
    for key, slot_key in key_aliases.items():
        if slot_key not in allowed:
            values[key] = ""
    return values


def _logo_path(campaign: Campaign) -> Path | None:
    """Prefiere un logo aislado; nunca usa una pagina/perfil completo como logo."""

    for source in campaign.sources:
        assets = source.meta.get("layer_assets", [])
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("role") != "logo":
                continue
            relative = asset.get("path")
            if not isinstance(relative, str):
                continue
            try:
                path = campaign_store.campaign_path(
                    campaign.client_id, campaign.campaign_id, relative
                )
            except Exception:  # noqa: BLE001
                continue
            if path.exists():
                return path
        # Campañas creadas en versiones intermedias pueden tener los PNG
        # extraídos pero no el manifiesto completo. Recuperar un archivo que
        # ya se llamó logo es seguro y evita que el renderer retroceda al
        # wordmark genérico al abrir una campaña antigua.
        for relative in source.asset_files:
            if "logo" not in Path(relative).name.casefold():
                continue
            try:
                path = campaign_store.campaign_path(
                    campaign.client_id, campaign.campaign_id, relative
                )
            except Exception:  # noqa: BLE001
                continue
            if path.is_file():
                return path
    for source in campaign.sources:
        roles = {role.value for role in source.roles}
        if source.kind.value != "image" or "logo" not in roles:
            continue
        try:
            path = campaign_store.campaign_path(
                campaign.client_id, campaign.campaign_id, source.stored_path
            )
        except Exception:  # noqa: BLE001
            continue
        if path.exists():
            return path
    return None


_FIXED_PSD_ROLES = {"fixed_background", "fixed_decoration"}
_FIXED_PSD_ASSET_MAX_PIXELS = 6_000_000
_FIXED_PSD_RENDER_LIMIT = 8


def _fixed_psd_asset_layers(
    campaign: Campaign,
    candidate: TemplateCandidate,
    canvas: tuple[int, int],
) -> list[tuple[str, Image.Image]]:
    """Compone capas fijas extraídas del PSD que sustenta esta candidata.

    Los assets se guardaron aislados durante la ingesta junto con la caja que
    ocupaban en el PSD maestro. Aquí se escala esa caja al canvas de destino,
    no el arte completo: los productos, precios y textos siguen siendo slots
    nuevos de la matriz. Si falta metadato o el archivo no se puede abrir, la
    plantilla conserva su fondo determinista normal.
    """

    source_ids = set(candidate.source_ids)
    if not source_ids:
        return []
    ordered_sources = sorted(
        (
            source for source in campaign.sources
            if source.source_id in source_ids and source.kind.value == "layered_design"
        ),
        key=lambda source: candidate.source_ids.index(source.source_id),
    )
    descriptors: list[tuple[int, int, int, str, Path, tuple[int, int, int, int], tuple[int, int]]] = []
    for source_order, source in enumerate(ordered_sources):
        assets = source.meta.get("layer_assets", [])
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("role") not in _FIXED_PSD_ROLES:
                continue
            relative = asset.get("path")
            raw_bbox = asset.get("bbox")
            raw_source_size = asset.get("source_size")
            if not isinstance(relative, str) or not isinstance(raw_bbox, (list, tuple)):
                continue
            if isinstance(raw_source_size, (list, tuple)) and len(raw_source_size) == 2:
                source_size = raw_source_size
            else:
                source_size = (source.width, source.height)
            try:
                source_width, source_height = (int(value) for value in source_size)
                left, top, right, bottom = (int(value) for value in raw_bbox)
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                source_width <= 0
                or source_height <= 0
                or source_width * source_height > settings.campaign_max_source_pixels
            ):
                continue
            left, right = max(0, min(source_width, left)), max(0, min(source_width, right))
            top, bottom = max(0, min(source_height, top)), max(0, min(source_height, bottom))
            if right <= left or bottom <= top:
                continue
            try:
                path = campaign_store.campaign_path(
                    campaign.client_id, campaign.campaign_id, relative
                )
            except Exception:  # noqa: BLE001 - manifiesto o ruta corrupta
                continue
            if not path.exists() or not path.is_file():
                continue
            try:
                z_index = int(asset.get("z_index", 0))
            except (TypeError, ValueError, OverflowError):
                z_index = 0
            role = str(asset["role"])
            role_order = 0 if role == "fixed_background" else 1
            descriptors.append(
                (
                    role_order, source_order, z_index, str(asset.get("name", "PSD fijo"))[:120],
                    path, (left, top, right, bottom), (source_width, source_height),
                )
            )

    layers: list[tuple[str, Image.Image]] = []
    width, height = canvas
    ordered_descriptors = sorted(
        descriptors, key=lambda item: (item[0], item[1], item[2], item[3])
    )
    for _role, _source_order, _z, name, path, bbox, source_size in ordered_descriptors[:_FIXED_PSD_RENDER_LIMIT]:
        try:
            with Image.open(path) as probe:
                if (
                    probe.width <= 0
                    or probe.height <= 0
                    or probe.width * probe.height > _FIXED_PSD_ASSET_MAX_PIXELS
                ):
                    continue
                probe.verify()
            with Image.open(path) as image:
                asset = image.convert("RGBA")
            left, top, right, bottom = bbox
            source_width, source_height = source_size
            x = round(left * width / source_width)
            y = round(top * height / source_height)
            target_width = max(1, round((right - left) * width / source_width))
            target_height = max(1, round((bottom - top) * height / source_height))
            x, y = min(max(0, x), width - 1), min(max(0, y), height - 1)
            target_width = min(target_width, width - x)
            target_height = min(target_height, height - y)
            fitted = asset.resize((target_width, target_height), Image.Resampling.LANCZOS)
            asset.close()
            layer = Image.new("RGBA", canvas, (0, 0, 0, 0))
            layer.alpha_composite(fitted, (x, y))
            fitted.close()
            layers.append((f"PSD fijo · {name}", layer))
        except Exception:  # noqa: BLE001 - un PNG dañado no bloquea producción
            continue
    return layers


def _render(
    campaign: Campaign,
    brand_name: str,
    candidate: TemplateCandidate,
    *,
    width: int,
    height: int,
    safe: dict[str, float],
    proposal: int,
    row: MatrixRow | None = None,
    products: list[Image.Image] | None = None,
) -> tuple[Image.Image, list[tuple[str, Image.Image]]]:
    colours = _palette(campaign.brief)
    canvas = (width, height)
    layers: list[tuple[str, Image.Image]] = []
    bold = _font_path(campaign, bold=True)
    regular = _font_path(campaign, bold=False)
    keys = _candidate_keys(candidate)
    if row is None:
        values = {
            "product_name": "NOMBRE DEL PRODUCTO", "headline": "TITULAR DE CAMPAÑA",
            "subheadline": "Subtitulo adaptable", "price": "$ PRECIO", "previous_price": "ANTES",
            "installment": "CUOTAS", "discount": "DESCUENTO", "cta": "CTA",
            "legal": "ESPACIO PARA LEGALES", "validity": "VIGENCIA",
        }
        # Un slot puede existir como capacidad futura de la matriz sin ser una
        # afirmación sobre esta campaña. La tarjeta de aprobación no debe
        # enseñar un precio/descuento ficticio solo para ilustrar esa capacidad.
        preview_slots = candidate.meta.get("preview_slot_keys")
        if isinstance(preview_slots, list):
            preview_keys = {str(item) for item in preview_slots}
            region_by_slot = {
                "nombre_producto": "product_name", "titular": "headline",
                "subtitulo": "subheadline", "precio": "price",
                "precio_anterior": "previous_price", "cuota": "installment",
                "descuento": "discount", "cta": "cta", "legal": "legal",
                "vigencia": "validity",
            }
            for slot, region in region_by_slot.items():
                if slot not in preview_keys:
                    values[region] = ""
    else:
        values = _values(row, campaign, candidate)

    slot_to_region = {
        "nombre_producto": "product_name", "titular": "headline",
        "subtitulo": "subheadline", "precio": "price",
        "precio_anterior": "previous_price", "cuota": "installment",
        "descuento": "discount", "cta": "cta", "legal": "legal",
        "vigencia": "validity", "logo": "logo", "producto": "product",
        "productos": "product",
    }
    # En la vista previa del master se conserva un hueco de producto para que
    # la persona pueda aprobarlo. En producción, una plantilla que admite cero
    # productos debe liberar esa zona cuando la fila no trae producto/imagen;
    # mostrar el placeholder en ese caso convertía una pieza institucional en
    # un arte falso de producto.
    show_product = row is None or bool(products)
    visible = {
        region for slot, region in slot_to_region.items()
        if slot in keys
        and (
            region == "logo"
            or (region == "product" and show_product)
            or (region not in {"product", "logo"} and values.get(region, "").strip())
        )
    }
    regions = _layout(candidate, width, height, safe, proposal, visible)

    blueprint = candidate.blueprint
    background = _background(
        width,
        height,
        colours,
        proposal + len(candidate.name),
        blueprint.background_style,
    )
    layers.append(("00 · Fondo", background))
    branding_background = _fixed_brand_background(campaign, canvas)
    if branding_background is not None:
        layers.append(("01 · " + branding_background[0], branding_background[1]))
    fixed_psd_layers = _fixed_psd_asset_layers(campaign, candidate, canvas)
    for index, (_name, layer) in enumerate(fixed_psd_layers, 1):
        layers.append((f"01.{index + 1:02d} · {_name}", layer))
    texture = (
        _reference_texture(campaign, candidate, width, height, proposal)
        if blueprint.background_style == "campaign" and not fixed_psd_layers and branding_background is None
        else None
    )
    if texture is not None:
        layers.append(("01 · Atmósfera de campaña", texture))
    # Si el PSD ya trajo sus marcos/ornamentos fijos, esa identidad es más
    # valiosa que añadir tarjetas, líneas o brillos genéricos encima.
    decoration_style = "minimal" if fixed_psd_layers else blueprint.accent_style
    layers.append(
        (
            "02 · Sistema visual",
            _decorations(width, height, colours, proposal, decoration_style),
        )
    )
    has_product_slot = any(
        slot.category == "product"
        or slot.key in {"producto", "productos", "product", "products"}
        for slot in candidate.slots
    )
    if has_product_slot and show_product:
        layers.extend(_product_layers(canvas, regions["product"], products or []))

    text_specs = [
        ("Titular", "headline", "titular", bold, True, 3, False, False),
        ("Subtitulo", "subheadline", "subtitulo", regular, False, 2, False, False),
        ("Nombre del producto", "product_name", "nombre_producto", bold, True, 2, False, False),
        ("Precio", "price", "precio", bold, True, 1, False, False),
        ("Precio anterior", "previous_price", "precio_anterior", regular, False, 1, False, True),
        ("Cuota", "installment", "cuota", bold, True, 1, False, False),
        ("Descuento", "discount", "descuento", bold, True, 1, True, False),
        ("CTA", "cta", "cta", bold, True, 1, True, False),
        ("Vigencia", "validity", "vigencia", regular, False, 1, False, False),
        ("Legal", "legal", "legal", regular, False, 3, False, False),
    ]
    for label, value_key, slot_key, font_path, is_bold, lines, badge, strike in text_specs:
        value = values.get(value_key, "").strip()
        if slot_key not in keys or not value:
            continue
        colour = (255, 255, 255, 245)
        layer = _text_layer(
            canvas, regions[value_key], value, font_path, colour=colour,
            bold=is_bold, max_lines=lines, align=blueprint.text_alignment,
            badge=badge, strike=strike,
        )
        layers.append((label, layer))

    # Logo real si fue subido; de lo contrario la marca queda como wordmark,
    # nunca como icono de Instagram/Facebook.
    logo_path = _logo_path(campaign)
    if logo_path is not None:
        try:
            with Image.open(logo_path) as image:
                logo = _fit_product(image.copy(), regions["logo"])
            layers.append(("Logo", _full_canvas(logo, canvas)))
        except Exception:  # noqa: BLE001
            logo_path = None
    if logo_path is None:
        layers.append(("Marca", _text_layer(canvas, regions["logo"], brand_name, bold, max_lines=1)))

    final = Image.new("RGBA", canvas, (0, 0, 0, 0))
    for _name, layer in layers:
        final.alpha_composite(layer)
    return final, layers


def render_candidate_previews(
    campaign: Campaign, brand_name: str, candidates: list[TemplateCandidate]
) -> None:
    folder = campaign_store.ensure_campaign_dirs(campaign.client_id, campaign.campaign_id) / "analysis" / "templates"
    folder.mkdir(parents=True, exist_ok=True)
    # Cada previsualización es una ubicación real reducida, no un rectángulo
    # decorativo: la medida conserva la proporción exacta del preset y el área
    # segura sale del propio catálogo. Importa sobre todo en story: Stories
    # reserva 14 % arriba y 20 % abajo para su interfaz, y con el margen
    # genérico del 3,5 % se aprobaba una plantilla cuyo titular o legal quedaba
    # justo debajo del nombre de la cuenta o del botón de enviar mensaje.
    formats = {
        "portrait": (720, 900, "meta_feed_4_5"),
        "square": (720, 720, "meta_feed_square"),
        "story": (540, 960, "meta_stories"),
        "landscape": (960, 503, "meta_feed_landscape"),
    }
    for index, candidate in enumerate(candidates, 1):
        candidate.preview_urls = {}
        for aspect, (width, height, preset_id) in formats.items():
            safe = FORMAT_PRESETS.get(preset_id, {}).get("safe_area", DEFAULT_SAFE_AREA)
            image, layers = _render(
                campaign,
                brand_name,
                candidate,
                width=width,
                height=height,
                safe={name: float(amount) for name, amount in safe.items()},
                proposal=index,
            )
            target = folder / f"{candidate.candidate_id}-{aspect}.png"
            image.convert("RGB").save(target, format="PNG", optimize=True)
            candidate.preview_urls[aspect] = (
                f"/clients/{campaign.client_id}/campaigns/{campaign.campaign_id}/"
                f"template-candidates/{candidate.candidate_id}/preview?aspect={aspect}"
            )
            image.close()
            for _name, layer in layers:
                layer.close()
        candidate.preview_url = (
            f"/clients/{campaign.client_id}/campaigns/{campaign.campaign_id}/"
            f"template-candidates/{candidate.candidate_id}/preview"
        )


def _product_tokens(row: MatrixRow) -> list[str]:
    if row.imagen:
        return [item.strip() for item in re.split(r"[|;]", row.imagen) if item.strip()]
    return [item.strip() for item in re.split(r"[|;]", row.producto) if item.strip()]


def _probe_product_image(path: Path) -> tuple[int, int]:
    """Valida el archivo que se va a abrir, sin confiar solo en la subida.

    Los activos persistidos pueden venir de una versión anterior de la campaña
    o quedar dañados en disco. ``verify`` detecta archivos truncados y el filtro
    convierte la advertencia de bomba de descompresión en un error explicable.
    """

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise CampaignProductionError(
            f"La imagen de producto '{path.name}' no se puede decodificar. "
            "Vuelve a cargar una copia válida."
        ) from exc
    if width <= 0 or height <= 0:
        raise CampaignProductionError(
            f"La imagen de producto '{path.name}' no tiene dimensiones válidas."
        )
    return width, height


def _validate_product_render_budget(
    row: MatrixRow,
    product_paths: list[Path],
    *,
    max_decode_pixels: int | None = None,
    max_products: int | None = None,
) -> None:
    """Comprueba el coste real de abrir un producto o combo antes de renderizar.

    El límite de subida por archivo no basta: cuatro fotos válidas de 20 Mpx
    pueden pedir varios cientos de MB a la vez. El presupuesto es por fila,
    porque las filas se procesan secuencialmente y un mismo producto puede
    reutilizarse en toda la matriz sin penalizar el lote entero.
    """

    maximum_products = max(
        1,
        int(
            max_products
            if max_products is not None
            else getattr(settings, "campaign_max_products_per_piece", 6)
        ),
    )
    if len(product_paths) > maximum_products:
        raise CampaignProductionError(
            f"Fila {row.row_number}: el combo tiene {len(product_paths)} imágenes; "
            f"el límite seguro es {maximum_products}. Divide el combo en más piezas."
        )

    native_limit = max(
        1,
        int(
            max_decode_pixels
            if max_decode_pixels is not None
            else getattr(
                settings,
                "campaign_max_product_decode_pixels",
                settings.campaign_max_product_pixels,
            )
        ),
    )
    total_pixels = 0
    for path in product_paths:
        width, height = _probe_product_image(path)
        pixels = width * height
        if pixels > settings.campaign_max_product_pixels:
            raise CampaignProductionError(
                f"La imagen de producto '{path.name}' excede el límite individual "
                f"de {settings.campaign_max_product_megapixels} Mpx."
            )
        total_pixels += pixels
    if total_pixels > native_limit:
        raise CampaignProductionError(
            f"Fila {row.row_number}: las imágenes del producto suman "
            f"{total_pixels / 1_000_000:.1f} Mpx; el límite seguro para un arte es "
            f"{native_limit / 1_000_000:.0f} Mpx. Reduce las fotos o divide el combo."
        )


def _open_product_for_render(
    path: Path, *, max_working_pixels: int | None = None
) -> Image.Image:
    """Abre una foto de producto en un tamaño de trabajo seguro para el renderer."""

    _probe_product_image(path)
    configured_working_limit = (
        max_working_pixels
        if max_working_pixels is not None
        else getattr(
            settings,
            "campaign_product_working_pixels",
            settings.campaign_max_output_pixels,
        )
    )
    # Un producto no necesita más píxeles que el lienzo más grande permitido;
    # incluso una variable de entorno demasiado generosa no debe retirar esta
    # protección de memoria.
    working_limit = max(
        1,
        min(int(configured_working_limit), settings.campaign_max_output_pixels),
    )
    try:
        with Image.open(path) as source:
            # JPEG admite una decodificación reducida; evita materializar una
            # foto de catálogo de 20 Mpx cuando el slot usa solo unos pocos.
            if source.format == "JPEG":
                scale = min(1.0, math.sqrt(working_limit / max(1, source.width * source.height)))
                if scale < 1.0:
                    source.draft("RGB", (
                        max(1, int(source.width * scale)),
                        max(1, int(source.height * scale)),
                    ))
            source.seek(0)
            source.load()
            product = ImageOps.exif_transpose(source)
            pixels = product.width * product.height
            if pixels > working_limit:
                scale = math.sqrt(working_limit / pixels)
                product.thumbnail(
                    (
                        max(1, int(product.width * scale)),
                        max(1, int(product.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            # ``convert`` sucede después de reducir, no sobre la foto nativa.
            return product.convert("RGBA").copy()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise CampaignProductionError(
            f"La imagen de producto '{path.name}' no se pudo preparar para el arte."
        ) from exc


def match_product_paths(row: MatrixRow, products: dict[str, Path]) -> list[Path]:
    by_key: dict[str, list[Path]] = {}
    for filename, path in products.items():
        for marker in {_normalise(filename), _normalise(Path(filename).stem)}:
            if path not in by_key.setdefault(marker, []):
                by_key[marker].append(path)
    matches: list[Path] = []
    for token in _product_tokens(row):
        key, stem = _normalise(token), _normalise(Path(token).stem)
        candidates = list(dict.fromkeys([*by_key.get(key, []), *by_key.get(stem, [])]))
        if not candidates:
            # Nombre de producto contra nombre de archivo: exige coincidencia
            # sustancial. Si hay dos coincidencias no se adivina silenciosamente.
            candidates = list(
                dict.fromkeys(
                    path
                    for marker, paths in by_key.items()
                    if len(key) >= 3 and (key in marker or marker in key)
                    for path in paths
                )
            )
        if len(candidates) > 1:
            raise CampaignProductionError(
                f"Fila {row.row_number}: '{token}' coincide con varias imagenes. "
                "Escribe el nombre de archivo exacto en la columna imagen."
            )
        if candidates and candidates[0] not in matches:
            matches.append(candidates[0])
    if not matches and len(products) == 1:
        matches = [next(iter(products.values()))]
    return matches


def resolve_formats(tokens: Iterable[str]) -> list[tuple[str, int, int, dict[str, float]]]:
    """Resuelve y deduplica aliases antes de contar o escribir archivos."""

    resolved: list[tuple[str, int, int, dict[str, float]]] = []
    seen: set[str] = set()
    for token in tokens:
        item = resolve_format(str(token))
        if item[0] in seen:
            continue
        seen.add(item[0])
        resolved.append(item)
    return resolved


def complete_copy_once(
    campaign: Campaign,
    rows: list[MatrixRow],
    allowed_fields_by_row: Mapping[int, set[str]] | None = None,
) -> list[str]:
    """Completa una vez el copy que cada plantilla aprobada permite.

    ``allowed_fields_by_row`` sale de la selección ya validada. Si falta (por
    compatibilidad con llamadas internas antiguas), se conservan los tres
    campos de copy, pero la producción siempre entrega el mapa explícito.
    """

    if not settings.openai_api_key:
        return ["OpenAI no esta configurado; el copy vacio uso el brief como respaldo."]

    def allowed(row: MatrixRow) -> set[str]:
        if allowed_fields_by_row is None:
            return set(_COPY_FIELDS)
        return set(allowed_fields_by_row.get(row.row_number, set())) & _COPY_FIELDS

    pending = [
        {
            "row_number": row.row_number,
            "product": row.producto,
            "headline": row.titular,
            "subtitle": row.subtitulo,
            "cta": row.cta,
            "instruction": row.notas,
            "suppressed": row.suppressed_fields,
        }
        for row in rows
        if any(
            value is None and field not in set(row.suppressed_fields) and field in allowed(row)
            for field, value in (
                ("titular", row.titular),
                ("subtitulo", row.subtitulo),
                ("cta", row.cta),
            )
        )
    ]
    if not pending:
        return []
    brief = campaign.brief or CampaignBrief(objective=campaign.objective)
    prompt = (
        "Completa copy publicitario breve en español para estas filas. Devuelve SOLO JSON "
        "{\"rows\":[{\"row_number\":2,\"headline\":\"\",\"subtitle\":\"\",\"cta\":\"\"}]}. "
        "No inventes precios, descuentos, legales, vigencias ni atributos. Si no hay base, deja "
        "la cadena vacia. Respeta suppressed y no escribas esos campos. Brief: "
        + json.dumps(brief.model_dump(mode="json"), ensure_ascii=False)
        + " Filas: " + json.dumps(pending[:120], ensure_ascii=False)
    )
    try:
        with httpx.Client(timeout=min(settings.request_timeout, 45)) as client:
            response = client.post(
                settings.openai_vision_endpoint,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_vision_model,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
        parsed = json.loads(response.json()["choices"][0]["message"]["content"])
        updates = {int(item["row_number"]): item for item in parsed.get("rows", [])}
        for row in rows:
            item = updates.get(row.row_number, {})
            suppressed = set(row.suppressed_fields)
            fields = allowed(row)
            if row.titular is None and "titular" not in suppressed and "titular" in fields:
                row.titular = str(item.get("headline") or "")[:220] or None
            if row.subtitulo is None and "subtitulo" not in suppressed and "subtitulo" in fields:
                row.subtitulo = str(item.get("subtitle") or "")[:300] or None
            if row.cta is None and "cta" not in suppressed and "cta" in fields:
                row.cta = str(item.get("cta") or "")[:80] or None
        return []
    except Exception:  # noqa: BLE001 - la produccion no depende de la API
        return ["OpenAI no completo el copy; se uso el brief como respaldo."]


def _save_psd(target: Path, size: tuple[int, int], layers: list[tuple[str, Image.Image]]) -> None:
    document = PSDImage.new("RGB", size, color=(0, 0, 0))
    for name, layer in layers:
        bbox = layer.getchannel("A").getbbox()
        if not bbox:
            continue
        crop = layer.crop(bbox)
        safe_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii") or "Capa"
        document.create_pixel_layer(crop, name=safe_name[:240], top=bbox[1], left=bbox[0])
        crop.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(target)


def _write_manifest(batch_dir: Path, batch: ProductionBatch) -> tuple[Path, Path]:
    csv_path = batch_dir / "estado.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["fila", "producto", "plantilla", "formato", "propuesta", "estado", "png", "jpg", "psd", "avisos"])
        for piece in batch.pieces:
            writer.writerow([
                piece.row_number, piece.product, piece.template_name, piece.format,
                piece.proposal, piece.status, piece.png, piece.jpg, piece.psd,
                " | ".join(piece.warnings),
            ])
    zip_path = batch_dir / "entregables.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for piece in batch.pieces:
            for relative in (piece.png, piece.jpg, piece.psd):
                path = batch_dir / relative
                if path.exists():
                    archive.write(path, relative)
        archive.write(csv_path, "estado.csv")
        archive.writestr("manifest.json", json.dumps(batch.model_dump(mode="json"), ensure_ascii=False, indent=2))
        archive.writestr(
            "LEEME.txt",
            "Cada arte incluye PNG, JPG y PSD por capas. Los campos vacios se ocultaron y la composicion se adapto al formato.\n",
        )
    return csv_path, zip_path


def produce_batch(
    campaign: Campaign,
    brand_name: str,
    rows: list[MatrixRow],
    products: dict[str, Path],
    *,
    matrix_filename: str,
    default_formats: Iterable[str],
    use_ai_copy: bool,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> ProductionBatch:
    approved = [candidate for candidate in campaign.template_candidates if candidate.approved]
    if not approved:
        raise CampaignProductionError("Aprueba al menos una plantilla antes de producir.")
    if not rows:
        raise CampaignProductionError("La matriz no contiene filas de produccion.")
    defaults = [item for item in default_formats if str(item).strip()] or ["meta_feed_4_5"]
    row_formats = {
        row.row_number: resolve_formats(row.formatos or defaults) for row in rows
    }
    planned = sum(
        len(row_formats[row.row_number]) * row.cantidad_propuestas for row in rows
    )

    def report_progress(done: int, detail: str) -> None:
        """El renderer no debe fallar si el canal de progreso se interrumpe."""

        if on_progress is None:
            return
        try:
            on_progress(done, planned, detail)
        except Exception:  # noqa: BLE001 - progreso es observabilidad, no render
            pass

    report_progress(0, "Validando productos y formatos…")
    maximum = max(1, int(getattr(settings, "campaign_max_pieces", 120)))
    if planned > maximum:
        raise CampaignProductionError(
            f"La tanda pide {planned} artes; el limite por tanda es {maximum}. Dividela en varias matrices."
        )
    planned_pixels = sum(
        width * height * row.cantidad_propuestas
        for row in rows
        for _format_id, width, height, _safe in row_formats[row.row_number]
    )
    if planned_pixels > settings.campaign_max_batch_pixels:
        raise CampaignProductionError(
            "La tanda acumula demasiada resolucion para una sola ejecucion "
            f"({planned_pixels / 1_000_000:.1f} Mpx; limite "
            f"{settings.campaign_max_batch_megapixels} Mpx). Divide la matriz."
        )
    missing: list[tuple[MatrixRow, int, int]] = []
    row_products: dict[int, list[Path]] = {}
    row_candidates: dict[int, TemplateCandidate] = {}
    for row in rows:
        expected = product_count(row)
        # Una fila sin producto puede elegir una plantilla institucional. No
        # usemos el fallback de ``match_product_paths`` (que toma la única foto
        # disponible) porque la foto no fue solicitada por esa fila.
        product_paths = match_product_paths(row, products) if expected else []
        row_products[row.row_number] = product_paths
        found = len(product_paths)
        if found < expected:
            missing.append((row, expected, found))
    if missing:
        labels = ", ".join(
            f"fila {row.row_number} ({found}/{expected} imagenes: "
            f"{row.imagen or row.producto or 'sin imagen'})"
            for row, expected, found in missing[:8]
        )
        raise CampaignProductionError(
            "Faltan imagenes de producto para " + labels
            + ". Cada producto del combo necesita un archivo que coincida con la columna imagen."
        )
    for row in rows:
        candidate = select_template(row, approved)
        if candidate is None:
            raise CampaignProductionError(
                f"Fila {row.row_number}: ninguna plantilla aprobada soporta ese contenido o combo."
            )
        row_candidates[row.row_number] = candidate
    # Validar el coste de decodificación antes de crear carpetas, llamar a IA o
    # abrir píxeles. Un error de tamaño/combo debe ser una respuesta 422 útil,
    # no un proceso agotado a mitad de la tanda.
    for row in rows:
        _validate_product_render_budget(row, row_products[row.row_number])
    batch = ProductionBatch(
        client_id=campaign.client_id,
        campaign_id=campaign.campaign_id,
        matrix_filename=matrix_filename[:240],
        total_rows=len(rows),
    )
    batch_dir = campaign_store.batch_dir(campaign.client_id, campaign.campaign_id, batch.batch_id)
    if use_ai_copy:
        report_progress(0, "Completando el copy permitido por el brief…")
        allowed_copy_by_row = {
            row_number: ai_fillable_fields(candidate)
            for row_number, candidate in row_candidates.items()
        }
        batch.warnings.extend(complete_copy_once(campaign, rows, allowed_copy_by_row))
    try:
        for row in rows:
            candidate = row_candidates[row.row_number]
            product_paths = row_products[row.row_number]
            opened: list[Image.Image] = []
            try:
                for path in product_paths:
                    opened.append(_open_product_for_render(path))
                for format_id, width, height, safe in row_formats[row.row_number]:
                    for proposal in range(1, row.cantidad_propuestas + 1):
                        final, layers = _render(
                            campaign, brand_name, candidate, width=width, height=height,
                            safe=safe, proposal=proposal, row=row, products=opened,
                        )
                        product_slug = slugify(row.producto or f"fila-{row.row_number}", "producto")
                        folder = Path(product_slug) / slugify(format_id, "formato")
                        stem = f"fila-{row.row_number:03d}_propuesta-{proposal:02d}"
                        png_rel, jpg_rel, psd_rel = (
                            str(folder / f"{stem}.png"), str(folder / f"{stem}.jpg"), str(folder / f"{stem}.psd")
                        )
                        png_path, jpg_path, psd_path = batch_dir / png_rel, batch_dir / jpg_rel, batch_dir / psd_rel
                        png_path.parent.mkdir(parents=True, exist_ok=True)
                        final.save(png_path, format="PNG", optimize=True)
                        final.convert("RGB").save(jpg_path, format="JPEG", quality=94, optimize=True)
                        _save_psd(psd_path, (width, height), layers)
                        piece = ProductionPiece(
                            row_number=row.row_number, product=row.producto,
                            template_candidate_id=candidate.candidate_id,
                            template_name=candidate.name, format=format_id,
                            width=width, height=height, proposal=proposal,
                            png=png_rel, jpg=jpg_rel, psd=psd_rel,
                        )
                        piece.preview_url = (
                            f"/clients/{campaign.client_id}/campaigns/{campaign.campaign_id}/"
                            f"production/{batch.batch_id}/files/{png_rel}"
                        )
                        batch.pieces.append(piece)
                        report_progress(
                            len(batch.pieces),
                            f"Renderizando arte {len(batch.pieces)} de {planned}…",
                        )
                        final.close()
                        for _name, layer in layers:
                            layer.close()
            finally:
                for image in opened:
                    image.close()
        batch.total_pieces = len(batch.pieces)
        batch.status = "partial" if batch.warnings else "ready"
        report_progress(planned, "Empaquetando PNG, JPG, PSD y CSV…")
        csv_path, zip_path = _write_manifest(batch_dir, batch)
        batch.manifest_url = (
            f"/clients/{campaign.client_id}/campaigns/{campaign.campaign_id}/production/"
            f"{batch.batch_id}/files/{csv_path.name}"
        )
        batch.zip_url = (
            f"/clients/{campaign.client_id}/campaigns/{campaign.campaign_id}/production/"
            f"{batch.batch_id}/files/{zip_path.name}"
        )
        campaign_store.save_batch(batch)
        report_progress(planned, "Entregables listos.")
        return batch
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise


__all__ = [
    "CampaignProductionError", "complete_copy_once", "match_product_paths",
    "produce_batch", "render_candidate_previews", "resolve_format",
]
