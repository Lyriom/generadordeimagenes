"""Ajustes finos · revisar y corregir lo que se detectó."""
from __future__ import annotations

import streamlit as st

import api_client as api

from .common import (
    CATEGORIES,
    cached_detections,
    CATEGORY_KEYS,
    confidence_badge,
    refresh_project,
    require_project,
    show_error,
    token,
    warning_list,
)


def render() -> None:
    st.header("Revisar lo que se detectó")
    project = require_project()
    if not project:
        return

    st.caption(
        "La detección automática combina segmentación (OpenCV o SAM si está habilitado) "
        "y OCR. Es una aproximación: corrija la categoría cuando haga falta."
    )

    col1, col2, col3 = st.columns([1, 1, 2])
    run_ocr = col1.toggle("Ejecutar OCR", value=True)
    max_regions = col2.slider("Máx. regiones", 4, 24, 12)
    if col3.button("🔍 Analizar arte", type="primary"):
        try:
            with st.spinner("Detectando componentes…"):
                result = api.analyze(project["project_id"], run_ocr=run_ocr, max_regions=max_regions)
            refresh_project()
            st.session_state.analysis_warnings = result.get("warnings", [])
            st.success(
                f"{len(result['layers'])} capas propuestas "
                f"(segmentación: {result.get('segmentation_provider') or 'n/d'}, "
                f"OCR: {result.get('ocr_provider') or 'no disponible'})"
            )
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)

    project = st.session_state.project
    analysis = project.get("analysis", {})
    layers = [layer for layer in project.get("layers", []) if layer["category"] != "background"]

    if analysis.get("ran_at"):
        st.caption(
            f"Último análisis: {analysis['ran_at']} · "
            f"{analysis.get('detections', 0)} regiones · "
            f"{analysis.get('text_regions', 0)} textos"
        )
    warning_list(analysis.get("warnings", []))

    if not layers:
        st.info("Aún no hay detecciones. Ejecute el análisis o cree las capas a mano.")
        return

    st.divider()
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Detecciones sobre el arte")
        try:
            preview = cached_detections(project["project_id"], token())
            st.image(preview, width="stretch")
        except Exception as exc:  # noqa: BLE001
            show_error(exc)

    with right:
        st.subheader("Revisar y corregir categorías")
        updates: list[dict] = []
        with st.form("categories"):
            for layer in layers:
                cols = st.columns([3, 2, 1])
                content = layer.get("content")
                label = layer["name"]
                if content:
                    label = f"{label} — “{content[:40]}”"
                cols[0].markdown(f"**{label}**")
                cols[0].caption(
                    f"{layer['type']} · ({layer['x']},{layer['y']}) "
                    f"{layer['width']}×{layer['height']}"
                )
                new_category = cols[1].selectbox(
                    "Categoría",
                    CATEGORY_KEYS,
                    index=CATEGORY_KEYS.index(layer["category"]),
                    format_func=lambda key: CATEGORIES[key],
                    key=f"cat-{layer['id']}",
                    label_visibility="collapsed",
                )
                cols[2].markdown(confidence_badge(layer.get("confidence", 0)))
                for warning in layer.get("warnings", []):
                    st.caption(f"⚠️ {warning}")
                if new_category != layer["category"]:
                    updates.append({"id": layer["id"], "category": new_category})
            saved = st.form_submit_button("Guardar categorías", type="primary")

        if saved:
            if not updates:
                st.info("No hay cambios de categoría.")
            else:
                try:
                    api.update_layers(project["project_id"], updates=updates)
                    refresh_project()
                    st.success(f"{len(updates)} categorías actualizadas.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)

    st.divider()
    st.subheader("Textos reconocidos")
    text_layers = [layer for layer in layers if layer["type"] == "text"]
    if not text_layers:
        st.caption(
            "Sin textos detectados. Si PaddleOCR no está instalado, cree las capas de "
            "texto a mano en \"Corregir un recorte\"."
        )
    else:
        st.dataframe(
            [
                {
                    "Capa": layer["name"],
                    "Categoría": CATEGORIES[layer["category"]],
                    "Texto": layer.get("content"),
                    "Confianza": round(layer.get("confidence", 0), 2),
                    "Tamaño fuente": layer.get("font_size"),
                    "Color": layer.get("color"),
                }
                for layer in text_layers
            ],
            width="stretch",
            hide_index=True,
        )
