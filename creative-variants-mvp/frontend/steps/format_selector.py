"""Selector visual de presets por plataforma y sus áreas seguras."""
from __future__ import annotations

from collections import defaultdict

import streamlit as st
from PIL import Image, ImageDraw

import api_client as api


@st.cache_data(show_spinner=False, ttl=900)
def format_catalog() -> list[dict]:
    capabilities = api.capabilities()
    detailed = capabilities.get("format_catalog") or []
    if detailed:
        return detailed
    # Compatibilidad si el frontend nuevo apunta temporalmente a un backend viejo.
    return [
        {
            "id": key,
            "platform": "Formatos disponibles",
            "family": "General",
            "placement": key,
            "label": key,
            "width": size[0],
            "height": size[1],
            "ratio": f"{size[0]}:{size[1]}",
            "safe_area": {"left": 0.035, "top": 0.035, "right": 0.035, "bottom": 0.035},
            "recommended": key in {"1080x1080", "1080x1350", "1080x1920"},
            "media_type": "image",
            "note": "",
        }
        for key, size in capabilities.get("formats", {}).items()
    ]


def _preview(spec: dict) -> Image.Image:
    width, height = int(spec["width"]), int(spec["height"])
    scale = min(220 / max(1, width), 170 / max(1, height))
    draw_w, draw_h = max(36, int(width * scale)), max(36, int(height * scale))
    image = Image.new("RGB", (240, 205), "white")
    draw = ImageDraw.Draw(image)
    x0, y0 = (240 - draw_w) // 2, 8
    x1, y1 = x0 + draw_w, y0 + draw_h
    for row in range(draw_h):
        mix = row / max(1, draw_h - 1)
        color = (int(36 + 126 * mix), int(94 - 25 * mix), int(214 - 32 * mix))
        draw.line((x0, y0 + row, x1, y0 + row), fill=color)
    safe = spec.get("safe_area") or {}
    sx0 = x0 + int(draw_w * float(safe.get("left", 0.035)))
    sy0 = y0 + int(draw_h * float(safe.get("top", 0.035)))
    sx1 = x1 - int(draw_w * float(safe.get("right", 0.035)))
    sy1 = y1 - int(draw_h * float(safe.get("bottom", 0.035)))
    # Pillow no ofrece rectángulo discontinuo en todas las versiones soportadas.
    dash = 6
    for pos in range(sx0, sx1, dash * 2):
        draw.line((pos, sy0, min(pos + dash, sx1), sy0), fill="white", width=2)
        draw.line((pos, sy1, min(pos + dash, sx1), sy1), fill="white", width=2)
    for pos in range(sy0, sy1, dash * 2):
        draw.line((sx0, pos, sx0, min(pos + dash, sy1)), fill="white", width=2)
        draw.line((sx1, pos, sx1, min(pos + dash, sy1)), fill="white", width=2)
    draw.text((8, 182), f"{width}×{height} px · {spec.get('ratio', '')}", fill=(20, 30, 48))
    return image


def _safe_label(spec: dict) -> str:
    safe = spec.get("safe_area") or {}
    values = [round(float(safe.get(key, 0)) * 100) for key in ("left", "top", "right", "bottom")]
    if len(set(values)) == 1:
        return f"{values[0]} % alrededor"
    return f"I {values[0]} % · A {values[1]} % · D {values[2]} % · B {values[3]} %"


def select_formats(
    *,
    key_prefix: str,
    allow_auto: bool,
    default_ids: list[str] | None = None,
) -> list[str] | None:
    """Selecciona ubicaciones concretas; ``None`` conserva el modo automático."""
    catalog = format_catalog()
    if allow_auto:
        mode = st.radio(
            "Tamaños",
            ["auto", "platforms"],
            format_func=lambda value: {
                "auto": "Automático · original + formatos sociales",
                "platforms": "Elegir por plataforma y ubicación",
            }[value],
            horizontal=True,
            key=f"{key_prefix}-format-mode",
            label_visibility="collapsed",
        )
        if mode == "auto":
            st.caption(
                "Mantiene la proporción más cercana del KV y añade cuadrado y vertical."
            )
            return None

    by_platform: dict[str, list[dict]] = defaultdict(list)
    for item in catalog:
        by_platform[item.get("platform", "Otros")].append(item)
    platforms = list(by_platform)
    default_platforms = ["Meta"] if "Meta" in platforms else platforms[:1]
    chosen_platforms = st.multiselect(
        "Plataformas",
        platforms,
        default=default_platforms,
        key=f"{key_prefix}-platforms",
    )
    visible = [item for item in catalog if item.get("platform") in chosen_platforms]
    by_id = {item["id"]: item for item in visible}
    requested_defaults = default_ids or [
        "meta_feed_4_5", "meta_stories", "meta_reels"
    ]
    defaults = [item for item in requested_defaults if item in by_id]
    if not defaults:
        defaults = [item["id"] for item in visible if item.get("recommended")][:3]
    chosen = st.multiselect(
        "Ubicaciones y medidas",
        list(by_id),
        default=defaults,
        format_func=lambda value: (
            f"{by_id[value]['family']} · {by_id[value]['placement']} · "
            f"{by_id[value]['width']}×{by_id[value]['height']} ({by_id[value]['ratio']})"
        ),
        key=f"{key_prefix}-format-ids",
    )

    selected = [by_id[item] for item in chosen if item in by_id]
    if selected:
        st.caption(
            f"{len(selected)} salida(s) seleccionada(s). Las líneas blancas muestran "
            "dónde deben quedar logo, producto, copy y legales."
        )
        columns = st.columns(min(4, len(selected)))
        for index, spec in enumerate(selected):
            with columns[index % len(columns)]:
                st.image(_preview(spec), width="stretch")
                st.caption(f"**{spec['placement']}**  \nÁrea segura: {_safe_label(spec)}")
                if spec.get("note"):
                    st.caption(spec["note"])
    elif chosen_platforms:
        st.warning("Elige al menos una ubicación para generar.")
    return chosen
