"""Persistencia atomica de campanas dentro de la biblioteca del cliente."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..models.campaign import Campaign, ClientKnowledge
from ..models.campaign_production import ProductionBatch, ProductionJob
from . import template_store
from .security import resolve_inside, validate_uuid


class CampaignNotFoundError(KeyError):
    """No existe la campana dentro del cliente indicado."""


def campaigns_root(client_id: str) -> Path:
    # brand_dir valida el identificador y mantiene cliente == marca.
    root = template_store.brand_dir(client_id) / "campaigns"
    root.mkdir(parents=True, exist_ok=True)
    return root


def campaign_dir(client_id: str, campaign_id: str) -> Path:
    return campaigns_root(client_id) / validate_uuid(campaign_id, "campaign_id")


def ensure_campaign_dirs(client_id: str, campaign_id: str) -> Path:
    base = campaign_dir(client_id, campaign_id)
    for name in ("sources", "analysis", "production"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def _write_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".campaign-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_campaign(campaign: Campaign) -> Path:
    base = ensure_campaign_dirs(campaign.client_id, campaign.campaign_id)
    campaign.touch()
    target = base / "campaign.json"
    _write_json(target, campaign.model_dump(mode="json"))
    return target


def load_campaign(client_id: str, campaign_id: str) -> Campaign:
    target = campaign_dir(client_id, campaign_id) / "campaign.json"
    if not target.exists():
        raise CampaignNotFoundError(campaign_id)
    with target.open("r", encoding="utf-8") as handle:
        campaign = Campaign.model_validate(json.load(handle))
    # Impide que un manifiesto copiado entre carpetas salte de cliente.
    if campaign.client_id != client_id:
        raise CampaignNotFoundError(campaign_id)
    return campaign


def list_campaigns(client_id: str) -> list[Campaign]:
    root = campaigns_root(client_id)
    campaigns: list[Campaign] = []
    for entry in sorted(root.iterdir()):
        manifest = entry / "campaign.json"
        if not entry.is_dir() or not manifest.exists():
            continue
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                item = Campaign.model_validate(json.load(handle))
            if item.client_id == client_id:
                campaigns.append(item)
        except Exception:  # noqa: BLE001 - una campana corrupta no rompe la biblioteca
            continue
    return sorted(campaigns, key=lambda item: item.created_at, reverse=True)


def source_dir(client_id: str, campaign_id: str, source_id: str) -> Path:
    source = validate_uuid(source_id, "source_id")
    base = ensure_campaign_dirs(client_id, campaign_id) / "sources" / source
    base.mkdir(parents=True, exist_ok=True)
    return base


def campaign_path(client_id: str, campaign_id: str, relative: str) -> Path:
    return resolve_inside(campaign_dir(client_id, campaign_id), relative)


def relative_path(client_id: str, campaign_id: str, path: Path) -> str:
    return str(path.resolve().relative_to(campaign_dir(client_id, campaign_id).resolve()))


def knowledge_path(client_id: str) -> Path:
    return template_store.brand_dir(client_id) / "knowledge.json"


def load_knowledge(client_id: str) -> ClientKnowledge:
    target = knowledge_path(client_id)
    if not target.exists():
        return ClientKnowledge(client_id=client_id)
    try:
        with target.open("r", encoding="utf-8") as handle:
            knowledge = ClientKnowledge.model_validate(json.load(handle))
    except Exception:  # noqa: BLE001 - recuperacion segura de un fichero antiguo
        return ClientKnowledge(client_id=client_id)
    if knowledge.client_id != client_id:
        return ClientKnowledge(client_id=client_id)
    return knowledge


def save_knowledge(knowledge: ClientKnowledge) -> Path:
    # Asegura que el cliente existe y prepara las carpetas permanentes.
    template_store.ensure_brand_dirs(knowledge.client_id)
    knowledge.touch()
    target = knowledge_path(knowledge.client_id)
    _write_json(target, knowledge.model_dump(mode="json"))
    return target


def production_root(client_id: str, campaign_id: str) -> Path:
    root = ensure_campaign_dirs(client_id, campaign_id) / "production"
    root.mkdir(parents=True, exist_ok=True)
    return root


def production_asset_dir(client_id: str, campaign_id: str, asset_id: str) -> Path:
    """Carpeta privada de una foto de producto antes de generar la tanda."""

    target = production_root(client_id, campaign_id) / "assets" / validate_uuid(
        asset_id, "asset_id"
    )
    target.mkdir(parents=True, exist_ok=True)
    return target


def batch_dir(client_id: str, campaign_id: str, batch_id: str) -> Path:
    target = production_root(client_id, campaign_id) / validate_uuid(batch_id, "batch_id")
    target.mkdir(parents=True, exist_ok=True)
    return target


def production_jobs_root(client_id: str, campaign_id: str) -> Path:
    """Órdenes pendientes, separadas de las tandas ya entregadas."""

    target = production_root(client_id, campaign_id) / "jobs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def production_drafts_root(client_id: str, campaign_id: str) -> Path:
    """Matrices validadas, disponibles aunque el navegador se recargue."""

    target = production_root(client_id, campaign_id) / "drafts"
    target.mkdir(parents=True, exist_ok=True)
    return target


def production_draft_dir(client_id: str, campaign_id: str, draft_id: str) -> Path:
    target = production_drafts_root(client_id, campaign_id) / validate_uuid(
        draft_id, "matrix_draft_id"
    )
    target.mkdir(parents=True, exist_ok=True)
    return target


def production_job_dir(client_id: str, campaign_id: str, task_id: str) -> Path:
    target = production_jobs_root(client_id, campaign_id) / validate_uuid(task_id, "task_id")
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_production_job(job: ProductionJob) -> Path:
    job.touch()
    target = production_job_dir(job.client_id, job.campaign_id, job.task_id) / "job.json"
    _write_json(target, job.model_dump(mode="json"))
    return target


def load_production_job(client_id: str, campaign_id: str, task_id: str) -> ProductionJob:
    target = production_job_dir(client_id, campaign_id, task_id) / "job.json"
    if not target.exists():
        raise CampaignNotFoundError(task_id)
    with target.open("r", encoding="utf-8") as handle:
        job = ProductionJob.model_validate(json.load(handle))
    if job.client_id != client_id or job.campaign_id != campaign_id:
        raise CampaignNotFoundError(task_id)
    return job


def list_production_jobs(
    client_id: str,
    campaign_id: str,
    *,
    states: set[str] | None = None,
) -> list[ProductionJob]:
    """Órdenes de producción, ordenadas por la última actualización."""

    jobs: list[ProductionJob] = []
    for entry in production_jobs_root(client_id, campaign_id).iterdir():
        manifest = entry / "job.json"
        if not entry.is_dir() or not manifest.exists():
            continue
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                item = ProductionJob.model_validate(json.load(handle))
            if (
                item.client_id == client_id
                and item.campaign_id == campaign_id
                and (states is None or item.state in states)
            ):
                jobs.append(item)
        except Exception:  # noqa: BLE001 - un manifiesto corrupto no bloquea la campaña
            continue
    return sorted(jobs, key=lambda item: item.updated_at, reverse=True)


def save_batch(batch: ProductionBatch) -> Path:
    target = batch_dir(batch.client_id, batch.campaign_id, batch.batch_id) / "batch.json"
    _write_json(target, batch.model_dump(mode="json"))
    return target


def load_batch(client_id: str, campaign_id: str, batch_id: str) -> ProductionBatch:
    target = batch_dir(client_id, campaign_id, batch_id) / "batch.json"
    if not target.exists():
        raise CampaignNotFoundError(batch_id)
    with target.open("r", encoding="utf-8") as handle:
        batch = ProductionBatch.model_validate(json.load(handle))
    if batch.client_id != client_id or batch.campaign_id != campaign_id:
        raise CampaignNotFoundError(batch_id)
    return batch


def list_batches(client_id: str, campaign_id: str) -> list[ProductionBatch]:
    batches: list[ProductionBatch] = []
    for entry in production_root(client_id, campaign_id).iterdir():
        manifest = entry / "batch.json"
        if not entry.is_dir() or not manifest.exists():
            continue
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                item = ProductionBatch.model_validate(json.load(handle))
            if item.client_id == client_id and item.campaign_id == campaign_id:
                batches.append(item)
        except Exception:  # noqa: BLE001 - una tanda corrupta no rompe las demas
            continue
    return sorted(batches, key=lambda item: item.created_at, reverse=True)


def batch_path(client_id: str, campaign_id: str, batch_id: str, relative: str) -> Path:
    return resolve_inside(batch_dir(client_id, campaign_id, batch_id), relative)
