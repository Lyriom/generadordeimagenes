"""Modelos Pydantic del proyecto, lienzo y capas."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LayerCategory(str, Enum):
    PRODUCT = "product"
    PERSON = "person"
    LOGO = "logo"
    HEADLINE = "headline"
    SUBHEADLINE = "subheadline"
    PRICE = "price"
    CTA = "cta"
    LEGAL = "legal"
    DECORATION = "decoration"
    BACKGROUND = "background"


TEXT_CATEGORIES = {
    LayerCategory.HEADLINE,
    LayerCategory.SUBHEADLINE,
    LayerCategory.PRICE,
    LayerCategory.CTA,
    LayerCategory.LEGAL,
}

#: Categorías cuyos píxeles nunca deben deformarse ni regenerarse.
PIXEL_CRITICAL_CATEGORIES = {
    LayerCategory.LOGO,
    LayerCategory.PRODUCT,
    LayerCategory.PERSON,
}

CATEGORY_LABELS_ES = {
    LayerCategory.PRODUCT: "Producto",
    LayerCategory.PERSON: "Persona",
    LayerCategory.LOGO: "Logo",
    LayerCategory.HEADLINE: "Titular",
    LayerCategory.SUBHEADLINE: "Subtítulo",
    LayerCategory.PRICE: "Precio",
    LayerCategory.CTA: "CTA",
    LayerCategory.LEGAL: "Texto legal",
    LayerCategory.DECORATION: "Decoración",
    LayerCategory.BACKGROUND: "Fondo",
}


class LayerType(str, Enum):
    IMAGE = "image"
    TEXT = "text"


class Canvas(BaseModel):
    width: int = Field(gt=0, le=8000)
    height: int = Field(gt=0, le=8000)


class Layer(BaseModel):
    """Una capa del lienzo. Coordenadas en píxeles del lienzo del proyecto."""

    id: str = Field(default_factory=new_id)
    name: str = "Capa"
    type: LayerType = LayerType.IMAGE
    category: LayerCategory = LayerCategory.DECORATION

    # Recursos en disco (rutas relativas a la carpeta del proyecto).
    src: str | None = None
    mask: str | None = None

    # Geometría
    x: int = 0
    y: int = 0
    width: int = Field(default=1, gt=0)
    height: int = Field(default=1, gt=0)
    rotation: float = 0.0
    z_index: int = 0

    # Comportamiento
    visible: bool = True
    locked: bool = False
    movable: bool = True
    resizable: bool = True
    reorderable: bool = True
    replaceable: bool = True
    preserve_aspect_ratio: bool = True

    # Texto
    content: str | None = None
    font_family: str = "DejaVu Sans"
    font_size: int = 48
    font_weight: Literal["normal", "bold"] = "normal"
    color: str = "#FFFFFF"
    text_align: Literal["left", "center", "right"] = "left"
    line_height: float = 1.15
    auto_contrast: bool = True

    # Metadatos
    confidence: float = 1.0
    source: Literal["auto", "manual", "ocr", "upload"] = "manual"
    extracted: bool = False
    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        raw = (value or "").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hex_part = raw[1:]
        if len(hex_part) == 3:
            hex_part = "".join(ch * 2 for ch in hex_part)
        if len(hex_part) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in hex_part):
            return "#FFFFFF"
        return "#" + hex_part.upper()

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 1.0

    @property
    def is_text(self) -> bool:
        return self.type == LayerType.TEXT

    @property
    def pixel_critical(self) -> bool:
        return self.locked or self.category in PIXEL_CRITICAL_CATEGORIES


class SourceImage(BaseModel):
    path: str
    width: int
    height: int
    format: str
    original_filename: str
    bytes: int


class ProjectReferences(BaseModel):
    kv: SourceImage | None = None
    logo: SourceImage | None = None
    font: str | None = None


class AnalysisInfo(BaseModel):
    ran_at: str | None = None
    segmentation_provider: str | None = None
    ocr_provider: str | None = None
    warnings: list[str] = Field(default_factory=list)
    detections: int = 0
    text_regions: int = 0


class BackgroundInfo(BaseModel):
    path: str | None = None
    provider: str | None = None
    generated_at: str | None = None
    warnings: list[str] = Field(default_factory=list)


class QualityReport(BaseModel):
    score: int = 0
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class VariantLayerPlacement(BaseModel):
    layer_id: str
    name: str
    category: LayerCategory
    type: LayerType
    x: int
    y: int
    width: int
    height: int
    z_index: int
    visible: bool = True
    locked: bool = False
    font_size: int | None = None
    color: str | None = None
    text_align: str | None = None
    content: str | None = None


class Variant(BaseModel):
    id: str = Field(default_factory=new_id)
    index: int = 0
    layout: str = ""
    layout_label: str = ""
    format: str = "1080x1080"
    width: int = 1080
    height: int = 1080
    seed: int = 0
    intensity: str = "moderate"
    background_style: str = "plate"
    image: str = ""
    thumbnail: str | None = None
    quality: QualityReport = Field(default_factory=QualityReport)
    prediction: dict[str, Any] = Field(default_factory=dict)
    placements: list[VariantLayerPlacement] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)


class Project(BaseModel):
    project_id: str = Field(default_factory=new_id)
    name: str = "Proyecto"
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    canvas: Canvas
    source: SourceImage
    references: ProjectReferences = Field(default_factory=ProjectReferences)
    layers: list[Layer] = Field(default_factory=list)
    analysis: AnalysisInfo = Field(default_factory=AnalysisInfo)
    background: BackgroundInfo = Field(default_factory=BackgroundInfo)
    variants: list[Variant] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def layer_by_id(self, layer_id: str) -> Layer | None:
        for layer in self.layers:
            if layer.id == layer_id:
                return layer
        return None

    def variant_by_id(self, variant_id: str) -> Variant | None:
        for variant in self.variants:
            if variant.id == variant_id:
                return variant
        return None

    def sorted_layers(self) -> list[Layer]:
        return sorted(self.layers, key=lambda layer: layer.z_index)

    def touch(self) -> None:
        self.updated_at = utcnow()
