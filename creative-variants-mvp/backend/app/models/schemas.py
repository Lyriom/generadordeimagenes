"""Esquemas de entrada/salida de la API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .formats import SUPPORTED_FORMATS
from .project import (
    Canvas,
    Layer,
    LayerCategory,
    LayerType,
    Project,
    QualityReport,
    Variant,
)

INTENSITIES = ("conservative", "moderate", "creative")


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str
    version: str
    providers: dict[str, Any] = Field(default_factory=dict)


class IngestFile(BaseModel):
    """Archivo disponible en la carpeta de ingesta."""

    path: str
    name: str
    format: str
    width: int
    height: int
    size_mb: float
    pieces: int = Field(
        default=1,
        description="Piezas detectadas dentro del archivo (artboards del PSD).",
    )


class PsdPieceInfo(BaseModel):
    """Una pieza dentro de un PSD, con su rectángulo en el pliego."""

    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    origin: str = Field(description="artboard | grid | canvas")


class PsdPiecesResponse(BaseModel):
    source: str
    width: int
    height: int
    pieces: list[PsdPieceInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class IngestListResponse(BaseModel):
    directory: str
    files: list[IngestFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class IngestImportRequest(BaseModel):
    source: str = Field(description="Ruta relativa devuelta por GET /ingest.")
    name: str | None = None
    kv: str | None = Field(default=None, description="KV opcional de la misma carpeta.")
    import_layers: bool = Field(
        default=True, description="Importar las capas del PSD (si psd-tools está instalado)."
    )
    pieces: list[int] | None = Field(
        default=None,
        description=(
            "Índices de las piezas a importar (ver GET /ingest/pieces). "
            "Vacío o nulo = todas las detectadas."
        ),
    )


class ProjectImportResponse(BaseModel):
    """Resultado de importar un archivo que puede contener varias piezas."""

    projects: list[Project] = Field(default_factory=list)
    pieces_detected: int = 1
    pieces_imported: int = 0
    warnings: list[str] = Field(default_factory=list)


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    created_at: str
    updated_at: str
    canvas: Canvas
    layers: int
    variants: int


class AnalyzeRequest(BaseModel):
    run_ocr: bool = True
    run_segmentation: bool = True
    max_regions: int = Field(default=12, ge=1, le=40)
    extract: bool = Field(
        default=True,
        description="Extrae los PNG RGBA en el mismo paso (una capa sin PNG no se puede componer).",
    )


class AnalyzeResponse(BaseModel):
    project_id: str
    layers: list[Layer]
    warnings: list[str] = Field(default_factory=list)
    segmentation_provider: str | None = None
    ocr_provider: str | None = None


class LayerPatch(BaseModel):
    """Actualización parcial de una capa existente (PUT /layers)."""

    id: str
    name: str | None = None
    category: LayerCategory | None = None
    type: LayerType | None = None
    x: int | None = None
    y: int | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    rotation: float | None = None
    z_index: int | None = None
    visible: bool | None = None
    locked: bool | None = None
    movable: bool | None = None
    resizable: bool | None = None
    reorderable: bool | None = None
    replaceable: bool | None = None
    preserve_aspect_ratio: bool | None = None
    content: str | None = None
    font_family: str | None = None
    font_size: int | None = Field(default=None, gt=0, le=600)
    font_weight: Literal["normal", "bold"] | None = None
    color: str | None = None
    text_align: Literal["left", "center", "right"] | None = None
    auto_contrast: bool | None = None
    export_as_text: bool | None = None
    text_verified: bool | None = None


class LayersUpdateRequest(BaseModel):
    updates: list[LayerPatch] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)
    order: list[str] | None = Field(
        default=None, description="Orden de capas de abajo hacia arriba (ids)."
    )


class LayerCreateRequest(BaseModel):
    """Crea una capa manualmente (rectángulo o texto)."""

    name: str | None = None
    category: LayerCategory = LayerCategory.DECORATION
    type: LayerType = LayerType.IMAGE
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    content: str | None = None
    font_size: int | None = Field(default=None, gt=0, le=600)
    color: str | None = None
    locked: bool = False
    auto_segment: bool = Field(
        default=True,
        description="Si es una capa imagen, intenta segmentar el sujeto dentro del rectángulo.",
    )


class MaskOp(BaseModel):
    """Operación de edición de máscara (pincel rectangular o elíptico)."""

    op: Literal["add", "subtract"] = "add"
    shape: Literal["rect", "ellipse"] = "rect"
    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class MaskEditRequest(BaseModel):
    layer_id: str
    operations: list[MaskOp] = Field(default_factory=list)
    reset_from_box: bool = Field(
        default=False, description="Reinicia la máscara con el rectángulo de la capa."
    )
    auto_segment: bool = Field(
        default=False, description="Re-segmenta automáticamente dentro del bounding box."
    )
    refine: bool = Field(default=True, description="Aplica limpieza morfológica.")
    feather: int = Field(default=2, ge=0, le=25)
    re_extract: bool = Field(default=True, description="Regenera el PNG de la capa.")


class ExtractRequest(BaseModel):
    layer_ids: list[str] | None = None
    feather: int = Field(default=2, ge=0, le=25)
    force: bool = False


class ExtractResponse(BaseModel):
    project_id: str
    extracted: list[str]
    skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ImageModelInfo(BaseModel):
    """Un modelo de IA de imagen que el usuario puede elegir."""

    id: str
    label: str
    description: str = ""
    provider: str = "magnific"
    supports_mask: bool = False
    resolutions: list[str] = Field(default_factory=list)


class ReconstructBackgroundRequest(BaseModel):
    layer_ids: list[str] | None = None
    prompt: str | None = None
    dilate: int = Field(default=6, ge=0, le=64)
    provider: str | None = Field(
        default=None, description="auto | opencv | magnific | openai | flux | adobe"
    )
    model: str | None = Field(
        default=None,
        description="Modelo de Magnific (ver /capabilities → image_models).",
    )


class ReconstructBackgroundResponse(BaseModel):
    project_id: str
    background: str
    provider: str
    warnings: list[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    count: int = Field(default=12, ge=1, le=30)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    formats: list[str] = Field(default_factory=lambda: ["1080x1080", "1080x1350", "1080x1920"])
    intensity: Literal["conservative", "moderate", "creative"] = "moderate"
    locked_layers: list[str] = Field(default_factory=list)
    movable_layers: list[str] | None = None
    resizable_layers: list[str] | None = None
    reorderable_layers: list[str] | None = None
    hidden_layers: list[str] = Field(default_factory=list)
    instruction: str | None = Field(default=None, max_length=400)
    product_position_instruction: str | None = Field(default=None, max_length=400)
    layouts: list[str] | None = None
    replace_existing: bool = True
    product_label: str | None = Field(default=None, max_length=180)
    product_arrangement: Literal["auto", "horizontal", "vertical", "overlap"] = "auto"

    @field_validator("formats")
    @classmethod
    def _validate_formats(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value))
        unknown = [item for item in unique if item not in SUPPORTED_FORMATS]
        if unknown:
            raise ValueError(f"Formatos no soportados: {', '.join(unknown)}")
        cleaned = [item for item in unique if item in SUPPORTED_FORMATS]
        if not cleaned:
            raise ValueError(
                f"Formatos no soportados. Use: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        return cleaned


class GenerateResponse(BaseModel):
    project_id: str
    variants: list[Variant]
    warnings: list[str] = Field(default_factory=list)


class ReplaceableLayer(BaseModel):
    """Capa que puede recibir otro producto, con lo justo para elegirla."""

    id: str
    name: str
    category: LayerCategory
    width: int
    height: int
    area_ratio: float = Field(description="Fracción del lienzo que ocupa (0..1).")
    src: str | None = None


class ReplaceableLayersResponse(BaseModel):
    project_id: str
    layers: list[ReplaceableLayer] = Field(default_factory=list)


class DetectProductRequest(BaseModel):
    """Recortar el producto de la fotografía cuando el PSD no lo trae como capa."""

    provider: str | None = Field(
        default=None,
        description="Proveedor para limpiar el fondo tras el recorte (auto | opencv | magnific | …).",
    )
    model: str | None = Field(default=None, description="Modelo de Magnific para el fondo.")
    scene_model: str | None = Field(
        default=None,
        description=(
            "Modelo de edición para las fotos de ambiente, donde el producto hay "
            "que separarlo del decorado (por defecto MAGNIFIC_SCENE_MODEL)."
        ),
    )
    prompt: str | None = Field(default=None, max_length=800)
    dilate: int = Field(
        default=4, ge=0, le=32, description="Expansión del borde al borrar el producto."
    )
    force: bool = Field(
        default=False,
        description=(
            "Repetir el recorte aunque la pieza ya tenga producto detectado. "
            "Se parte siempre de la foto original, no del fondo ya modificado."
        ),
    )


class DetectProductResponse(BaseModel):
    project_id: str
    layer: Layer | None = None
    detected: bool = False
    warnings: list[str] = Field(default_factory=list)


class ArtTextStyle(BaseModel):
    """Cómo estaba escrito el original, medido sobre sus propios píxeles."""

    color: str
    align: Literal["left", "center", "right"]
    lines: int
    ink_height: int
    line_height: float
    stroke: float = Field(
        default=0.0,
        description="Grosor de trazo relativo al alto de tinta: decide redonda o negrita.",
    )
    line_pitch: int = Field(
        default=0, description="Distancia entre líneas en píxeles del arte original."
    )


class ArtTextLayer(BaseModel):
    """Un elemento del arte que se puede reescribir o quitar."""

    id: str
    name: str
    category: LayerCategory
    z_index: int
    text: str = Field(description="Lo que dice hoy el elemento.")
    original_text: str = Field(default="", description="Lo que decía el arte importado.")
    editable: bool = Field(description="Sus píxeles tienen texto que se puede medir.")
    rewritten: bool = False
    removed: bool = False
    in_plate: bool = Field(
        default=False,
        description="Sus píxeles siguen aplanados en el fondo: quitarlo exige borrarlo de ahí.",
    )
    src: str | None = None
    style: ArtTextStyle | None = None


class ArtTextListResponse(BaseModel):
    project_id: str
    layers: list[ArtTextLayer] = Field(default_factory=list)
    brand_font: bool = Field(
        default=False,
        description=(
            "Hay tipografía de marca subida. Sin ella el copy reescrito sale con "
            "la del sistema: el color y el cuerpo son los del arte, las letras no."
        ),
    )
    brand_font_bold: bool = Field(
        default=False, description="Además hay una cara negrita de marca."
    )


class ArtTextRequest(BaseModel):
    """Reescribir el copy de un elemento, quitarlo del arte o devolverlo."""

    content: str | None = Field(default=None, max_length=400)
    color: str | None = None
    align: Literal["left", "center", "right"] | None = None
    weight: Literal["normal", "bold"] | None = None
    font_size: int | None = Field(default=None, gt=0, le=600)
    removed: bool | None = Field(
        default=None, description="True lo quita del arte; False lo devuelve."
    )
    restore: bool = Field(
        default=False, description="Vuelve a los píxeles originales del elemento."
    )
    erase_background: bool | None = Field(
        default=None,
        description=(
            "Borrar sus píxeles de la plancha. Por defecto se decide solo: solo "
            "hace falta cuando el elemento venía aplanado en el fondo."
        ),
    )


class ArtTextResponse(BaseModel):
    project_id: str
    layer: Layer
    warnings: list[str] = Field(default_factory=list)


class FontReferenceResponse(BaseModel):
    """Tipografía de marca del proyecto después de subirla."""

    project_id: str
    font: str | None = None
    font_bold: str | None = None
    rewritten: int = Field(
        default=0,
        description="Textos ya reescritos que se recalcularon con la cara nueva.",
    )
    warnings: list[str] = Field(default_factory=list)


class TextOverride(BaseModel):
    """Texto que una tanda concreta debe escribir en un elemento del arte."""

    layer_id: str
    content: str = Field(max_length=400)


class ReplaceProductResponse(BaseModel):
    project_id: str
    layer: Layer
    warnings: list[str] = Field(default_factory=list)


class AutoRequest(BaseModel):
    """Modo automático: solo lo que un usuario necesita decidir."""

    count: int = Field(default=3, ge=1, le=10)
    formats: list[str] | None = Field(
        default=None,
        description="Vacío o nulo = tamaño nativo del arte más los dos formatos de redes.",
    )
    intensity: Literal["conservative", "moderate", "creative"] = "moderate"
    instruction: str | None = Field(default=None, max_length=400)
    product_position_instruction: str | None = Field(default=None, max_length=400)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    replace_existing: bool = True
    product_label: str | None = Field(default=None, max_length=180)
    product_arrangement: Literal["auto", "horizontal", "vertical", "overlap"] = "auto"
    template_mode: bool = Field(
        default=False,
        description=(
            "Sustitución fiel: conserva tamaño, fondo, capas y posiciones del KV; "
            "solo cambia el producto."
        ),
    )
    background_provider: (
        Literal["auto", "opencv", "magnific", "openai", "flux", "adobe"] | None
    ) = None
    background_model: str | None = Field(
        default=None,
        max_length=80,
        description="Modelo de Magnific a usar (ver /capabilities → image_models).",
    )
    background_prompt: str | None = Field(default=None, max_length=800)
    regenerate_background: bool = False
    text_overrides: list[TextOverride] | None = Field(
        default=None,
        description=(
            "Copy de esta tanda. Es el juego completo: un elemento reescrito antes "
            "y ausente aquí vuelve a su texto original, así que el precio de un "
            "producto no se queda pegado en el arte del siguiente."
        ),
    )

    @field_validator("formats")
    @classmethod
    def _validate_formats(cls, value: list[str] | None) -> list[str] | None:
        if not value:
            return None
        unique = list(dict.fromkeys(value))
        unknown = [item for item in unique if item not in SUPPORTED_FORMATS]
        if unknown:
            raise ValueError(f"Formatos no soportados: {', '.join(unknown)}")
        cleaned = [item for item in unique if item in SUPPORTED_FORMATS]
        if not cleaned:
            raise ValueError(
                f"Formatos no soportados. Use: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        return cleaned


class AutoStep(BaseModel):
    """Un paso ejecutado por el modo automático, en lenguaje llano."""

    name: str
    detail: str
    ok: bool = True


class AutoResponse(BaseModel):
    project_id: str
    steps: list[AutoStep] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VariantListResponse(BaseModel):
    project_id: str
    variants: list[Variant]


class QualityResponse(BaseModel):
    variant_id: str
    quality: QualityReport


class DeleteResponse(BaseModel):
    project_id: str
    deleted: bool


class CapabilitiesResponse(BaseModel):
    segmentation: dict[str, Any]
    ocr: dict[str, Any]
    inpainting: dict[str, Any]
    image_models: list[ImageModelInfo] = Field(default_factory=list)
    formats: dict[str, list[int]]
    format_catalog: list[dict[str, Any]] = Field(default_factory=list)
    layouts: list[dict[str, str]]
    intensities: list[str]
