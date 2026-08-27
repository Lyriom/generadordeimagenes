"""Frontend Streamlit del generador de variantes.

Solo presentación y llamadas HTTP: toda la lógica vive en el backend FastAPI.
"""
from __future__ import annotations

import streamlit as st

import api_client as api
from steps import advanced_step, simple_step

st.set_page_config(
    page_title="Generador de variantes creativas",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="expanded",
)

PAGES = {
    "Generar": simple_step.render,
    "Ajustes finos": advanced_step.render,
}


def _init_state() -> None:
    st.session_state.setdefault("project_id", None)
    st.session_state.setdefault("project", None)
    st.session_state.setdefault("campaign_projects", [])
    st.session_state.setdefault("page", "Generar")
    st.session_state.setdefault("cache_token", 0)
    st.session_state.setdefault("selected_variants", [])
    st.session_state.setdefault("auto_steps", [])
    st.session_state.setdefault("auto_warnings", [])


def _status_note() -> None:
    """Estado del backend, sin jerga en primer plano."""
    try:
        status = api.health()
    except Exception as exc:  # noqa: BLE001
        st.sidebar.error(f"No hay conexión con el motor en {api.BACKEND_URL}\n\n{exc}")
        return

    providers = status.get("providers", {})
    ocr = providers.get("ocr", {})
    st.sidebar.caption("🟢 Motor conectado")
    with st.sidebar.expander("Detalle técnico", expanded=False):
        st.write(f"Versión `{status.get('version', '?')}`")
        st.write(f"Recorte: `{providers.get('segmentation', {}).get('active')}`")
        st.write(
            "Lectura de texto: "
            + (f"`{ocr.get('active')}`" if ocr.get("available") else "no instalada")
        )
        st.write(f"Relleno de fondo: `{providers.get('inpainting', {}).get('active')}`")
        image_provider = providers.get("inpainting", {})
        if image_provider.get("openai_available"):
            st.write(f"IA de imagen: `{image_provider.get('openai_model', 'gpt-image-2')}` ✅")
        else:
            st.write("IA de imagen: sin clave (modo local)")
        if not ocr.get("available"):
            st.caption(
                "Sin lector de texto los titulares no se detectan solos: puede "
                "escribirlos en Ajustes finos."
            )


def _sidebar() -> str:
    st.sidebar.title("🎨 Artes desde KV")
    st.sidebar.caption("Un KV en PSD, varios productos y todas las piezas listas.")

    page = st.sidebar.radio("Sección", list(PAGES), key="page")

    project = st.session_state.get("project")
    campaign = st.session_state.get("campaign_projects") or []
    if project:
        st.sidebar.divider()
        st.sidebar.markdown(
            f"**Campaña abierta**\n\n{len(campaign) or 1} KV · {project['name']} activo"
        )
        st.sidebar.caption(
            f"{project['canvas']['width']}×{project['canvas']['height']} px · "
            f"{len(project.get('variants', []))} variantes"
        )

    st.sidebar.divider()
    _status_note()
    st.sidebar.caption(
        "Un JPG o PNG no tiene capas: la separación es aproximada. Con el PSD, "
        "en cambio, se usan las capas reales."
    )
    return page


def main() -> None:
    _init_state()
    page = _sidebar()
    PAGES[page]()


if __name__ == "__main__":
    main()
