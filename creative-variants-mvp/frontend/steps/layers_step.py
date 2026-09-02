"""Ajustes finos · corregir capas, máscaras, comportamiento y orden.

No se usa una librería de canvas: la corrección se hace con rectángulos y
controles numéricos (pincel rectangular/elíptico), que funciona en cualquier
versión de Streamlit.
"""
from __future__ import annotations

import streamlit as st
from streamlit_drawable_canvas import st_canvas
import io
from PIL import Image


import api_client as api

from .common import (
    CATEGORIES,
    CATEGORY_KEYS,
    cached_file,
    cached_mask,
    layer_label,
    refresh_project,
    require_project,
    show_error,
    token,
)


def _selected_layer(project: dict) -> dict | None:
    layers = [layer for layer in project.get("layers", []) if layer["category"] != "background"]
    if not layers:
        return None
    ids = [layer["id"] for layer in layers]
    labels = {layer["id"]: layer_label(layer) for layer in layers}
    default = st.session_state.get("selected_layer_id")
    index = ids.index(default) if default in ids else 0
    selected = st.selectbox(
        "Capa seleccionada",
        ids,
        index=index,
        format_func=lambda value: labels[value],
        key="selected_layer_id",
    )
    return next(layer for layer in layers if layer["id"] == selected)


def _geometry_and_mask(project: dict, layer: dict) -> None:
    canvas = project["canvas"]
    st.markdown("##### Geometría y máscara")
    col1, col2 = st.columns(2)
    x = col1.number_input("X", 0, canvas["width"] - 1, int(layer["x"]), key=f"x-{layer['id']}")
    y = col2.number_input("Y", 0, canvas["height"] - 1, int(layer["y"]), key=f"y-{layer['id']}")
    width = col1.number_input(
        "Ancho", 4, canvas["width"], int(layer["width"]), key=f"w-{layer['id']}"
    )
    height = col2.number_input(
        "Alto", 4, canvas["height"], int(layer["height"]), key=f"h-{layer['id']}"
    )

    col1, col2, col3 = st.columns(3)
    if col1.button("Aplicar rectángulo", key=f"geo-{layer['id']}"):
        _update(project, [{"id": layer["id"], "x": x, "y": y, "width": width, "height": height}])
    if col2.button("Auto-segmentar", key=f"seg-{layer['id']}", help="Segmenta el sujeto dentro del rectángulo"):
        _mask_op(project, {"layer_id": layer["id"], "auto_segment": True, "re_extract": True})
    if col3.button("Máscara = rectángulo", key=f"rst-{layer['id']}"):
        _mask_op(
            project, {"layer_id": layer["id"], "reset_from_box": True, "re_extract": True}
        )

    with st.expander("Pincel (dibujo libre a mano alzada)"):
        st.caption("Dibuja sobre la imagen para definir la nueva máscara de la capa.")
        bg_image_path = cached_file(project["project_id"], project["source"]["path"], token())
        bg_image = Image.open(bg_image_path).convert("RGBA")
        
        # Calculate proportional size for display
        display_width = 600
        ratio = display_width / canvas["width"]
        display_height = int(canvas["height"] * ratio)
        
        stroke_width = st.slider("Tamaño del pincel", 1, 50, 10, key=f"sw-{layer['id']}")
        
        canvas_result = st_canvas(
            fill_color="rgba(0, 255, 0, 0.3)",
            stroke_width=stroke_width,
            stroke_color="rgba(0, 255, 0, 1.0)",
            background_image=bg_image,
            update_streamlit=False,
            height=display_height,
            width=display_width,
            drawing_mode="freedraw",
            key=f"canvas-{layer['id']}",
        )
        
        if st.button("Guardar dibujo como máscara", type="primary", key=f"save-canvas-{layer['id']}"):
            if canvas_result.image_data is not None:
                # Resize the canvas mask back to original resolution
                mask_image = Image.fromarray(canvas_result.image_data.astype('uint8'), 'RGBA')
                mask_image = mask_image.resize((canvas["width"], canvas["height"]), Image.NEAREST)
                # Convert to L (grayscale)
                mask_l = Image.new("L", mask_image.size, 0)
                mask_l.paste(255, mask=mask_image.split()[3]) # Use alpha channel as mask
                
                buf = io.BytesIO()
                mask_l.save(buf, format="PNG")
                api.upload_mask(project["project_id"], layer["id"], buf.getvalue())
                refresh_project()
                st.rerun()



def _behaviour(project: dict, layer: dict) -> None:
    st.markdown("##### Categoría y comportamiento")
    col1, col2 = st.columns(2)
    category = col1.selectbox(
        "Categoría",
        CATEGORY_KEYS,
        index=CATEGORY_KEYS.index(layer["category"]),
        format_func=lambda key: CATEGORIES[key],
        key=f"lcat-{layer['id']}",
    )
    name = col2.text_input("Nombre", layer["name"], key=f"lname-{layer['id']}")

    col1, col2, col3 = st.columns(3)
    visible = col1.checkbox("Visible", layer["visible"], key=f"vis-{layer['id']}")
    locked = col1.checkbox(
        "Bloqueada (píxeles)",
        layer["locked"],
        key=f"lock-{layer['id']}",
        help="Se renderiza siempre desde su PNG original, sin deformar ni regenerar.",
    )
    movable = col2.checkbox("Puede moverse", layer["movable"], key=f"mov-{layer['id']}")
    resizable = col2.checkbox("Puede escalarse", layer["resizable"], key=f"res-{layer['id']}")
    reorderable = col3.checkbox(
        "Puede reordenarse", layer["reorderable"], key=f"reo-{layer['id']}"
    )
    replaceable = col3.checkbox(
        "Puede reemplazarse", layer["replaceable"], key=f"rep-{layer['id']}"
    )
    preserve = st.checkbox(
        "Mantener relación de aspecto",
        layer["preserve_aspect_ratio"],
        key=f"par-{layer['id']}",
    )

    payload = {
        "id": layer["id"],
        "category": category,
        "name": name,
        "visible": visible,
        "locked": locked,
        "movable": movable,
        "resizable": resizable,
        "reorderable": reorderable,
        "replaceable": replaceable,
        "preserve_aspect_ratio": preserve,
    }

    if layer["type"] == "text" or category == "legal":
        st.markdown("##### Contenido editable")
        if layer["type"] == "image":
            st.caption(
                "El PNG conserva el legal exactamente como llegó. Este contenido se "
                "usa solo en el SVG para Illustrator y queda en una capa aparte."
            )
        content = st.text_area(
            "Texto",
            layer.get("content") or (layer.get("meta") or {}).get("editable_content") or "",
            key=f"txt-{layer['id']}",
            height=90,
        )
        col1, col2, col3 = st.columns(3)
        font_size = col1.number_input(
            "Tamaño", 8, 400, int(layer.get("font_size") or 48), key=f"fs-{layer['id']}"
        )
        weight = col2.selectbox(
            "Peso",
            ["normal", "bold"],
            index=0 if layer.get("font_weight") == "normal" else 1,
            key=f"fw-{layer['id']}",
        )
        align = col3.selectbox(
            "Alineación",
            ["left", "center", "right"],
            index=["left", "center", "right"].index(layer.get("text_align", "left")),
            key=f"al-{layer['id']}",
        )
        col1, col2 = st.columns(2)
        color = col1.color_picker("Color", layer.get("color", "#FFFFFF"), key=f"col-{layer['id']}")
        auto_contrast = col2.checkbox(
            "Ajustar color automáticamente por contraste",
            layer.get("auto_contrast", True),
            key=f"ac-{layer['id']}",
        )
        payload.update(
            {
                "content": content,
                "font_size": int(font_size),
                "font_weight": weight,
                "text_align": align,
                "color": color,
                "auto_contrast": auto_contrast,
            }
        )
        if category == "legal":
            export_as_text = st.checkbox(
                "Exportar como texto editable en SVG / Illustrator",
                value=bool(layer.get("export_as_text") or layer["type"] == "text"),
                disabled=not bool(content.strip()),
                key=f"export-text-{layer['id']}",
            )
            text_verified = st.checkbox(
                "Confirmo que el texto fue revisado y es exacto",
                value=bool(layer.get("text_verified", False)),
                disabled=not bool(content.strip()),
                key=f"verified-text-{layer['id']}",
                help="Los textos legales detectados por OCR deben revisarse antes de publicar.",
            )
            payload.update(
                {
                    "export_as_text": export_as_text,
                    "text_verified": text_verified,
                }
            )

    col1, col2 = st.columns(2)
    if col1.button("💾 Guardar capa", type="primary", key=f"save-{layer['id']}"):
        _update(project, [payload])
    if col2.button("🗑 Eliminar capa", key=f"drop-{layer['id']}"):
        try:
            api.update_layers(project["project_id"], delete=[layer["id"]])
            st.session_state.pop("selected_layer_id", None)
            refresh_project()
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)


def _order_editor(project: dict) -> None:
    st.markdown("##### Orden de capas (arriba = al frente)")
    layers = sorted(
        [layer for layer in project["layers"] if layer["category"] != "background"],
        key=lambda item: item["z_index"],
        reverse=True,
    )
    for index, layer in enumerate(layers):
        col1, col2, col3 = st.columns([6, 1, 1])
        col1.write(f"`z={layer['z_index']}` {layer_label(layer)}")
        if col2.button("⬆", key=f"up-{layer['id']}", disabled=index == 0):
            _swap(project, layers, index, index - 1)
        if col3.button("⬇", key=f"down-{layer['id']}", disabled=index == len(layers) - 1):
            _swap(project, layers, index, index + 1)


def _swap(project: dict, layers: list[dict], first: int, second: int) -> None:
    order = [layer["id"] for layer in layers]
    order[first], order[second] = order[second], order[first]
    order.reverse()  # la API espera de abajo hacia arriba
    try:
        api.update_layers(project["project_id"], order=order)
        refresh_project()
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        show_error(exc)


def _create_layer_form(project: dict) -> None:
    canvas = project["canvas"]
    st.markdown("##### Crear capa manualmente")
    with st.form("new-layer"):
        col1, col2 = st.columns(2)
        name = col1.text_input("Nombre", "Nueva capa")
        category = col2.selectbox(
            "Categoría", CATEGORY_KEYS[:-1], format_func=lambda key: CATEGORIES[key]
        )
        layer_type = col1.radio("Tipo", ["image", "text"], horizontal=True)
        locked = col2.checkbox("Bloquear píxeles", value=category in {"logo", "product", "person"})
        col1, col2, col3, col4 = st.columns(4)
        x = col1.number_input("X", 0, canvas["width"] - 1, int(canvas["width"] * 0.1))
        y = col2.number_input("Y", 0, canvas["height"] - 1, int(canvas["height"] * 0.1))
        width = col3.number_input("Ancho", 4, canvas["width"], int(canvas["width"] * 0.3))
        height = col4.number_input("Alto", 4, canvas["height"], int(canvas["height"] * 0.2))
        content = st.text_input("Texto (solo capas de texto)", "")
        auto_segment = st.checkbox(
            "Segmentar automáticamente dentro del rectángulo (capas imagen)", value=True
        )
        created = st.form_submit_button("Crear capa", type="primary")

    if created:
        payload = {
            "name": name,
            "category": category,
            "type": layer_type,
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
            "locked": locked,
            "auto_segment": auto_segment,
        }
        if layer_type == "text":
            payload["content"] = content or name
        try:
            layer = api.create_layer(project["project_id"], payload)
            st.session_state.selected_layer_id = layer["id"]
            refresh_project()
            st.success(f"Capa creada: {layer['name']}")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)


def _extract_and_background(project: dict) -> None:
    st.markdown("##### Extracción y fondo")
    col1, col2, col3 = st.columns([1, 1, 2])
    feather = col1.slider("Suavizado de bordes", 0, 12, 2)
    if col2.button("📤 Extraer todas las capas", type="primary"):
        try:
            with st.spinner("Extrayendo PNG transparentes…"):
                result = api.extract(project["project_id"], feather=feather, force=True)
            refresh_project()
            st.success(f"{len(result['extracted'])} capas extraídas.")
            for warning in result.get("warnings", []):
                st.warning(warning)
        except Exception as exc:  # noqa: BLE001
            show_error(exc)

    dilate = col3.slider(
        "Expansión de la máscara al reconstruir el fondo", 0, 40, 8,
        help="Más expansión borra mejor los bordes, pero reconstruye más superficie.",
    )
    models = api.image_models()
    by_id = {model["id"]: model for model in models}
    engine_options = ["auto"] + (["magnific"] if models else []) + ["opencv"]
    engine = st.selectbox(
        "Motor de reconstrucción",
        engine_options,
        format_func=lambda key: {
            "auto": "Automático (el mejor disponible)",
            "magnific": "Magnific · elegir modelo",
            "opencv": "Local · OpenCV (gratis)",
        }[key],
    )
    model_id = None
    if engine == "magnific" and by_id:
        model_id = st.selectbox(
            "Modelo de IA",
            list(by_id),
            format_func=lambda key: by_id[key]["label"],
        )
        st.caption(by_id[model_id].get("description", ""))
    prompt = st.text_input(
        "Instrucción para el inpainting (solo con IA)",
        placeholder="fondo limpio de estudio, sin objetos ni texto",
    )
    if st.button("🩹 Reconstruir fondo"):
        try:
            with st.spinner("Reconstruyendo el fondo…"):
                result = api.reconstruct_background(
                    project["project_id"],
                    prompt=prompt or None,
                    dilate=dilate,
                    provider=engine,
                    model=model_id,
                )
            refresh_project()
            st.success(f"Fondo reconstruido con `{result['provider']}`.")
            for warning in result.get("warnings", []):
                st.warning(warning)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)

    background = project.get("background", {})
    if background.get("path"):
        try:
            st.image(
                cached_file(project["project_id"], background["path"], token()),
                caption=f"Fondo reconstruido ({background.get('provider')})",
                width="stretch",
            )
        except Exception as exc:  # noqa: BLE001
            show_error(exc)


def _update(project: dict, updates: list[dict]) -> None:
    try:
        api.update_layers(project["project_id"], updates=updates)
        refresh_project()
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        show_error(exc)


def _mask_op(project: dict, payload: dict) -> None:
    try:
        with st.spinner("Actualizando máscara…"):
            api.edit_mask(project["project_id"], payload)
        refresh_project()
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        show_error(exc)


def render() -> None:
    st.header("Corregir elementos")
    project = require_project()
    if not project:
        return

    st.caption(
        "Ajuste máscaras, categorías, comportamiento y orden. Las capas bloqueadas "
        "conservan sus píxeles originales en todas las variantes."
    )

    layers = [layer for layer in project.get("layers", []) if layer["category"] != "background"]
    if not layers:
        st.info("No hay capas. Use \"Revisar lo detectado\" o cree una capa a mano.")
        _create_layer_form(project)
        return

    left, right = st.columns([3, 3])
    with left:
        layer = _selected_layer(project)
        if layer:
            try:
                preview = cached_mask(project["project_id"], layer["id"], token())
                st.image(
                    preview,
                    caption=f"Máscara de «{layer['name']}» (verde = incluido)",
                    width="stretch",
                )
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
            if layer.get("src"):
                with st.expander("PNG extraído"):
                    try:
                        st.image(
                            cached_file(project["project_id"], layer["src"], token()),
                            caption=f"{layer['width']}×{layer['height']} px · RGBA",
                        )
                    except Exception as exc:  # noqa: BLE001
                        show_error(exc)
            else:
                st.caption("Esta capa todavía no tiene PNG extraído.")
            for warning in layer.get("warnings", []):
                st.warning(warning)

    with right:
        if layer:
            _geometry_and_mask(project, layer)
            st.divider()
            _behaviour(project, layer)

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        _order_editor(project)
    with col2:
        _create_layer_form(project)

    st.divider()
    _extract_and_background(project)
