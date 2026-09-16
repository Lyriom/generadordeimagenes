"""Marca y plantilla: la biblioteca que sobrevive al barrido de proyectos.

Un proyecto es desechable a propósito (un PSD de 100 MB se convierte en cientos
de MB de capas, máscaras y variantes, y `PROJECT_RETENTION_HOURS` los barre). Lo
que no puede ser desechable es el trabajo de decidir **qué cambia en cada arte**:
que el precio es campo y el logo no, que el producto va en ese hueco y no en
otro. Eso se decide una vez por KV y se reutiliza en cada tanda.

Tres reglas que atraviesan todo el módulo:

1. **Las cajas van en fracciones del lienzo, nunca en píxeles.** Es lo mismo que
   ya hace `ProductZone` y por el mismo motivo: la misma decisión tiene que valer
   para las cinco medidas de la tanda.
2. **Un campo se identifica por un slug legible** (`precio`, `producto_2`), no
   por el uuid de la capa que lo originó. Ese slug es la cabecera de la columna
   en la matriz y en el CSV que el cliente rellena; un uuid ahí no lo llena
   nadie.
3. **Los píxeles se congelan dentro de la plantilla.** Referenciar el PNG del
   proyecto deja una biblioteca que se rompe sola en ocho horas.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .project import (
    Canvas,
    LayerCategory,
    LayerType,
    QualityReport,
    new_id,
    utcnow,
)


class Box(BaseModel):
    """Rectángulo en fracciones del lienzo (0..1), esquina superior izquierda.

    Se recorta en lugar de rechazarse cuando sobresale un poco: viene de un
    ratón arrastrando sobre el arte, y un píxel fuera es la intención correcta
    mal dibujada. Lo que sí se rechaza es un lado nulo, que no es un rectángulo.
    """

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_canvas(self) -> "Box":
        self.width = min(self.width, 1.0 - self.x)
        self.height = min(self.height, 1.0 - self.y)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("La caja queda fuera del lienzo.")
        return self

    def to_pixels(self, canvas_width: int, canvas_height: int) -> tuple[int, int, int, int]:
        """(x, y, ancho, alto) en píxeles de un lienzo concreto, con lado mínimo 1."""
        x = int(round(self.x * canvas_width))
        y = int(round(self.y * canvas_height))
        width = max(1, int(round(self.width * canvas_width)))
        height = max(1, int(round(self.height * canvas_height)))
        return x, y, width, height

    @classmethod
    def from_pixels(
        cls, x: int, y: int, width: int, height: int, canvas_width: int, canvas_height: int
    ) -> "Box":
        canvas_width = max(1, canvas_width)
        canvas_height = max(1, canvas_height)
        return cls(
            x=min(max(x / canvas_width, 0.0), 1.0),
            y=min(max(y / canvas_height, 0.0), 1.0),
            width=min(max(width / canvas_width, 1e-6), 1.0),
            height=min(max(height / canvas_height, 1e-6), 1.0),
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)


def normalise_slug(value: str) -> str:
    """Slug de campo: minúsculas ascii, guiones y nada más.

    Es la cabecera de una columna de CSV, así que una tilde o un espacio ahí
    reaparecen como columna distinta en cuanto la hoja pasa por otro programa.
    """
    cleaned = "".join(
        ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "_"
        for ch in (value or "").strip().lower()
    ).strip("_-")
    if not cleaned:
        raise ValueError("El identificador del campo no puede quedar vacío.")
    return cleaned[:60]


class SlotKind(str, Enum):
    IMAGE = "image"
    TEXT = "text"


#: Nombre canónico del campo según lo que era en el arte. Es el slug que verá
#: quien rellene el Excel, así que se escribe en castellano y sin acentos: una
#: cabecera de CSV con tildes se rompe en cuanto pasa por una hoja de cálculo
#: guardada en otra codificación.
SLOT_SLUGS: dict[LayerCategory, str] = {
    LayerCategory.PRODUCT: "producto",
    LayerCategory.PERSON: "persona",
    LayerCategory.HEADLINE: "titular",
    LayerCategory.SUBHEADLINE: "subtitulo",
    LayerCategory.PRICE: "precio",
    LayerCategory.CTA: "cta",
    LayerCategory.LEGAL: "legal",
    LayerCategory.LOGO: "logo",
    LayerCategory.DECORATION: "elemento",
    LayerCategory.BACKGROUND: "fondo",
}

#: Cuerpo mínimo por debajo del cual un texto deja de leerse en una pieza de
#: redes vista en un teléfono. No es una cifra de diseño: es el corte con el que
#: la validación de la matriz marca una celda antes de producir doscientas
#: piezas con el precio en un hilo.
MIN_READABLE_FONT_SIZE = 18


class Slot(BaseModel):
    """Un campo de la plantilla: lo que cambia de un arte al siguiente."""

    id: str = Field(min_length=1, max_length=60, description="Slug estable. Cabecera de columna.")
    label: str = Field(default="Campo", max_length=120)
    kind: SlotKind = SlotKind.TEXT
    category: LayerCategory = LayerCategory.HEADLINE
    required: bool = False
    box: Box
    z_index: int = 0

    #: Capa del proyecto de origen. Informativo: la plantilla ya no depende de
    #: ella, pero permite explicar de dónde salió el campo.
    source_layer_id: str | None = None
    #: PNG congelado con los píxeles originales del campo. Es lo que se dibuja
    #: en la pieza de prueba de un máster y lo que permite devolver el campo a
    #: capa fija sin volver al proyecto.
    sample_src: str | None = None

    # --- imagen ---
    fit: Literal["contain", "cover"] = "contain"
    #: Lado menor mínimo, en píxeles, que debe traer la imagen de cada fila.
    #: Cero = sin exigencia. Lo calcula la plantilla a partir de su caja.
    min_source_px: int = 0

    # --- texto ---
    default: str | None = Field(default=None, max_length=400)
    max_lines: int = Field(default=2, ge=1, le=12)
    min_font_size: int = Field(default=MIN_READABLE_FONT_SIZE, ge=6, le=400)
    font_size: int = Field(default=48, gt=0, le=600)
    font_family: str = "DejaVu Sans"
    font_weight: Literal["normal", "bold"] = "normal"
    color: str = "#FFFFFF"
    align: Literal["left", "center", "right"] = "left"
    line_height: float = Field(default=1.15, gt=0.5, le=3.0)

    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        return normalise_slug(value)


class TemplateLayer(BaseModel):
    """Una capa fija: la identidad del KV, que no cambia entre artes."""

    layer_id: str = Field(default_factory=new_id)
    name: str = "Capa"
    type: LayerType = LayerType.IMAGE
    category: LayerCategory = LayerCategory.DECORATION
    #: Ruta relativa a la carpeta de la plantilla. Congelada, no del proyecto.
    src: str | None = None
    box: Box
    z_index: int = 0
    rotation: float = 0.0
    #: Escenografía autorizada a estirarse hasta el borde (el mismo permiso que
    #: concede `layout_engine`; nunca se da a producto, logo ni persona).
    stretch: bool = False

    # Texto fijo (un legal de campaña que no cambia por producto).
    content: str | None = None
    font_family: str = "DejaVu Sans"
    font_size: int = Field(default=48, gt=0, le=600)
    font_weight: Literal["normal", "bold"] = "normal"
    color: str = "#FFFFFF"
    align: Literal["left", "center", "right"] = "left"
    line_height: float = Field(default=1.15, gt=0.5, le=3.0)
    meta: dict[str, Any] = Field(default_factory=dict)


class MasterPlacement(BaseModel):
    """Dónde cae un campo o una capa fija en un formato concreto."""

    #: Id del campo (`precio`) o `layer_id` de la capa fija.
    ref: str
    box: Box
    z_index: int = 0
    font_size: int | None = None
    pinned: bool = True
    stretch: bool = False


class FormatMaster(BaseModel):
    """La adaptación de la plantilla a un formato, firmada por una persona.

    `approved` es el punto entero del máster: mientras esté en falso, es una
    propuesta del motor como cualquier variante. Aprobado, la producción masiva
    deja de componer y se limita a pegar valores en cajas conocidas, que es lo
    que hace que doscientas piezas salgan iguales a la que se revisó.
    """

    format: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    layout: str = ""
    background_style: str = "plate"
    approved: bool = False
    approved_at: str | None = None
    placements: list[MasterPlacement] = Field(default_factory=list)
    preview: str | None = None
    quality: QualityReport = Field(default_factory=QualityReport)
    warnings: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utcnow)


class Template(BaseModel):
    """Un KV convertido en plantilla reutilizable de una marca."""

    template_id: str = Field(default_factory=new_id)
    brand_id: str
    name: str = "Plantilla"
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    source_canvas: Canvas
    #: Proyecto del que nació. Puede no existir ya: es trazabilidad, no un enlace.
    source_project_id: str | None = None
    #: Plancha congelada, **ya vaciada de los campos**. Sin esto, cada arte de la
    #: tanda lleva debajo el precio del KV original.
    plate: str | None = None

    fixed_layers: list[TemplateLayer] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list)
    masters: dict[str, FormatMaster] = Field(default_factory=dict)

    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def slot_by_id(self, slot_id: str) -> Slot | None:
        for slot in self.slots:
            if slot.id == slot_id:
                return slot
        return None

    def layer_by_id(self, layer_id: str) -> TemplateLayer | None:
        for layer in self.fixed_layers:
            if layer.layer_id == layer_id:
                return layer
        return None

    @property
    def required_slots(self) -> list[Slot]:
        return [slot for slot in self.slots if slot.required]

    def touch(self) -> None:
        self.updated_at = utcnow()


class BrandFonts(BaseModel):
    """Tipografías de la marca, del catálogo permanente o subidas."""

    client_id: str | None = Field(
        default=None, description="Cliente del catálogo en app/assets/client_fonts."
    )
    regular: str | None = Field(default=None, description="Id de la fuente redonda.")
    bold: str | None = Field(default=None, description="Id de la cara negrita.")


class PaletteColor(BaseModel):
    hex: str
    #: Fracción de píxeles de las referencias con este color. 0 = sin medir.
    share: float = Field(default=0.0, ge=0.0, le=1.0)
    role: str = ""

    @field_validator("hex")
    @classmethod
    def _normalise(cls, value: str) -> str:
        raw = (value or "").strip()
        raw = raw if raw.startswith("#") else "#" + raw
        body = raw[1:]
        if len(body) == 3:
            body = "".join(ch * 2 for ch in body)
        if len(body) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in body):
            raise ValueError(f"Color inválido: {value}")
        return "#" + body.upper()


class StyleProfile(BaseModel):
    """Lo aprendido de los artes que la marca publica de verdad (etapa 4).

    Vive aquí desde ya, vacío, para que la marca tenga sitio donde guardarlo sin
    migrar el archivo cuando llegue el escaneo de redes.
    """

    analysed_at: str | None = None
    source: str | None = Field(default=None, description="folder | windsor | scraper")
    posts: int = 0
    palette: list[PaletteColor] = Field(default_factory=list)
    #: Proporciones que la marca publica, con su reparto: "1:1" -> 0.62.
    aspect_census: dict[str, float] = Field(default_factory=dict)
    #: Esquina donde aparece el logo, con su reparto.
    logo_corners: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class Brand(BaseModel):
    """El cliente: tipografías, paleta, estilo y su biblioteca de plantillas."""

    brand_id: str = Field(default_factory=new_id)
    name: str = Field(default="Marca", max_length=120)
    slug: str = Field(default="marca", max_length=60)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    fonts: BrandFonts = Field(default_factory=BrandFonts)
    palette: list[PaletteColor] = Field(default_factory=list)
    style: StyleProfile | None = None
    #: Cuentas de la marca en redes, para el escaneo de la etapa 4.
    handles: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = utcnow()
