"""Cliente, campana y conocimiento creativo persistente.

Una fuente de campana no es un ``Project``.  Los proyectos siguen siendo artes
producibles y caducan; estos objetos describen el material que ensena a la
aplicacion como debe verse una campana y viven junto a la biblioteca de la
marca.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .project import new_id, utcnow


class CampaignSourceKind(str, Enum):
    PDF = "pdf"
    PRESENTATION = "presentation"
    LAYERED_DESIGN = "layered_design"
    IMAGE = "image"
    DOCUMENT = "document"
    FONT = "font"


class CampaignSourceRole(str, Enum):
    STRATEGY = "strategy"
    VISUAL_REFERENCE = "visual_reference"
    KEY_VISUAL = "key_visual"
    FINAL_ART = "final_art"
    BRAND_MANUAL = "brand_manual"
    LOGO = "logo"
    BACKGROUND = "background"
    TYPOGRAPHY = "typography"
    LEGAL = "legal"
    SCHEDULE = "schedule"
    PRODUCT_REFERENCE = "product_reference"
    OTHER = "other"


class CampaignSource(BaseModel):
    """Un archivo aportado como conocimiento, nunca como arte de produccion."""

    source_id: str = Field(default_factory=new_id)
    filename: str = Field(max_length=240)
    kind: CampaignSourceKind
    media_type: str = "application/octet-stream"
    extension: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    stored_path: str
    roles: list[CampaignSourceRole] = Field(default_factory=list)
    page_count: int = Field(default=1, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    extracted_text: str = ""
    preview_files: list[str] = Field(default_factory=list)
    asset_files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)


class ProductionAsset(BaseModel):
    """Foto de producto persistida para una tanda, separada del brief.

    No es una fuente de conocimiento: subirla no reanaliza la campaña ni
    cambia las plantillas. Solo queda disponible para las filas de matriz y
    sobrevive a una actualización del navegador.
    """

    asset_id: str = Field(default_factory=new_id)
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(default="application/octet-stream", max_length=160)
    extension: str = Field(min_length=2, max_length=12)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    stored_path: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    created_at: str = Field(default_factory=utcnow)


class BriefField(BaseModel):
    """Campo que una plantilla puede reservar u omitir."""

    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["image", "text", "money", "date", "badge"] = "text"
    required: bool = False
    repeatable: bool = False
    generate_if_missing: bool = False
    hide_when_empty: bool = True
    notes: str = Field(default="", max_length=300)


class CampaignBrief(BaseModel):
    """Brief util para diseno; no es una respuesta libre dificil de consumir."""

    objective: str = ""
    audience: str = ""
    primary_message: str = ""
    creative_concept: str = ""
    tone: list[str] = Field(default_factory=list)
    palette: list[str] = Field(default_factory=list)
    typography: list[str] = Field(default_factory=list)
    visual_rules: list[str] = Field(default_factory=list)
    product_treatment: list[str] = Field(default_factory=list)
    headline_style: str = ""
    cta_style: str = ""
    legal_requirements: list[str] = Field(default_factory=list)
    required_elements: list[str] = Field(default_factory=list)
    optional_elements: list[str] = Field(default_factory=list)
    forbidden_elements: list[str] = Field(default_factory=list)
    variable_fields: list[BriefField] = Field(default_factory=list)
    source_summary: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    generated_at: str = Field(default_factory=utcnow)


class ProductCountRange(BaseModel):
    minimum: int = Field(default=1, ge=0, le=20)
    maximum: int = Field(default=1, ge=0, le=20)


class TemplateSlotProposal(BaseModel):
    """Contrato de un hueco dinamico antes de dibujar la plantilla real."""

    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=100)
    category: Literal[
        "product",
        "product_name",
        "headline",
        "subheadline",
        "price",
        "previous_price",
        "installment",
        "discount",
        "cta",
        "legal",
        "validity",
        "logo",
    ]
    kind: Literal["image", "text", "money", "date", "badge"] = "text"
    required: bool = False
    repeatable: bool = False
    generate_if_missing: bool = False
    hide_when_empty: bool = True
    layout_role: str = Field(default="", max_length=240)


class NormalizedPlacement(BaseModel):
    """Caja de un elemento dentro del area segura, en coordenadas 0..1."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @field_validator("width")
    @classmethod
    def _width_is_finite(cls, value: float) -> float:
        return round(float(value), 4)

    @field_validator("height")
    @classmethod
    def _height_is_finite(cls, value: float) -> float:
        return round(float(value), 4)


class TemplateBlueprint(BaseModel):
    """Sistema visual ejecutable que la IA puede inferir y el renderer consume.

    ``placements`` usa las familias ``square``, ``portrait``, ``story`` y
    ``landscape``. No es una descripción decorativa: al aprobar una candidata
    esta geometría queda congelada en la memoria del cliente y vuelve a usarse
    en producción.
    """

    version: int = Field(default=1, ge=1, le=10)
    archetype: Literal[
        "hero_center", "split_left", "split_right", "price_focus", "product_grid", "editorial"
    ] = "hero_center"
    text_alignment: Literal["left", "center", "right"] = "left"
    background_style: Literal["campaign", "gradient", "solid", "light"] = "campaign"
    accent_style: Literal["orbs", "diagonal", "cards", "frame", "minimal"] = "frame"
    density: Literal["airy", "balanced", "compact"] = "balanced"
    mirror_variants: bool = True
    placements: dict[str, dict[str, NormalizedPlacement]] = Field(default_factory=dict)
    #: Color de cada texto tal como estaba en el arte original, por hueco. El
    #: renderer pintaba siempre en blanco, y un precio en blanco sobre la
    #: pastilla blanca del propio arte no se ve. Vacío = blanco, como antes.
    text_colors: dict[str, str] = Field(default_factory=dict)


class TemplateCandidate(BaseModel):
    """Propuesta persistente y aprobable, sin productos reales dentro."""

    candidate_id: str = Field(default_factory=new_id)
    name: str = Field(max_length=120)
    category: Literal[
        "single_product",
        "price_promotion",
        "combo",
        "product_benefit",
        "institutional",
    ]
    rationale: str = Field(default="", max_length=500)
    layout_intent: str = Field(default="", max_length=700)
    supported_product_count: ProductCountRange = Field(default_factory=ProductCountRange)
    slots: list[TemplateSlotProposal] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    supported_aspects: list[str] = Field(
        default_factory=lambda: ["1:1", "4:5", "9:16", "landscape", "custom"]
    )
    adaptation_rules: list[str] = Field(default_factory=list)
    blueprint: TemplateBlueprint = Field(default_factory=TemplateBlueprint)
    revision_hash: str = Field(default="", max_length=64)
    status: Literal["proposed", "approved", "rejected"] = "proposed"
    approved: bool = False
    approved_at: str | None = None
    decision_notes: str = ""
    source_project_id: str | None = None
    preview_url: str | None = None
    preview_urls: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class Campaign(BaseModel):
    campaign_id: str = Field(default_factory=new_id)
    client_id: str
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(default="", max_length=1000)
    social_urls: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    status: Literal[
        "collecting", "ready_for_brief", "templates_proposed", "templates_approved"
    ] = "collecting"
    sources: list[CampaignSource] = Field(default_factory=list)
    production_assets: list[ProductionAsset] = Field(default_factory=list)
    brief: CampaignBrief | None = None
    brief_reviewed_at: str | None = None
    template_candidates: list[TemplateCandidate] = Field(default_factory=list)
    analysis_engine: Literal["none", "deterministic", "openai"] = "none"
    warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("social_urls")
    @classmethod
    def _unique_urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip()[:500] for item in values if item.strip()))

    def touch(self) -> None:
        self.updated_at = utcnow()


class ApprovedCandidateMemory(BaseModel):
    campaign_id: str
    candidate_id: str
    name: str
    category: str
    slots: list[str] = Field(default_factory=list)
    layout_intent: str = ""
    candidate_snapshot: dict[str, Any] = Field(default_factory=dict)
    approved_at: str = Field(default_factory=utcnow)


class ClientKnowledge(BaseModel):
    """Memoria acumulativa del cliente que sobrevive a cada campana."""

    client_id: str
    social_urls: list[str] = Field(default_factory=list)
    learned_rules: list[str] = Field(default_factory=list)
    approved_candidates: list[ApprovedCandidateMemory] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()


class ClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    social_urls: list[str] = Field(default_factory=list)


class ClientUpdateRequest(BaseModel):
    """Datos permanentes del cliente, separados del brief de una campaña."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    social_urls: list[str] | None = None


class ClientProfile(BaseModel):
    client_id: str
    name: str
    social_urls: list[str] = Field(default_factory=list)
    campaigns: int = 0
    templates: int = 0
    learned_rules: list[str] = Field(default_factory=list)
    approved_candidates: int = 0
    created_at: str
    updated_at: str


class CampaignCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(default="", max_length=1000)
    social_urls: list[str] = Field(default_factory=list)


class CampaignUpdateRequest(BaseModel):
    """Cambios de contexto que obligan a volver a contrastar el brief."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, max_length=1000)
    social_urls: list[str] | None = None


class CampaignDeleteResponse(BaseModel):
    """Recuento de lo que se fue con la campana.

    Borrar no puede ser un 204 mudo: la carpeta se lleva fuentes, tandas y la
    plantilla aprobada de esa campana. Quien pulsa el boton merece ver cuanto
    desaparecio, y que las correcciones aprendidas del cliente siguen ahi.
    """

    deleted: bool
    campaign_id: str
    name: str = ""
    sources_deleted: int = 0
    batches_deleted: int = 0
    approved_templates_removed: int = 0


class CampaignSourceRoleUpdateRequest(BaseModel):
    """Decisión humana sobre el papel de un archivo de campaña.

    Un logo o una textura que el equipo marca aquí pasa a ser branding fijo;
    no queda como una referencia genérica que el renderer puede difuminar.
    """

    role: CampaignSourceRole


class CampaignSourcesResponse(BaseModel):
    campaign_id: str
    sources: list[CampaignSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProductionAssetsResponse(BaseModel):
    campaign_id: str
    assets: list[ProductionAsset] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GenerateBriefRequest(BaseModel):
    use_ai: bool = True
    preserve_review: bool = False


class CampaignBriefPatchRequest(BaseModel):
    """Correcciones humanas persistentes sobre el brief generado."""

    objective: str | None = Field(default=None, max_length=2000)
    audience: str | None = Field(default=None, max_length=2000)
    primary_message: str | None = Field(default=None, max_length=3000)
    creative_concept: str | None = Field(default=None, max_length=1000)
    tone: list[str] | None = None
    headline_style: str | None = Field(default=None, max_length=1000)
    cta_style: str | None = Field(default=None, max_length=1000)
    visual_rules: list[str] | None = None
    product_treatment: list[str] | None = None
    legal_requirements: list[str] | None = None
    required_elements: list[str] | None = None
    optional_elements: list[str] | None = None
    forbidden_elements: list[str] | None = None
    feedback: str = Field(default="", max_length=1000)


class GenerateBriefResponse(BaseModel):
    campaign_id: str
    brief: CampaignBrief
    template_candidates: list[TemplateCandidate]
    engine: Literal["deterministic", "openai"]
    warnings: list[str] = Field(default_factory=list)


class CandidateDecisionRequest(BaseModel):
    notes: str = Field(default="", max_length=1000)


class CandidateDecisionResponse(BaseModel):
    campaign_id: str
    candidate: TemplateCandidate
    campaign_status: str
