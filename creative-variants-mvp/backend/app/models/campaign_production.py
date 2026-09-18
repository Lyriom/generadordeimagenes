"""Contratos persistentes de una tanda generada desde plantillas aprobadas."""
from __future__ import annotations

from typing import Literal

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


__all__ = ["ProductionBatch", "ProductionBatchList", "ProductionPiece"]
