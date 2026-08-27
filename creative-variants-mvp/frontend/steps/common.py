"""Utilidades compartidas entre pasos."""
from __future__ import annotations

import streamlit as st

import api_client as api

CATEGORIES = {
    "product": "Producto",
    "person": "Persona",
    "logo": "Logo",
    "headline": "Titular",
    "subheadline": "Subtítulo",
    "price": "Precio",
    "cta": "CTA",
    "legal": "Texto legal",
    "decoration": "Decoración",
    "background": "Fondo",
}

CATEGORY_KEYS = list(CATEGORIES)


@st.cache_data(show_spinner=False, ttl=900, max_entries=200)
def cached_file(project_id: str, relative_path: str, token: int) -> bytes:
    """Descarga un archivo del proyecto. `token` invalida la caché tras cada cambio."""
    return api.project_file(project_id, relative_path)


@st.cache_data(show_spinner=False, ttl=900, max_entries=60)
def cached_variant_image(project_id: str, variant_id: str, token: int) -> bytes:
    return api.variant_image(project_id, variant_id)


@st.cache_data(show_spinner=False, ttl=900, max_entries=20)
def cached_zip(
    project_id: str, variant_ids: tuple[str, ...], include_layers: bool, token: int
) -> bytes:
    return api.export_zip(project_id, list(variant_ids) or None, include_layers)


@st.cache_data(show_spinner=False, ttl=900, max_entries=40)
def cached_detections(project_id: str, token: int) -> bytes:
    return api.preview_detections(project_id)


@st.cache_data(show_spinner=False, ttl=900, max_entries=80)
def cached_mask(project_id: str, layer_id: str, token: int) -> bytes:
    return api.preview_mask(project_id, layer_id)


def token() -> int:
    return int(st.session_state.get("cache_token", 0))


def require_project() -> dict | None:
    project = st.session_state.get("project")
    if not project:
        st.info("Primero elija un arte en la pantalla Generar.")
        return None
    return project


def refresh_project() -> dict:
    project = api.get_project(st.session_state.project_id)
    st.session_state.project = project
    st.session_state.cache_token += 1
    return project


def show_error(exc: Exception) -> None:
    st.error(f"❌ {exc}")


def confidence_badge(confidence: float) -> str:
    if confidence >= 0.75:
        return f"🟢 {confidence:.2f}"
    if confidence >= 0.5:
        return f"🟡 {confidence:.2f}"
    return f"🔴 {confidence:.2f}"


def score_badge(score: int) -> str:
    if score >= 85:
        return f"🟢 {score}/100"
    if score >= 65:
        return f"🟡 {score}/100"
    return f"🔴 {score}/100"


def layer_label(layer: dict) -> str:
    icon = "🅣" if layer["type"] == "text" else "🖼"
    lock = " 🔒" if layer.get("locked") else ""
    return f"{icon} {layer['name']} ({CATEGORIES.get(layer['category'], layer['category'])}){lock}"


def warning_list(warnings: list[str], empty: str | None = None) -> None:
    if not warnings:
        if empty:
            st.success(empty)
        return
    for warning in warnings:
        st.warning(warning)
