"""Endpoints de marcas y plantillas: la biblioteca que no caduca.

Las rutas van anidadas (`/brands/{brand_id}/templates/{template_id}`) a
propósito: una plantilla solo existe dentro de una marca, y así el identificador
de la marca se valida antes de tocar el disco en todos los caminos, en vez de
depender de un índice que haya que mantener aparte.
"""
from __future__ import annotations

import logging
import mimetypes

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..models.template import Brand, Slot, SlotKind, Template, normalise_slug
from ..models.template_schemas import (
    BrandCreateRequest,
    BrandDeleteResponse,
    BrandListResponse,
    BrandResponse,
    BrandSummary,
    BrandUpdateRequest,
    TemplateDeleteResponse,
    TemplateFromProjectRequest,
    TemplateListResponse,
    TemplateResponse,
    TemplateUpdateRequest,
    summarise,
)
from ..services import storage, template_store, templating
from ..services.security import slugify
from .deps import bind_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/brands",
    tags=["marcas"],
    dependencies=[Depends(bind_session)],
)


def _load_brand(brand_id: str) -> Brand:
    try:
        return template_store.load_brand(brand_id)
    except template_store.BrandNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa marca.") from None


def _load_template(brand_id: str, template_id: str) -> Template:
    _load_brand(brand_id)
    try:
        return template_store.load_template(brand_id, template_id)
    except template_store.TemplateNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No existe esa plantilla en esta marca."
        ) from None


# ----------------------------------------------------------------------- marcas
@router.post("", response_model=BrandResponse, status_code=status.HTTP_201_CREATED)
def create_brand(request: BrandCreateRequest) -> BrandResponse:
    brand = Brand(
        name=request.name.strip(),
        slug=slugify(request.name, fallback="marca"),
        fonts=request.fonts or Brand().fonts,
        palette=request.palette,
        handles=request.handles,
    )
    template_store.save_brand(brand)
    return BrandResponse(brand=brand, templates=0)


@router.get("", response_model=BrandListResponse)
def list_brands() -> BrandListResponse:
    return BrandListResponse(
        brands=[
            BrandSummary(
                brand_id=brand.brand_id,
                name=brand.name,
                slug=brand.slug,
                templates=len(template_store.list_templates(brand.brand_id)),
                updated_at=brand.updated_at,
            )
            for brand in template_store.list_brands()
        ]
    )


@router.get("/{brand_id}", response_model=BrandResponse)
def get_brand(brand_id: str) -> BrandResponse:
    brand = _load_brand(brand_id)
    return BrandResponse(brand=brand, templates=len(template_store.list_templates(brand_id)))


@router.put("/{brand_id}", response_model=BrandResponse)
def update_brand(brand_id: str, request: BrandUpdateRequest) -> BrandResponse:
    brand = _load_brand(brand_id)
    if request.name is not None:
        brand.name = request.name.strip()
        brand.slug = slugify(brand.name, fallback="marca")
    if request.fonts is not None:
        brand.fonts = request.fonts
    if request.palette is not None:
        brand.palette = request.palette
    if request.handles is not None:
        brand.handles = request.handles
    template_store.save_brand(brand)
    return BrandResponse(brand=brand, templates=len(template_store.list_templates(brand_id)))


@router.delete("/{brand_id}", response_model=BrandDeleteResponse)
def delete_brand(brand_id: str) -> BrandDeleteResponse:
    """Borra la marca y **toda su biblioteca**. No hay papelera."""
    _load_brand(brand_id)
    cuantas = len(template_store.list_templates(brand_id))
    borrada = template_store.delete_brand(brand_id)
    return BrandDeleteResponse(deleted=borrada, brand_id=brand_id, templates_deleted=cuantas)


# -------------------------------------------------------------------- plantillas
@router.post(
    "/{brand_id}/templates/from-project",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Convertir un KV importado en plantilla de la marca",
)
def create_template_from_project(
    brand_id: str, request: TemplateFromProjectRequest
) -> TemplateResponse:
    _load_brand(brand_id)
    try:
        project = storage.load_project(request.project_id)
    except storage.ProjectNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No existe ese KV. Si pasaron horas desde que lo subiste, la limpieza "
            "ya se lo llevó: vuelve a importarlo y crea la plantilla.",
        ) from None

    if not templating.candidate_layers(project):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "El KV no tiene capas utilizables todavía. Impórtalo desde un PSD o "
            "analízalo antes de convertirlo en plantilla.",
        )

    template = templating.derive(
        project,
        brand_id,
        name=request.name,
        erase_from_plate=request.erase_slots_from_plate,
        classify_with_vision=request.classify_with_vision,
    )
    # El KV queda con los campos retirados: es lo mismo que «Quitar del arte» y
    # hay que guardarlo, porque la plancha congelada de la plantilla ya cuenta
    # con ello.
    storage.save_project(project)
    return TemplateResponse(template=template, warnings=template.warnings)


@router.get("/{brand_id}/templates", response_model=TemplateListResponse)
def list_templates(brand_id: str) -> TemplateListResponse:
    _load_brand(brand_id)
    return TemplateListResponse(
        brand_id=brand_id,
        templates=[summarise(item) for item in template_store.list_templates(brand_id)],
    )


@router.get("/{brand_id}/templates/{template_id}", response_model=TemplateResponse)
def get_template(brand_id: str, template_id: str) -> TemplateResponse:
    template = _load_template(brand_id, template_id)
    return TemplateResponse(template=template, warnings=templating.checks(template))


@router.put("/{brand_id}/templates/{template_id}", response_model=TemplateResponse)
def update_template(
    brand_id: str, template_id: str, request: TemplateUpdateRequest
) -> TemplateResponse:
    """Corrige la propuesta: qué es campo, cómo se llama y dónde va."""
    template = _load_template(brand_id, template_id)
    warnings: list[str] = []

    if request.name is not None:
        template.name = request.name.strip()[:120] or template.name

    for layer_id in request.promote:
        try:
            templating.promote(template, layer_id)
        except KeyError:
            warnings.append(f"No hay ninguna capa fija con id {layer_id}.")

    for slot_id in request.demote:
        try:
            templating.demote(template, slot_id)
        except KeyError:
            warnings.append(f"No hay ningún campo llamado «{slot_id}».")

    for patch in request.slots:
        slot = template.slot_by_id(patch.id)
        if slot is None:
            warnings.append(f"No hay ningún campo llamado «{patch.id}».")
            continue
        warnings.extend(_apply_patch(template, slot, patch))

    # Renombrar un campo cambia la cabecera de su columna, así que un máster que
    # lo referencie por el nombre viejo apuntaría al vacío. Se avisa en vez de
    # reescribirlos a ciegas: los másters se aprueban a mano.
    conocidos = {slot.id for slot in template.slots} | {
        layer.layer_id for layer in template.fixed_layers
    }
    for clave, master in template.masters.items():
        huérfanas = [p.ref for p in master.placements if p.ref not in conocidos]
        if huérfanas:
            master.approved = False
            warnings.append(
                f"El máster de {clave} referencia campos que ya no existen "
                f"({', '.join(huérfanas)}): queda sin aprobar y hay que rehacerlo."
            )

    template.warnings = templating.checks(template)
    template_store.save_template(template)
    return TemplateResponse(template=template, warnings=warnings + template.warnings)


def _apply_patch(template: Template, slot: Slot, patch) -> list[str]:
    """Aplica una corrección sobre un campo. Devuelve lo que no pudo aplicarse."""
    warnings: list[str] = []
    sencillos = (
        "label",
        "required",
        "box",
        "default",
        "max_lines",
        "min_font_size",
        "font_size",
        "font_family",
        "font_weight",
        "color",
        "align",
        "fit",
        "min_source_px",
        "category",
    )
    for campo in sencillos:
        valor = getattr(patch, campo, None)
        if valor is not None:
            setattr(slot, campo, valor)

    if patch.kind is not None:
        nuevo = SlotKind(patch.kind)
        if nuevo == SlotKind.TEXT and not slot.default:
            warnings.append(
                f"El campo «{slot.id}» pasa a texto sin valor por defecto: "
                "cada fila tendrá que traer el suyo."
            )
        slot.kind = nuevo

    if patch.rename_to:
        nuevo_id = normalise_slug(patch.rename_to)
        if nuevo_id != slot.id and template.slot_by_id(nuevo_id) is not None:
            warnings.append(f"Ya existe un campo llamado «{nuevo_id}»: no se renombró.")
        else:
            slot.id = nuevo_id
    return warnings


@router.delete(
    "/{brand_id}/templates/{template_id}", response_model=TemplateDeleteResponse
)
def delete_template(brand_id: str, template_id: str) -> TemplateDeleteResponse:
    _load_template(brand_id, template_id)
    return TemplateDeleteResponse(
        deleted=template_store.delete_template(brand_id, template_id),
        template_id=template_id,
    )


@router.get(
    "/{brand_id}/templates/{template_id}/files/{path:path}",
    summary="Servir un archivo congelado de la plantilla (plancha, recorte, máster)",
)
def template_file(brand_id: str, template_id: str, path: str) -> FileResponse:
    _load_template(brand_id, template_id)
    target = template_store.template_path(brand_id, template_id, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese archivo.")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)
