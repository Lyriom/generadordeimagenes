"""Galería de resultados: ver, elegir y descargar."""
from __future__ import annotations

import streamlit as st

import api_client as api

from .common import (
    cached_file,
    cached_variant_image,
    cached_zip,
    score_badge,
    show_error,
    token,
)

COLUMNS = 3


def _download_bar(
    project: dict, variants: list[dict], selected: list[str], key_prefix: str
) -> None:
    col1, col2 = st.columns([3, 2])
    with col1:
        label = (
            f"⬇ Descargar las {len(selected)} elegidas (ZIP)"
            if selected
            else f"⬇ Descargar todas · {len(variants)} imágenes (ZIP)"
        )
        try:
            archive = cached_zip(project["project_id"], tuple(selected), True, token())
            st.download_button(
                label.replace("(ZIP)", "· PNG + PSD + SVG Illustrator (ZIP)"),
                data=archive,
                file_name=f"{project['name'].replace(' ', '_')}_variantes.zip",
                mime="application/zip",
                type="primary",
                width="stretch",
                key=f"zip-{key_prefix}-{len(selected)}",
            )
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
    with col2:
        st.caption(
            "Marca “Elegir” en las que te gusten para descargar solo esas. "
            "Cada imagen también se puede bajar sola."
        )


def gallery(project: dict, key_prefix: str | None = None) -> None:
    """Galería reutilizable. Lee las variantes ya guardadas en el proyecto."""
    key_prefix = key_prefix or project["project_id"]
    try:
        variants = api.list_variants(project["project_id"])["variants"]
    except Exception as exc:  # noqa: BLE001
        show_error(exc)
        return

    if not variants:
        st.info("Todavía no hay propuestas generadas.")
        return

    scores = [variant["quality"]["score"] for variant in variants]
    st.subheader(f"Resultados · {len(variants)} propuestas")
    st.caption(
        f"Calidad promedio {sum(scores) / len(scores):.0f}/100 · mejor {max(scores)}/100. "
        "El puntaje mide legibilidad, márgenes, contraste y tamaño del producto: "
        "es una ayuda, no un juez."
    )

    order = st.radio(
        "Orden",
        ["Mejores primero", "Orden de generación"],
        horizontal=True,
        label_visibility="collapsed",
        key=f"order-{key_prefix}",
    )
    visible = sorted(
        variants,
        key=(
            (lambda item: -item["quality"]["score"])
            if order == "Mejores primero"
            else (lambda item: item["index"])
        ),
    )

    selected = [
        variant["id"]
        for variant in visible
        if st.session_state.get(f"pick-{variant['id']}")
    ]
    _download_bar(project, variants, selected, key_prefix)
    st.divider()

    for row_start in range(0, len(visible), COLUMNS):
        columns = st.columns(COLUMNS)
        for column, variant in zip(columns, visible[row_start : row_start + COLUMNS]):
            with column:
                try:
                    st.image(
                        cached_file(
                            project["project_id"],
                            variant["image"],
                            token(),
                        ),
                        width="stretch",
                    )
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)

                st.markdown(
                    f"**{variant['width']}×{variant['height']}** · "
                    f"{score_badge(variant['quality']['score'])}"
                )
                format_meta = variant.get("meta", {}).get("format") or {}
                if format_meta:
                    st.caption(
                        f"{format_meta.get('platform', '')} · "
                        f"{format_meta.get('placement', variant['format'])} · "
                        f"{format_meta.get('ratio', '')}"
                    )
                product_label = variant.get("meta", {}).get("product_label")
                if product_label:
                    st.caption(f"Producto: {product_label}")
                st.checkbox("Elegir", key=f"pick-{variant['id']}")

                png_download, svg_download = st.columns(2)
                try:
                    png_download.download_button(
                        "⬇ PNG",
                        data=cached_variant_image(
                            project["project_id"], variant["id"], token()
                        ),
                        file_name=f"{(product_label or 'producto')}_{variant['index']:02d}_"
                        f"{variant['layout']}_"
                        f"{variant['format']}.png",
                        mime="image/png",
                        key=f"dl-{variant['id']}",
                        width="stretch",
                    )
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)
                svg_path = variant.get("meta", {}).get("svg")
                if svg_path:
                    try:
                        svg_download.download_button(
                            "⬇ Illustrator",
                            data=cached_file(project["project_id"], svg_path, token()),
                            file_name=(
                                f"{(product_label or 'producto')}_{variant['index']:02d}_"
                                f"{variant['format']}.svg"
                            ),
                            mime="image/svg+xml",
                            key=f"svg-{variant['id']}",
                            width="stretch",
                        )
                    except Exception as exc:  # noqa: BLE001
                        show_error(exc)
                else:
                    svg_download.caption("SVG disponible al regenerar")

                warnings = variant["quality"]["warnings"]
                with st.expander(
                    f"⚠️ {len(warnings)} avisos" if warnings else "Detalle", expanded=False
                ):
                    st.caption(f"Composición: {variant['layout_label']}")
                    for warning in warnings:
                        st.caption(f"• {warning}")
                    if not warnings:
                        st.caption("Sin avisos.")
