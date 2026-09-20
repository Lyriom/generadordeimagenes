"""Contratos persistentes de una tanda generada desde plantillas aprobadas."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .project import new_id, utcnow


class ProductionPiece(BaseModel):
    piece_id: str = Field(default_factory=new_id)
    row_number: int = Field(ge=2)
    product: str = ""
    template_candidate_id: str
    template_name: str
    format: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    proposal: int = Field(default=1, ge=1)
    png: str
    jpg: str
    psd: str
    preview_url: str = ""
    warnings: list[str] = Field(default_factory=list)
    status: Literal["ready", "warning", "error"] = "ready"


class ProductionBatch(BaseModel):
    batch_id: str = Field(default_factory=new_id)
    client_id: str
    campaign_id: str
    created_at: str = Field(default_factory=utcnow)
    status: Literal["ready", "partial", "failed"] = "ready"
    matrix_filename: str = "matriz.csv"
    pieces: list[ProductionPiece] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    zip_url: str = ""
    manifest_url: str = ""
    total_rows: int = 0
    total_pieces: int = 0


class ProductionBatchList(BaseModel):
    batches: list[ProductionBatch] = Field(default_factory=list)


class ProductionJob(BaseModel):
    """Orden persistente que el worker puede retomar sin depender del navegador.

    El navegador no conserva los ``File`` de una subida tras recargar, y el
    broker no debe recibir una matriz ni imágenes grandes como argumentos. La
    API guarda ambos dentro de esta orden y Celery recibe solamente sus tres
    identificadores. ``campaign_snapshot`` congela el brief y las plantillas
    aprobadas tal como estaban cuando el equipo pulsó «Generar».
    """

    task_id: str = Field(default_factory=new_id)
    client_id: str
    campaign_id: str
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    state: Literal["PENDING", "STARTED", "PROGRESS", "COMPLETED", "FAILED"] = "PENDING"
    progress: int = Field(default=0, ge=0, le=100)
    detail: str = "En espera del worker…"
    matrix_filename: str = "matriz.csv"
    matrix_path: str = ""
    # Nombre original -> ruta relativa dentro de production/jobs/<task_id>.
    # La copia aislada impide que borrar un activo de la biblioteca mientras la
    # tanda espera deje al worker sin su producto.
    product_files: dict[str, str] = Field(default_factory=dict)
    product_asset_ids: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    default_formats: list[str] = Field(default_factory=list)
    use_ai_copy: bool = True
    brand_name: str = ""
    campaign_snapshot: dict[str, Any] = Field(default_factory=dict)
    planned_pieces: int = Field(default=0, ge=0)
    batch_id: str | None = None
    error: str | None = None

    def touch(self) -> None:
        self.updated_at = utcnow()


class ProductionTaskStatus(BaseModel):
    """Respuesta de polling estable para la producción de campaña."""

    task_id: str
    state: Literal["PENDING", "STARTED", "PROGRESS", "COMPLETED", "FAILED"]
    result: ProductionBatch | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ProductionTaskList(BaseModel):
    """Órdenes activas que se pueden retomar al abrir una campaña."""

    tasks: list[ProductionTaskStatus] = Field(default_factory=list)


__all__ = [
    "ProductionBatch",
    "ProductionBatchList",
    "ProductionJob",
    "ProductionPiece",
    "ProductionTaskList",
    "ProductionTaskStatus",
]
