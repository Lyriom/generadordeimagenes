"""Convertir un KV importado en una plantilla de marca con campos.

Esto es lo que hoy vive en la cabeza de quien hace la tanda: que el precio
cambia en cada arte y el logo no, que el producto va en ese hueco y no en otro.
Se decide una vez por KV, se guarda, y cada tanda posterior es rellenar celdas.

Dos cosas que este módulo hace y conviene no perder de vista:

**Propone, no dicta.** Las categorías del PSD salen de nombres como «Capa 15» y
de la geometría, así que la lista de campos es una primera versión para revisar,
igual que «Revisar lo detectado». La API deja convertir cualquier capa fija en
campo y al revés.

**Vacía los campos de la plancha.** Es el paso que no se ve y sin el cual nada
de esto sirve: si el precio del KV original sigue pintado en el fondo, los
doscientos artes de la tanda llevan debajo el precio de otro producto. `art_text`
ya sabe hacerlo —es lo mismo que «Quitar del arte»— y aquí se usa tal cual. Como
efecto, **el proyecto de origen queda con esos elementos retirados**: es el mismo
resultado que pulsar «Quitar del arte» a mano, y se deshace igual.
"""
from __future__ import annotations

import logging

from PIL import Image

from ..models import Layer, LayerCategory, LayerType, Project
from ..models.template import (
    MIN_READABLE_FONT_SIZE,
    SLOT_SLUGS,
    Box,
    Slot,
    SlotKind,
    Template,
    TemplateLayer,
)
from . import art_text, layer_extraction, template_store
from .imaging import load_flat_rgb
from .layout_engine import MAX_UPSCALE

logger = logging.getLogger(__name__)

#: Lo que cambia de un arte al siguiente. El resto de categorías es la identidad
#: del KV y se congela: logo, decoración, persona y fondo.
SLOT_CATEGORIES = {
    LayerCategory.PRODUCT,
    LayerCategory.PRICE,
    LayerCategory.HEADLINE,
    LayerCategory.SUBHEADLINE,
    LayerCategory.CTA,
    LayerCategory.LEGAL,
}

#: Sin estos dos no hay arte de catálogo, así que una fila sin ellos se bloquea
#: antes de producir en vez de salir con un hueco. Los demás campos —titular,
#: subtítulo, CTA y legal— suelen ser de campaña, los mismos en toda la tanda:
#: son campo, pero opcional, y se rellenan una vez con su valor por defecto.
REQUIRED_SLOT_CATEGORIES = {LayerCategory.PRODUCT}

#: Relación entre el alto de tinta medido y el cuerpo de letra que lo produce.
#: Una mayúscula ocupa en torno al 72 % del cuerpo en las tipografías de palo
#: seco que usan estos artes. Es una semilla razonable, no una medida exacta: al
#: dibujar, `renderer.fit_text` ajusta el cuerpo a la caja de verdad.
CAP_HEIGHT_RATIO = 0.72

#: Lado mínimo de un campo, en fracción del lienzo. Por debajo de esto no es una
#: decisión sino un resbalón, y produciría un sello ilegible en todas las piezas.
MIN_SLOT_SIDE = 0.02

PLATE_REL = "plate.png"


def _slot_slug(category: LayerCategory, used: set[str]) -> str:
    """`precio`, y si ya había uno, `precio_2`. Nunca un uuid: es cabecera de CSV."""
    base = SLOT_SLUGS.get(category, "campo")
    if base not in used:
        used.add(base)
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    slug = f"{base}_{index}"
    used.add(slug)
    return slug


def _has_text(layer: Layer) -> bool:
    return bool((layer.content or "").strip() or layer.meta.get("editable_content"))


def candidate_layers(project: Project) -> list[Layer]:
    """Capas que pueden formar parte de una plantilla, de atrás hacia delante.

    Se excluyen el fondo (que pasa a ser la plancha) y lo que está oculto: si
    alguien ya quitó un elemento del KV, no lo quería en el arte y tampoco lo
    quiere en la plantilla.

    Una capa imagen entra aunque todavía no tenga PNG extraído: el recorte se
    hace al crear la plantilla. Exigirlo aquí dejaba fuera justo el caso normal
    del flujo manual, donde las capas se dibujan y se extraen después.
    """
    usable = [
        layer
        for layer in project.layers
        if layer.visible
        and layer.category != LayerCategory.BACKGROUND
        and (layer.type == LayerType.IMAGE or _has_text(layer))
    ]
    return sorted(usable, key=lambda layer: layer.z_index)


def _ensure_png(project: Project, layer: Layer) -> str | None:
    """Ruta relativa del PNG de la capa, extrayéndolo si aún no existe."""
    if layer.src:
        return layer.src
    if layer.type == LayerType.TEXT:
        return None
    ok, warning = layer_extraction.extract_layer(project, layer)
    if not ok:
        logger.info("No se pudo extraer '%s' para la plantilla: %s", layer.name, warning)
        return None
    return layer.src


def _text_slot_fields(project: Project, layer: Layer) -> tuple[dict, list[str]]:
    """Estilo con el que escribir el campo: el del arte, medido, no inventado."""
    warnings: list[str] = []
    if layer.type == LayerType.TEXT:
        return (
            {
                "default": (layer.content or "").strip() or None,
                "font_family": layer.font_family,
                "font_size": layer.font_size,
                "font_weight": layer.font_weight,
                "color": layer.color,
                "align": layer.text_align,
                "line_height": layer.line_height,
                "max_lines": 2,
            },
            warnings,
        )

    style = art_text.measure(project, layer)
    if style is None:
        return {}, [
            f"'{layer.name}' no se pudo medir como texto: queda como campo imagen. "
            "Se puede convertir a texto desde «Textos y logos del arte»."
        ]

    contenido = art_text.current_text(layer)
    if contenido:
        # Ni el peso ni el cuerpo se adivinan: se escribe el texto en cada cara
        # disponible y gana la que da el mismo grosor de palo que el original.
        # Es la misma decisión que toma `art_text.apply` al reescribir.
        peso, cuerpo = art_text.measured_face(project, style, contenido)
    else:
        warnings.append(
            f"No se conoce el texto original de '{layer.name}' (el PSD lo trae "
            "rasterizado): hay que escribirlo en la matriz."
        )
        # Sin texto no hay nada que escribir ni que comparar, así que el cuerpo
        # sale del alto de tinta medido: una mayúscula ocupa en torno al 72 %
        # del cuerpo en las tipografías de palo seco de estos artes. Es una
        # semilla; al dibujar, `renderer.fit_text` ajusta a la caja de verdad.
        peso = "normal"
        cuerpo = max(MIN_READABLE_FONT_SIZE, int(round(style.ink_height / CAP_HEIGHT_RATIO)))
    return (
        {
            "default": contenido or None,
            "font_size": cuerpo,
            "font_weight": peso,
            "color": style.color,
            "align": style.align,
            "line_height": style.line_height,
            "max_lines": max(1, style.lines),
        },
        warnings,
    )


def _freeze_plate(project: Project, template: Template) -> tuple[str | None, list[str]]:
    """Congela el fondo del KV dentro de la plantilla, al tamaño del lienzo.

    Se normaliza a PNG RGB del tamaño exacto del lienzo en vez de copiar el
    archivo: el fondo puede venir de una plancha reconstruida, del arte original
    aplanado o de un PSD, y la plantilla no debería tener que saber cuál.
    """
    from . import storage  # import local: evita un ciclo con storage en pruebas

    origen = project.background.path or project.source.path
    ruta = storage.abs_path(project.project_id, origen)
    if not ruta.exists():
        return None, ["El KV no tiene fondo guardado: la plantilla queda sin plancha."]
    try:
        plate = load_flat_rgb(ruta)
    except Exception as exc:  # noqa: BLE001 - un fondo ilegible no debe tumbar la creación
        return None, [f"No se pudo congelar la plancha: {exc}"]

    tamaño = (project.canvas.width, project.canvas.height)
    if plate.size != tamaño:
        plate = plate.resize(tamaño, Image.Resampling.LANCZOS)
    destino = template_store.template_path(template.brand_id, template.template_id, PLATE_REL)
    destino.parent.mkdir(parents=True, exist_ok=True)
    plate.save(destino, format="PNG", optimize=True)
    return PLATE_REL, []


#: Cuántas capas con categoría de campo hacen falta para dar la importación por
#: buena. Con menos, el PSD no dijo nada útil —sus capas se llaman «Capa 15» y
#: «Decoración 7»— y una plantilla con un solo campo obliga a marcar a mano el
#: precio, el titular y las condiciones en cada KV.
MIN_SLOT_LAYERS = 2


def _classify_with_vision(project: Project) -> list[str]:
    """Pide categorías a visión cuando el PSD no las trae.

    El clasificador ya existe y está probado (`semantic_layers`), pero solo
    corría al analizar un arte plano. En un PSD nadie lo llamaba nunca, que es
    justo donde más falta hace: Photoshop entrega «Decoración 7» y el importador
    no puede saber que ese rectángulo es el precio.

    Degrada como el resto de proveedores: sin `ENABLE_LAYER_VISION` ni clave, no
    consulta nada y la propuesta sale de la categoría que trajera el PSD.
    """
    from . import semantic_layers

    candidatos = candidate_layers(project)
    con_campo = [layer for layer in candidatos if layer.category in SLOT_CATEGORIES]
    if len(con_campo) >= MIN_SLOT_LAYERS:
        return []
    avisos = semantic_layers.classify(project, candidatos)
    return avisos


def derive(
    project: Project,
    brand_id: str,
    *,
    name: str | None = None,
    erase_from_plate: bool = True,
    classify_with_vision: bool = True,
) -> Template:
    """Propone la plantilla de un KV ya importado y la deja escrita en disco."""
    avisos_vision: list[str] = []
    if classify_with_vision:
        try:
            avisos_vision = _classify_with_vision(project)
        except Exception as exc:  # noqa: BLE001 - la visión es opcional, nunca bloquea
            logger.info("La clasificación por visión no se pudo usar: %s", exc)
            avisos_vision = []

    template = Template(
        brand_id=brand_id,
        name=(name or project.name or "Plantilla").strip()[:120],
        source_canvas=project.canvas,
        source_project_id=project.project_id,
    )
    template_store.ensure_template_dirs(brand_id, template.template_id)

    canvas_w, canvas_h = project.canvas.width, project.canvas.height
    usados: set[str] = set()
    campos_en_el_arte: list[Layer] = []

    for layer in candidate_layers(project):
        caja = Box.from_pixels(layer.x, layer.y, layer.width, layer.height, canvas_w, canvas_h)
        rel = _ensure_png(project, layer)
        congelado = None
        if rel:
            origen = _project_path(project, rel)
            congelado = template_store.freeze_file(
                brand_id,
                template.template_id,
                origen,
                f"assets/{layer_extraction.layer_slug(layer)}.png",
            )
        elif layer.type == LayerType.IMAGE:
            # Sin píxeles no hay nada que dibujar ni que congelar: meterla
            # dejaría un campo que produce un hueco en las doscientas piezas.
            template.warnings.append(
                f"'{layer.name}' se quedó fuera de la plantilla: no se pudo "
                "recortar su PNG."
            )
            continue

        if layer.category not in SLOT_CATEGORIES:
            template.fixed_layers.append(
                TemplateLayer(
                    layer_id=layer.id,
                    name=layer.name,
                    type=layer.type,
                    category=layer.category,
                    src=congelado,
                    box=caja,
                    z_index=layer.z_index,
                    rotation=layer.rotation,
                    content=(layer.content or None) if layer.type == LayerType.TEXT else None,
                    font_family=layer.font_family,
                    font_size=layer.font_size,
                    font_weight=layer.font_weight,
                    color=layer.color,
                    align=layer.text_align,
                    line_height=layer.line_height,
                    meta={"psd_name": layer.meta.get("psd_name")},
                )
            )
            continue

        campo = Slot(
            id=_slot_slug(layer.category, usados),
            label=layer.name,
            kind=SlotKind.IMAGE,
            category=layer.category,
            required=layer.category in REQUIRED_SLOT_CATEGORIES,
            box=caja,
            z_index=layer.z_index,
            source_layer_id=layer.id,
            sample_src=congelado,
        )

        if layer.category == LayerCategory.PRODUCT:
            ancho_px, alto_px = caja.to_pixels(canvas_w, canvas_h)[2:]
            campo.min_source_px = int(min(ancho_px, alto_px) / MAX_UPSCALE)
        else:
            campos, avisos = _text_slot_fields(project, layer)
            if campos:
                campo.kind = SlotKind.TEXT
                for clave, valor in campos.items():
                    setattr(campo, clave, valor)
            campo.warnings.extend(avisos)

        template.slots.append(campo)
        campos_en_el_arte.append(layer)

    if erase_from_plate and campos_en_el_arte:
        for layer in campos_en_el_arte:
            try:
                art_text.set_removed(project, layer, True, rebuild=False)
            except Exception as exc:  # noqa: BLE001 - una capa no debe romper la plantilla
                template.warnings.append(f"No se pudo vaciar '{layer.name}' del fondo: {exc}")
        template.warnings.extend(art_text.rebuild_plate(project))

    plate, avisos = _freeze_plate(project, template)
    template.plate = plate
    template.warnings.extend(avisos_vision)
    template.warnings.extend(avisos)
    template.warnings.extend(checks(template))
    template_store.save_template(template)
    return template


def _project_path(project: Project, relative: str):
    from . import storage

    return storage.abs_path(project.project_id, relative)


def checks(template: Template) -> list[str]:
    """Lo que hay que mirar antes de producir con esta plantilla.

    Son avisos, no rechazos: una plantilla sin campo de producto es rara pero
    legítima (una pieza de promoción sin producto), y discutírselo a quien la
    creó sobra. Lo que no sobra es decirlo antes de la tanda de doscientas.
    """
    avisos: list[str] = []
    if not template.slots:
        avisos.append(
            "La plantilla no tiene campos: todas las piezas saldrían idénticas. "
            "Marca como campo al menos el producto o el precio."
        )
    if not any(slot.category == LayerCategory.PRODUCT for slot in template.slots):
        avisos.append("No hay campo de producto: las piezas saldrán sin producto.")
    if 0 < len(template.slots) < MIN_SLOT_LAYERS and template.fixed_layers:
        # El caso real de un PSD de agencia: sus capas se llaman «Decoración 7»
        # y el importador no puede saber cuál es el precio. Decirlo aquí es la
        # diferencia entre revisar trece miniaturas una vez y descubrirlo en la
        # tanda, cuando los doscientos artes salen con el precio del KV.
        avisos.append(
            f"Solo se reconoció {len(template.slots)} campo. El PSD no dice qué es "
            f"cada capa, así que revisa las {len(template.fixed_layers)} capas fijas "
            "y marca como campo el precio, el titular y lo que cambie por producto."
        )
    if template.plate is None:
        avisos.append("La plantilla no tiene plancha congelada: el fondo saldrá vacío.")

    for slot in template.slots:
        if min(slot.box.width, slot.box.height) < MIN_SLOT_SIDE:
            avisos.append(
                f"El campo «{slot.label}» ocupa menos del "
                f"{int(MIN_SLOT_SIDE * 100)} % del lienzo: revisa su caja."
            )
        if slot.kind == SlotKind.TEXT and slot.font_size < slot.min_font_size:
            avisos.append(
                f"El campo «{slot.label}» arranca con un cuerpo menor que su mínimo legible."
            )
    return avisos


def promote(template: Template, layer_id: str) -> Slot:
    """Convierte una capa fija en campo. Conserva su caja y sus píxeles."""
    layer = template.layer_by_id(layer_id)
    if layer is None:
        raise KeyError(layer_id)
    usados = {slot.id for slot in template.slots}
    slot = Slot(
        id=_slot_slug(layer.category, usados),
        label=layer.name,
        kind=SlotKind.TEXT if layer.type == LayerType.TEXT else SlotKind.IMAGE,
        category=layer.category,
        required=False,
        box=layer.box,
        z_index=layer.z_index,
        source_layer_id=layer.layer_id,
        sample_src=layer.src,
        default=layer.content,
        font_family=layer.font_family,
        font_size=layer.font_size,
        font_weight=layer.font_weight,
        color=layer.color,
        align=layer.align,
        line_height=layer.line_height,
    )
    template.slots.append(slot)
    template.fixed_layers = [
        item for item in template.fixed_layers if item.layer_id != layer_id
    ]
    return slot


def demote(template: Template, slot_id: str) -> TemplateLayer:
    """Devuelve un campo a capa fija: se dibujará siempre con sus píxeles originales."""
    slot = template.slot_by_id(slot_id)
    if slot is None:
        raise KeyError(slot_id)
    layer = TemplateLayer(
        layer_id=slot.source_layer_id or slot.id,
        name=slot.label,
        type=LayerType.TEXT if slot.kind == SlotKind.TEXT else LayerType.IMAGE,
        category=slot.category,
        src=slot.sample_src,
        box=slot.box,
        z_index=slot.z_index,
        content=slot.default if slot.kind == SlotKind.TEXT else None,
        font_family=slot.font_family,
        font_size=slot.font_size,
        font_weight=slot.font_weight,
        color=slot.color,
        align=slot.align,
        line_height=slot.line_height,
    )
    template.fixed_layers.append(layer)
    template.slots = [item for item in template.slots if item.id != slot_id]
    return layer
