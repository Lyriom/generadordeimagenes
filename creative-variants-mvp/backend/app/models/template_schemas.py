"""Entradas y salidas de la API de marcas y plantillas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .project import LayerCategory
from .template import Box, Brand, BrandFonts, PaletteColor, Slot, Template, TemplateLayer


class BrandCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    fonts: BrandFonts | None = None
    palette: list[PaletteColor] = Field(default_factory=list)
    handles: dict[str, str] = Field(
        default_factory=dict,
        description="Cuentas en redes por plataforma: {'instagram': 'marcimex'}.",
    )


class BrandUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    fonts: BrandFonts | None = None
    palette: list[PaletteColor] | None = None
    handles: dict[str, str] | None = None


class BrandSummary(BaseModel):
    brand_id: str
    name: str
    slug: str
    templates: int = 0
    updated_at: str


class BrandListResponse(BaseModel):
    brands: list[BrandSummary] = Field(default_factory=list)


class BrandResponse(BaseModel):
    brand: Brand
    templates: int = 0


class TemplateFromProjectRequest(BaseModel):
    project_id: str
    name: str | None = Field(default=None, max_length=120)
    erase_slots_from_plate: bool = Field(
        default=True,
        description=(
            "Vaciar del fondo los elementos que pasan a ser campo. Sin esto, cada "
            "arte de la tanda lleva debajo el precio y el producto del KV original. "
            "Como efecto, el proyecto de origen queda con esos elementos retirados."
        ),
    )
    classify_with_vision: bool = Field(
        default=True,
        description=(
            "Preguntar a visión qué es cada capa cuando el PSD no lo dice. Una "
            "consulta por plantilla, y solo si la importación dejó menos de dos "
            "campos. Sin ENABLE_LAYER_VISION ni clave no consulta nada."
        ),
    )


class SlotPatch(BaseModel):
    """Corrección de un campo propuesto. Todo opcional: se aplica lo que venga."""

    id: str
    label: str | None = Field(default=None, max_length=120)
    rename_to: str | None = Field(
        default=None, max_length=60, description="Nuevo slug del campo (cabecera de columna)."
    )
    kind: Literal["image", "text"] | None = None
    category: LayerCategory | None = None
    required: bool | None = None
    box: Box | None = None
    default: str | None = Field(default=None, max_length=400)
    max_lines: int | None = Field(default=None, ge=1, le=12)
    min_font_size: int | None = Field(default=None, ge=6, le=400)
    font_size: int | None = Field(default=None, gt=0, le=600)
    font_family: str | None = Field(default=None, max_length=120)
    font_weight: Literal["normal", "bold"] | None = None
    color: str | None = None
    align: Literal["left", "center", "right"] | None = None
    fit: Literal["contain", "cover"] | None = None
    min_source_px: int | None = Field(default=None, ge=0, le=20000)


class TemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    slots: list[SlotPatch] = Field(default_factory=list)
    promote: list[str] = Field(
        default_factory=list,
        description="layer_id de capas fijas que pasan a ser campo.",
    )
    demote: list[str] = Field(
        default_factory=list,
        description="id de campos que vuelven a ser capa fija con sus píxeles originales.",
    )


class TemplateSummary(BaseModel):
    template_id: str
    brand_id: str
    name: str
    slots: int = 0
    required_slots: int = 0
    fixed_layers: int = 0
    formats_approved: int = 0
    formats_proposed: int = 0
    width: int = 0
    height: int = 0
    updated_at: str


class TemplateListResponse(BaseModel):
    brand_id: str
    templates: list[TemplateSummary] = Field(default_factory=list)


class TemplateResponse(BaseModel):
    template: Template
    warnings: list[str] = Field(default_factory=list)


class TemplateDeleteResponse(BaseModel):
    deleted: bool
    template_id: str


class BrandDeleteResponse(BaseModel):
    deleted: bool
    brand_id: str
    templates_deleted: int = 0


def summarise(template: Template) -> TemplateSummary:
    aprobados = sum(1 for master in template.masters.values() if master.approved)
    return TemplateSummary(
        template_id=template.template_id,
        brand_id=template.brand_id,
        name=template.name,
        slots=len(template.slots),
        required_slots=len(template.required_slots),
        fixed_layers=len(template.fixed_layers),
        formats_approved=aprobados,
        formats_proposed=len(template.masters) - aprobados,
        width=template.source_canvas.width,
        height=template.source_canvas.height,
        updated_at=template.updated_at,
    )


__all__ = [
    "BrandCreateRequest",
    "BrandDeleteResponse",
    "BrandListResponse",
    "BrandResponse",
    "BrandSummary",
    "BrandUpdateRequest",
    "Slot",
    "SlotPatch",
    "TemplateDeleteResponse",
    "TemplateFromProjectRequest",
    "TemplateLayer",
    "TemplateListResponse",
    "TemplateResponse",
    "TemplateSummary",
    "TemplateUpdateRequest",
    "summarise",
]
