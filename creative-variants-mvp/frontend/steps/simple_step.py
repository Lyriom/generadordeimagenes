"""Pantalla principal: elegir el arte, elegir tamaños, generar.

Todo el proceso (detectar, recortar, fondo, componer) lo hace el backend en una
sola llamada: POST /projects/{id}/auto. Aquí no hay lógica de imagen.
"""
from __future__ import annotations

import time
import streamlit as st

import api_client as api

from . import results_step
from .common import (
    cached_file,
    cached_piece_preview,
    cached_pieces,
    refresh_project,
    show_error,
    token,
)

ALLOWED_ARTWORK = ["psd"]

FORMAT_LABELS = {
    "1080x1080": "Cuadrado · feed (1080×1080)",
    "1080x1350": "Vertical · feed (1080×1350)",
    "1080x1920": "Historia / Reel (1080×1920)",
    "1920x1080": "Horizontal · YouTube (1920×1080)",
    "900x660": "Banner 900×660",
    "900x1350": "Banner 900×1350",
    "1200x400": "Banner 1200×400",
}
SOCIAL = ["1080x1080", "1080x1350", "1080x1920"]

SIZE_CHOICES = {
    "auto": "El tamaño del original + los de redes",
    "social": "Solo redes sociales",
    "custom": "Yo elijo los tamaños",
}

INTENSITY_LABELS = {
    "conservative": "Parecidas al original",
    "moderate": "Equilibradas",
    "creative": "Muy distintas entre sí",
}


def _file_tuple(uploaded, default_type: str = "image/png"):
    if uploaded is None:
        return None
    return (uploaded.name, uploaded.getvalue(), uploaded.type or default_type)


def _open(project: dict, message: str) -> None:
    _open_many([project], message)


def _open_many(projects: list[dict], message: str) -> None:
    if not projects:
        return
    project = projects[0]
    st.session_state.project_id = project["project_id"]
    st.session_state.project = project
    st.session_state.campaign_projects = projects
    st.session_state.selected_variants = []
    st.session_state.auto_steps = []
    st.session_state.layer_reviews = {}
    st.session_state.cache_token += 1
    st.session_state.flash = message
    st.rerun()


def _choose_pieces(sources: list[str]) -> dict[str, list[int] | None]:
    """Detecta las piezas de cada PSD y deja marcar cuáles importar.

    Devuelve {ruta: índices} y `None` cuando el PSD trae una sola pieza, que es
    la forma de decirle al backend "todas". Los archivos sin ninguna pieza
    marcada se quedan fuera del diccionario.
    """
    selection: dict[str, list[int] | None] = {}
    for source in sources:
        try:
            with st.spinner(f"Analizando {source}…"):
                info = cached_pieces(source)
        except Exception as exc:  # noqa: BLE001 - un archivo ilegible no bloquea el resto
            st.warning(f"{source}: no se pudieron detectar las piezas ({exc}).")
            selection[source] = None
            continue

        pieces = info.get("pieces") or []
        if len(pieces) <= 1:
            selection[source] = None
            continue

        with st.expander(f"📐 {source} · {len(pieces)} piezas detectadas", expanded=True):
            st.caption(
                "El PSD trae varias piezas en el mismo lienzo. Cada una marcada se "
                "convierte en una plantilla independiente con sus capas reales."
            )
            chosen: list[int] = []
            columns = st.columns(min(4, len(pieces)))
            for position, piece in enumerate(pieces):
                column = columns[position % len(columns)]
                with column:
                    try:
                        st.image(
                            cached_piece_preview(source, piece["index"]),
                            width="stretch",
                        )
                    except Exception:  # noqa: BLE001 - la miniatura es un extra
                        st.caption("(sin vista previa)")
                    marked = st.checkbox(
                        f"{piece['name'][:22]} · {piece['width']}×{piece['height']}",
                        value=True,
                        key=f"pieza-{source}-{piece['index']}",
                    )
                    if marked:
                        chosen.append(piece["index"])
            if chosen:
                selection[source] = chosen
    return selection


# ------------------------------------------------------------------- 1 · el arte
def _pick_artwork() -> None:
    st.subheader("1 · Carga los KV de la campaña")
    st.caption(
        "Puedes subir varios PSD. Cada uno se convierte en una plantilla editable y "
        "participa en la producción de la campaña."
    )

    subir, carpeta, anterior = st.tabs(
        ["Subir PSD", "Carpeta compartida", "Abrir un trabajo anterior"]
    )

    with subir:
        uploaded = st.file_uploader(
            "KV maestros (PSD)", type=ALLOWED_ARTWORK, label_visibility="collapsed",
            accept_multiple_files=True,
        )
        st.caption("Hasta 300 MB. Para archivos más pesados usa la carpeta compartida.")
        with st.expander("Recursos de marca (opcional)"):
            logo = st.file_uploader("Logo en PNG", type=["png", "jpg", "jpeg"], key="logo-up")
            font = st.file_uploader("Tipografía .ttf u .otf", type=["ttf", "otf"], key="font-up")
            st.caption(
                "La tipografía tiene que ser el **archivo** de la fuente (.ttf u .otf): "
                "no un enlace ni un .rtf. Y solo se usa si hay que volver a dibujar los "
                "textos — si vienen del PSD se conservan tal cual y no hace falta."
            )
        st.caption(
            "Si un PSD trae varias piezas (varios avisos en el mismo lienzo), se "
            "detectan solas y cada una se convierte en una plantilla independiente."
        )
        if st.button("Crear campaña con estos KV", type="primary", disabled=not uploaded):
            try:
                projects = []
                with st.status(f"Importando {len(uploaded)} KV…", expanded=True):
                    for index, artwork in enumerate(uploaded):
                        st.write(f"{index + 1}/{len(uploaded)} · {artwork.name}")
                        result = api.create_projects_split(
                            name=artwork.name.rsplit(".", 1)[0][:120],
                            artwork=_file_tuple(artwork),
                            logo=_file_tuple(logo),
                            font=_file_tuple(font, "font/ttf"),
                        )
                        detected = result.get("pieces_detected", 1)
                        if detected > 1:
                            st.write(f"　　↳ {detected} piezas detectadas en el pliego")
                        projects.extend(result.get("projects", []))
                        for warning in result.get("warnings", []):
                            st.caption(f"⚠️ {warning}")
                _open_many(
                    projects,
                    f"{len(projects)} plantilla(s) lista(s). Continúa en el punto 2.",
                )
            except Exception as exc:  # noqa: BLE001
                show_error(exc)

    with carpeta:
        try:
            listing = api.list_ingest()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
            return
        st.caption(
            f"Copia los archivos en `{listing['directory']}` y aparecerán aquí. "
            "Es la vía recomendada para los PSD de 60–100 MB: no pasan por el navegador."
        )
        files = [item for item in listing.get("files", []) if item["format"] == "PSD"]
        if not files:
            st.info("La carpeta está vacía.")
        else:
            labels = {
                item["path"]: f"{item['path']}  ·  {item['format']} "
                f"{item['width']}×{item['height']}  ·  {item['size_mb']} MB"
                for item in files
            }
            sources = st.multiselect(
                "Archivos PSD", list(labels), default=list(labels),
                format_func=lambda key: labels[key]
            )
            selection = _choose_pieces(sources)
            total = sum(
                len(chosen) if chosen is not None else 1 for chosen in selection.values()
            )
            if st.button(
                f"Crear campaña con {total} plantilla(s)", type="primary", key="use-ingest",
                disabled=not selection,
            ):
                try:
                    projects = []
                    with st.status(f"Importando {total} plantilla(s)…", expanded=True):
                        for index, source in enumerate(selection):
                            st.write(f"{index + 1}/{len(selection)} · {source}")
                            result = api.create_projects_from_ingest_split(
                                source=source, kv=None, pieces=selection[source],
                            )
                            projects.extend(result.get("projects", []))
                            for warning in result.get("warnings", []):
                                st.caption(f"⚠️ {warning}")
                    _open_many(
                        projects,
                        f"{len(projects)} plantilla(s) lista(s). Continúa en el punto 2.",
                    )
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)

    with anterior:
        try:
            projects = api.list_projects()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
            projects = []
        if not projects:
            st.caption("Todavía no hay trabajos guardados.")
        for item in projects:
            col1, col2, col3, col4 = st.columns([4, 3, 1, 1])
            col1.write(f"**{item['name']}**")
            col2.caption(
                f"{item['canvas']['width']}×{item['canvas']['height']} · "
                f"{item['variants']} variantes"
            )
            if col3.button("Abrir", key=f"open-{item['project_id']}"):
                st.session_state.project_id = item["project_id"]
                project = refresh_project()
                _open(project, f"Abierto: {project['name']}")
            if col4.button("Borrar", key=f"del-{item['project_id']}"):
                try:
                    api.delete_project(item["project_id"])
                    if st.session_state.get("project_id") == item["project_id"]:
                        st.session_state.project_id = None
                        st.session_state.project = None
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)


def _artwork_cards(projects: list[dict]) -> None:
    """Resumen compacto de todos los KV que forman la campaña."""
    header, action = st.columns([5, 1])
    header.subheader(f"Campaña · {len(projects)} KV")
    if action.button("Cambiar KV", width="stretch"):
        st.session_state.project = None
        st.session_state.project_id = None
        st.session_state.campaign_projects = []
        st.session_state.auto_steps = []
        st.rerun()

    columns = st.columns(min(4, len(projects)))
    for index, project in enumerate(projects):
        elements = [
            layer for layer in project.get("layers", [])
            if layer["category"] != "background"
        ]
        with columns[index % len(columns)]:
            try:
                st.image(
                    cached_file(project["project_id"], project["source"]["path"], token()),
                    width="stretch",
                )
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
            st.markdown(f"**{project['name']}**")
            st.caption(
                f"{project['canvas']['width']}×{project['canvas']['height']} · "
                f"{len(elements)} capas"
            )

    active_ids = [project["project_id"] for project in projects]
    active_id = st.selectbox(
        "KV activo para Ajustes finos",
        active_ids,
        format_func=lambda value: next(
            project["name"] for project in projects if project["project_id"] == value
        ),
    )
    active = next(project for project in projects if project["project_id"] == active_id)
    st.session_state.project_id = active_id
    st.session_state.project = active


# ------------------------------------------------- cambiar el producto de un KV
CATEGORY_NAMES = {
    "product": "Producto",
    "person": "Persona",
    "logo": "Logo",
    "decoration": "Decoración",
    "headline": "Titular",
    "subheadline": "Subtítulo",
    "price": "Precio",
    "cta": "CTA",
    "legal": "Legal",
}

LAYER_ROLES = {
    "product": "Producto original · eliminar y reemplazar",
    "logo": "Obligatorio · logo",
    "headline": "Obligatorio · titular",
    "subheadline": "Obligatorio · subtítulo",
    "price": "Obligatorio · precio o descuento",
    "cta": "Obligatorio · CTA",
    "legal": "Obligatorio · legal",
    "decoration": "Decoración · conservar",
    "background": "Parte del fondo · no recomponer como capa",
    "ignore": "No usar",
}

MANDATORY_ROLES = {"logo", "headline", "subheadline", "price", "cta", "legal"}


def _review_layers(projects: list[dict]) -> tuple[list[dict], bool]:
    """Confirmación humana de las capas del PSD antes de producir el lote."""
    st.subheader("2 · Revisa las capas de cada KV")
    st.caption(
        "Define qué se elimina y qué debe conservarse exactamente. Esta revisión evita "
        "que una prenda, un sello o un copy mal nombrado se interpreten de forma incorrecta."
    )
    reviews = st.session_state.setdefault("layer_reviews", {})
    project_ids = [item["project_id"] for item in projects]
    # La confirmación también vive en las capas del proyecto, no solo en la sesión
    # del navegador. Así sobrevive a recargas y a volver a abrir la campaña.
    for item in projects:
        reviewable = [
            layer for layer in item.get("layers", [])
            if not layer.get("meta", {}).get("external")
        ]
        if reviewable and all(
            layer.get("meta", {}).get("role_confirmed") for layer in reviewable
        ):
            reviews[item["project_id"]] = True

    next_review_id = st.session_state.pop("_next_review_id", None)
    if next_review_id in project_ids:
        st.session_state["review-kv"] = next_review_id
    review_id = st.selectbox(
        "KV que vas a revisar",
        project_ids,
        format_func=lambda value: next(
            item["name"] for item in projects if item["project_id"] == value
        ),
        key="review-kv",
    )
    project = next(item for item in projects if item["project_id"] == review_id)
    layers = [
        layer for layer in project.get("layers", [])
        if not layer.get("meta", {}).get("external")
    ]

    with st.expander(f"Capas de {project['name']} · {len(layers)}", expanded=True):
        with st.form(f"layer-review-form-{review_id}"):
            selections: dict[str, str] = {}
            for index, layer in enumerate(sorted(layers, key=lambda item: item["z_index"])):
                preview, control = st.columns([1, 4], vertical_alignment="center")
                with preview:
                    if layer.get("src"):
                        try:
                            st.image(
                                cached_file(review_id, layer["src"], token()),
                                width="stretch",
                            )
                        except Exception:  # noqa: BLE001
                            st.caption("Sin vista previa")
                with control:
                    st.caption(
                        f"Capa {index + 1} · {layer['name']} · "
                        f"{layer['width']}×{layer['height']} px"
                    )
                    default = layer.get("category", "decoration")
                    if not layer.get("visible", True):
                        default = "ignore"
                    options = list(LAYER_ROLES)
                    selections[layer["id"]] = st.selectbox(
                        "Función",
                        options,
                        index=options.index(default) if default in options else options.index("decoration"),
                        format_func=lambda value: LAYER_ROLES[value],
                        key=f"role-{review_id}-{layer['id']}",
                        label_visibility="collapsed",
                    )
                st.divider()
            confirmed = st.form_submit_button(
                "Guardar y confirmar estas capas", type="primary", width="stretch"
            )

        if confirmed:
            try:
                updates = []
                for layer in layers:
                    role = selections[layer["id"]]
                    ignored = role == "ignore"
                    category = "decoration" if ignored else role
                    updates.append(
                        {
                            "id": layer["id"],
                            "category": category,
                            "visible": not ignored and role != "background",
                            "locked": role in MANDATORY_ROLES,
                            "replaceable": role == "product",
                            "preserve_aspect_ratio": True,
                        }
                    )
                api.update_layers(review_id, updates=updates)
                reviews[review_id] = True
                pending_after_save = [
                    item["project_id"]
                    for item in projects
                    if item["project_id"] != review_id
                    and not reviews.get(item["project_id"])
                ]
                if pending_after_save:
                    st.session_state["_next_review_id"] = pending_after_save[0]
                    st.session_state["_layer_review_flash"] = (
                        f"Capas de {project['name']} confirmadas. "
                        "Avanzamos al siguiente KV."
                    )
                else:
                    st.session_state["_layer_review_flash"] = (
                        "Todas las capas de la campaña quedaron confirmadas."
                    )
                refreshed = [api.get_project(item["project_id"]) for item in projects]
                st.session_state.campaign_projects = refreshed
                st.session_state.project = next(
                    item for item in refreshed if item["project_id"] == review_id
                )
                st.session_state.cache_token += 1
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                show_error(exc)

    review_flash = st.session_state.pop("_layer_review_flash", None)
    if review_flash:
        st.success(review_flash)

    done = sum(bool(reviews.get(project_id)) for project_id in project_ids)
    if done == len(project_ids):
        st.success(f"Capas confirmadas en los {done} KV.")
    else:
        pending = [
            item["name"] for item in projects if not reviews.get(item["project_id"])
        ]
        st.warning(f"Falta confirmar {len(pending)} KV: " + ", ".join(pending))
    return projects, done == len(project_ids)


def _product_batch(projects: list[dict]):
    """Recoge productos para reconstruir piezas desde la plantilla del KV."""
    st.subheader("3 · Carga los productos")
    with st.container(border=True):
        st.caption(
            "El sistema quitará todos los productos originales del KV. El PSD quedará "
            "como plantilla editable y cada PNG nuevo generará una composición distinta."
        )
        try:
            target_by_project = {}
            original_count = 0
            missing = []
            for project in projects:
                candidates = api.replaceable_layers(project["project_id"])["layers"]
                product_candidates = [
                    item for item in candidates if item.get("category") == "product"
                ]
                if not product_candidates:
                    missing.append(project["name"])
                    continue
                target_by_project[project["project_id"]] = product_candidates[0]["id"]
                original_count += len(product_candidates)
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
            return {"individual": [], "groups": [], "valid": False}, {}
        if missing:
            st.info(
                "Estos KV no tienen una capa identificada como Producto: "
                + ", ".join(missing)
                + ". Selecciónalos arriba y corrígelos en **Ajustes finos**."
            )
        if not target_by_project:
            return {"individual": [], "groups": [], "valid": False}, {}

        st.success(
            f"{len(target_by_project)} KV listos: se retirarán {original_count} "
            "producto(s) original(es)."
        )
        uploads = st.file_uploader(
            "Catálogo de productos recortados (PNG)",
            type=["png"],
            accept_multiple_files=True,
            key="product-catalog-up",
            help=(
                "Sube una sola vez todos los productos. Luego puedes generar cada "
                "uno por separado y también combinarlos libremente."
            ),
        ) or []
        names = [item.name for item in uploads]
        by_name = {item.name: item for item in uploads}
        individual_names = st.multiselect(
            "Productos que también tendrán un arte individual",
            names,
            default=names,
            key="individual-product-selection",
        )
        individual = [by_name[name] for name in individual_names]

        groups: list[tuple[str, list]] = []
        groups_valid = True
        with st.expander("Combinar cualquier producto en una misma arte", expanded=False):
            if uploads:
                group_count = int(
                    st.number_input(
                        "Cantidad de combinaciones",
                        min_value=0,
                        max_value=max(1, len(uploads) * 2),
                        value=0,
                        step=1,
                        help=(
                            "Cada combinación genera un arte adicional. Un producto "
                            "puede aparecer en más de una combinación."
                        ),
                    )
                )
                for group_index in range(group_count):
                    selected = st.multiselect(
                        f"Combinación {group_index + 1}",
                        names,
                        default=[],
                        key=f"free-product-group-{group_index}",
                    )
                    if len(selected) >= 2:
                        groups.append(
                            (
                                f"Combinación {group_index + 1}",
                                [by_name[name] for name in selected],
                            )
                        )
                    elif selected:
                        st.warning(
                            f"La combinación {group_index + 1} necesita al menos 2 productos."
                        )
                if len(groups) != group_count:
                    groups_valid = False
                st.caption(
                    f"{len(groups)} de {group_count} combinación(es) listas."
                )

        batch = {
            "individual": individual,
            "groups": groups,
            "valid": groups_valid and bool(individual or groups),
        }
        # La capa de producto más grande sirve como soporte interno del nuevo recorte;
        # todas las originales se ocultan automáticamente.
        return batch, target_by_project


# ---------------------------------------------------------------- 2 y 3 · generar
def _sizes() -> list[str] | None:
    """Devuelve la lista de formatos, o None para dejar que el backend elija."""
    st.markdown("**Formatos de salida**")
    choice = st.radio(
        "Tamaños",
        list(SIZE_CHOICES),
        format_func=lambda key: SIZE_CHOICES[key],
        label_visibility="collapsed",
        horizontal=True,
    )
    if choice == "auto":
        st.caption(
            "Recomendado: mantiene la proporción del arte y añade feed cuadrado y vertical."
        )
        return None
    if choice == "social":
        return SOCIAL
    chosen = st.multiselect(
        "Tamaños a generar",
        list(FORMAT_LABELS),
        default=SOCIAL[:2],
        format_func=lambda key: FORMAT_LABELS[key],
    )
    return chosen or None



def _poll_task(project_id: str, task_id: str, progress_text: str):
    progress_bar = st.progress(0, text=progress_text)
    while True:
        status = api.get_task_status(project_id, task_id)
        if status["state"] == "COMPLETED":
            progress_bar.progress(1.0, text=f"{progress_text} (Completado)")
            time.sleep(0.5)
            progress_bar.empty()
            return status["result"]
        elif status["state"] == "FAILED":
            progress_bar.empty()
            raise Exception("La tarea asíncrona falló.")
        elif status["state"] == "PROGRESS":
            meta = status["meta"] or {}
            progress_bar.progress(meta.get("progress", 0) / 100.0, text=meta.get("status", progress_text))
        time.sleep(2)

def _generate(projects: list[dict], product_batch: dict, targets: dict[str, str]) -> None:
    st.subheader("4 · Elige formatos y genera")
    individual = product_batch.get("individual", [])
    groups = product_batch.get("groups", [])
    st.caption(
        f"Se producirán {len(individual)} pieza(s) individuales y "
        f"{len(groups)} pieza(s) grupales por cada KV."
    )
    formats = _sizes()
    count = st.slider(
        "Propuestas finales",
        2,
        6,
        3,
        help="Se prueban más composiciones internamente y solo se conservan las mejores.",
    )

    try:
        provider_status = api.health().get("providers", {}).get("inpainting", {})
    except Exception:  # noqa: BLE001
        provider_status = {}
    openai_ready = bool(provider_status.get("openai_available"))
    magnific_ready = bool(provider_status.get("magnific_available"))
    magnific_models = provider_status.get("magnific_models") or []
    background_provider = "opencv"
    background_model = None
    background_prompt = None

    with st.expander("Reconstrucción del fondo y variación"):
        st.caption(
            "La IA reconstruye únicamente los huecos del fondo. La ubicación de los "
            "productos se toma de la zona diseñada en el PSD y no se delega a la IA."
        )
        engines = []
        if magnific_ready:
            engines.append("magnific")
        if openai_ready:
            engines.append("openai")
        engines.append("local")
        background_mode = st.radio(
            "Motor del fondo",
            engines,
            format_func=lambda value: {
                "local": "Local · gratis",
                "magnific": "Magnific · eliges el modelo de IA (con costo)",
                "openai": "OpenAI · IA de imagen (con costo)",
            }[value],
            horizontal=True,
        )
        if not magnific_ready:
            st.caption(
                "Para habilitar Magnific, pega la clave en `MAGNIFIC_API_KEY=` dentro "
                "de `.env` y reinicia los contenedores."
            )
        if background_mode == "magnific":
            background_provider = "magnific"
            by_id = {model["id"]: model for model in magnific_models}
            options = list(by_id)
            preferred = provider_status.get("magnific_model")
            index = options.index(preferred) if preferred in options else 0
            background_model = st.selectbox(
                "Modelo de IA",
                options,
                index=index,
                format_func=lambda key: by_id[key]["label"],
                help="Todos usan la misma clave de Magnific; cambia el costo y el estilo.",
            )
            chosen = by_id.get(background_model, {})
            st.caption(chosen.get("description", ""))
            if chosen.get("supports_mask"):
                st.success(
                    "Repinta solo el hueco de los productos: el resto del arte queda "
                    "idéntico al original."
                )
            else:
                st.info(
                    "Este modelo regenera la imagen completa. El resultado se recompone "
                    "solo dentro de la zona borrada, así el resto del KV no se altera."
                )
        elif background_mode == "openai":
            background_provider = "openai"
            st.success(
                f"OpenAI listo · modelo {provider_status.get('openai_model', 'gpt-image-2')}"
            )
        if background_mode in {"magnific", "openai"}:
            background_prompt = st.text_area(
                "Dirección visual para el fondo",
                placeholder=(
                    "Ej.: fondo deportivo premium, luces azules, volumen, profundidad, "
                    "sin texto, sin logos y con espacio para el producto"
                ),
                help=(
                    "El fondo se genera una sola vez por lote. Producto, logo y copy se "
                    "componen después con sus archivos reales."
                ),
            ) or None
        intensity = st.radio(
            "Cuánto se pueden alejar del original",
            list(INTENSITY_LABELS),
            index=0,
            format_func=lambda key: INTENSITY_LABELS[key],
            horizontal=True,
        )
        instruction = st.text_input(
            "Pedido en palabras (opcional)",
            placeholder="producto grande y titular arriba",
            help="Entiende: producto grande/pequeño, titular arriba, centrado, "
            "vertical, diagonal, dividido, izquierda, derecha, minimal.",
        )
        seed = st.number_input(
            "Semilla", 0, 2**31 - 1, 42, help="La misma semilla repite el mismo resultado."
        )

    if st.button(
        "✨ Generar artes por producto",
        type="primary",
        width="stretch",
        disabled=not product_batch.get("valid") or not targets,
    ):
        try:
            total = (len(individual) + len(groups)) * len(targets)
            label = f"Produciendo {total} tanda(s) desde {len(targets)} KV…"
            with st.status(label, expanded=True) as box:
                all_steps, all_warnings = [], []
                completed = 0
                for kv_index, project in enumerate(projects):
                    target = targets.get(project["project_id"])
                    if not target:
                        continue
                    first_batch = True
                    for product_index, product in enumerate(individual):
                        completed += 1
                        st.write(
                            f"{completed}/{total} · {project['name']} · {product.name}"
                        )
                        replaced = api.replace_product(
                            project["project_id"], image=_file_tuple(product),
                            layer_id=target, hide_others=True,
                        )
                        task = api.auto_generate(
                            project["project_id"], count=int(count), formats=formats,
                            intensity=intensity, instruction=instruction or None,
                            seed=int(seed) + kv_index * 100 + product_index,
                            replace_existing=first_batch,
                            product_label=product.name.rsplit(".", 1)[0],
                            template_mode=True,
                            background_provider=background_provider,
                            background_model=background_model,
                            background_prompt=background_prompt,
                            regenerate_background=(
                                background_provider in {"magnific", "openai"}
                                and first_batch
                            ),
                        )
                        result = _poll_task(project["project_id"], task["task_id"], f"Generando {product.name}...")
                        first_batch = False
                        all_steps.extend(result.get("steps", []))
                        all_warnings.extend(replaced.get("warnings", []))
                        all_warnings.extend(result.get("warnings", []))

                    for group_index, (group_name, group_products) in enumerate(groups):
                        completed += 1
                        st.write(
                            f"{completed}/{total} · {project['name']} · {group_name} "
                            f"({len(group_products)} productos)"
                        )
                        for member_index, product in enumerate(group_products):
                            replaced = api.replace_product(
                                project["project_id"], image=_file_tuple(product),
                                layer_id=target,
                                hide_others=member_index == 0,
                                append=member_index > 0,
                            )
                            all_warnings.extend(replaced.get("warnings", []))
                        group_label = " + ".join(
                            product.name.rsplit(".", 1)[0] for product in group_products
                        )
                        task = api.auto_generate(
                            project["project_id"], count=int(count), formats=formats,
                            intensity=intensity, instruction=instruction or None,
                            seed=int(seed) + kv_index * 100 + len(individual) + group_index,
                            replace_existing=first_batch,
                            product_label=group_label,
                            template_mode=True,
                            background_provider=background_provider,
                            background_model=background_model,
                            background_prompt=background_prompt,
                            regenerate_background=(
                                background_provider in {"magnific", "openai"}
                                and first_batch
                            ),
                        )
                        result = _poll_task(project["project_id"], task["task_id"], f"Generando {group_label}...")
                        first_batch = False
                        all_steps.extend(result.get("steps", []))
                        all_warnings.extend(result.get("warnings", []))
                box.update(label="Listo", state="complete", expanded=False)
            st.session_state.auto_steps = all_steps
            st.session_state.auto_warnings = list(dict.fromkeys(all_warnings))
            refreshed = [api.get_project(project["project_id"]) for project in projects]
            st.session_state.campaign_projects = refreshed
            active_id = st.session_state.project_id
            st.session_state.project = next(
                (item for item in refreshed if item["project_id"] == active_id),
                refreshed[0],
            )
            st.session_state.cache_token += 1
            st.session_state.selected_variants = []
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            show_error(exc)
            st.caption(
                "Revisa en **Ajustes finos** que la capa correcta esté categorizada "
                "como producto y marcada como reemplazable."
            )


def _report() -> None:
    """Qué hizo el sistema, en lenguaje llano."""
    steps = st.session_state.get("auto_steps") or []
    warnings = st.session_state.get("auto_warnings") or []
    if not steps:
        return
    with st.expander("Qué hizo el sistema", expanded=False):
        for step in steps:
            icon = "✅" if step.get("ok", True) else "⚠️"
            st.write(f"{icon} **{step['name']}** — {step['detail']}")
        for warning in warnings:
            st.caption(f"• {warning}")


def render() -> None:
    st.title("Generar artes desde varios KV")

    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)

    project = st.session_state.get("project")
    projects = st.session_state.get("campaign_projects") or ([project] if project else [])
    if not projects:
        st.caption(
            "Carga varios KV en PSD, añade productos y genera la campaña completa."
        )
        _pick_artwork()
        return

    _artwork_cards(projects)
    projects, layers_ready = _review_layers(projects)
    if not layers_ready:
        return
    st.divider()
    products, targets = _product_batch(projects)
    st.divider()
    _generate(projects, products, targets)
    _report()

    result_projects = st.session_state.get("campaign_projects") or projects
    if any(project.get("variants") for project in result_projects):
        st.divider()
        st.header("Resultados por KV")
        for result_project in result_projects:
            if not result_project.get("variants"):
                continue
            with st.expander(
                f"{result_project['name']} · {len(result_project['variants'])} artes",
                expanded=len(result_projects) == 1,
            ):
                results_step.gallery(
                    result_project, key_prefix=result_project["project_id"]
                )
