"""Ajustes finos: las herramientas manuales, agrupadas y opcionales.

Solo se usan cuando el modo automático se equivoca: corregir qué es cada
elemento, retocar un recorte o controlar la generación al detalle.
"""
from __future__ import annotations

import streamlit as st

from . import analyze_step, configure_step, layers_step
from .common import require_project

TOOLS = {
    "Revisar lo detectado": (
        analyze_step.render,
        "Ver qué encontró el sistema y corregir qué es cada cosa "
        "(producto, titular, precio…).",
    ),
    "Corregir un recorte": (
        layers_step.render,
        "Retocar el recorte de un elemento, escribir textos o reconstruir el fondo.",
    ),
    "Control total": (
        configure_step.render,
        "Elegir composiciones, permisos por elemento y todos los parámetros.",
    ),
}


def render() -> None:
    st.title("Ajustes finos")
    st.caption(
        "No hace falta entrar aquí para generar. Úsalo solo si el resultado "
        "automático necesita correcciones."
    )
    project = require_project()
    if not project:
        return

    tool = st.radio(
        "Herramienta", list(TOOLS), horizontal=True, label_visibility="collapsed"
    )
    renderer, help_text = TOOLS[tool]
    st.caption(help_text)
    st.divider()
    renderer()
