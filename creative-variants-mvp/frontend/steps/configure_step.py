"""Ajustes finos · configurar y generar variaciones con control total."""
from __future__ import annotations

import streamlit as st

import api_client as api

from .common import layer_label, refresh_project, require_project, show_error

FORMATS = [
    "1080x1080",
    "1080x1350",
    "1080x1920",
    "1920x1080",
    "900x660",
    "900x1350",
    "1200x400",
]
INTENSITY_LABELS = {
    "conservative": "Conservadora (respeta el arte original)",
    "moderate": "Moderada (recomendada)",
    "creative": "Creativa (cambia mucho la composición)",
}


def render() -> None:
    st.header("Control total de la generación")
    project = require_project()
    if not project:
        return

    layers = [
        layer
        for layer in project.get("layers", [])
        if layer["category"] != "background"
    ]
    usable = [
        layer
        for layer in layers
        if (layer["type"] == "text" and (layer.get("content") or "").strip())
        or (layer["type"] == "image" and layer.get("src"))
    ]

    if not layers:
        st.info("Primero genere en la pantalla Generar o cree las capas a mano.")
        return
    if not usable:
        st.warning(
            "Ninguna capa está lista: extraiga los PNG de las capas imagen y escriba el "
            "contenido de las capas de texto en \"Corregir un recorte\"."
        )

    if not project.get("background", {}).get("path"):
        st.info(
            "No hay fondo reconstruido. Se usará el arte original como fondo, lo que "
            "puede dejar visibles los elementos antiguos. Reconstrúyalo en \"Corregir un recorte\"."
        )

    col1, col2 = st.columns(2)
    with col1:
        count = st.slider("Cantidad de variantes", 4, 30, 12)
        seed = st.number_input("Semilla aleatoria", 0, 2**31 - 1, 42, help="Misma semilla = mismo resultado")
        intensity = st.radio(
            "Intensidad de cambio",
            list(INTENSITY_LABELS),
            index=1,
            format_func=lambda key: INTENSITY_LABELS[key],
        )
    with col2:
        formats = st.multiselect(
            "Formatos", FORMATS, default=["1080x1080", "1080x1350", "1080x1920"]
        )
        instruction = st.text_area(
            "Instrucción opcional",
            placeholder="Ej.: producto grande y titular arriba",
            height=80,
            help=(
                "Se interpretan palabras clave: producto grande/pequeño, titular arriba, "
                "centrado, vertical, diagonal, dividido, izquierda, derecha, minimal."
            ),
        )
        try:
            catalog = api.capabilities().get("layouts", [])
        except Exception:  # noqa: BLE001
            catalog = []
        layout_keys = [item["key"] for item in catalog]
        chosen_layouts = st.multiselect(
            "Familias de layout (vacío = todas)",
            layout_keys,
            default=[],
            format_func=lambda key: next(
                (item["label"] for item in catalog if item["key"] == key), key
            ),
        )

    st.divider()
    st.subheader("Permisos por capa")
    st.caption(
        "Las capas bloqueadas conservan sus píxeles y proporciones. Puede seguir "
        "moviéndolas o escalándolas de forma uniforme."
    )

    header = st.columns([4, 1, 1, 1, 1, 1])
    for col, title in zip(header, ["Capa", "Bloqueada", "Mover", "Escalar", "Reordenar", "Ocultar"]):
        col.markdown(f"**{title}**")

    locked, movable, resizable, reorderable, hidden = [], [], [], [], []
    for layer in layers:
        cols = st.columns([4, 1, 1, 1, 1, 1])
        cols[0].write(layer_label(layer))
        if cols[1].checkbox("Bloqueada", value=layer["locked"], key=f"g-lock-{layer['id']}", label_visibility="collapsed"):
            locked.append(layer["id"])
        if cols[2].checkbox("Mover", value=layer["movable"], key=f"g-mov-{layer['id']}", label_visibility="collapsed"):
            movable.append(layer["id"])
        if cols[3].checkbox("Escalar", value=layer["resizable"], key=f"g-res-{layer['id']}", label_visibility="collapsed"):
            resizable.append(layer["id"])
        if cols[4].checkbox("Reordenar", value=layer["reorderable"], key=f"g-reo-{layer['id']}", label_visibility="collapsed"):
            reorderable.append(layer["id"])
        if cols[5].checkbox("Ocultar", value=not layer["visible"], key=f"g-hid-{layer['id']}", label_visibility="collapsed"):
            hidden.append(layer["id"])

    st.divider()
    if st.button("✨ Generar variantes", type="primary", disabled=not usable):
        if not formats:
            st.error("Seleccione al menos un formato.")
            return
        config = {
            "count": int(count),
            "seed": int(seed),
            "formats": formats,
            "intensity": intensity,
            "locked_layers": locked,
            "movable_layers": movable,
            "resizable_layers": resizable,
            "reorderable_layers": reorderable,
            "hidden_layers": hidden,
            "instruction": instruction or None,
            "layouts": chosen_layouts or None,
            "replace_existing": True,
        }
        try:
            with st.spinner(f"Generando {count} variantes…"):
                result = api.generate(project["project_id"], config)
            refresh_project()
            st.session_state.selected_variants = []
            scores = [variant["quality"]["score"] for variant in result["variants"]]
            st.success(
                f"{len(result['variants'])} variantes generadas · "
                f"puntaje promedio {sum(scores) / len(scores):.0f}/100"
            )
            for warning in result.get("warnings", []):
                st.warning(warning)
            st.info("Vuelva a la pantalla Generar para ver la galería y descargar.")
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
