"""Flujo cliente -> conocimiento de campana -> brief -> plantillas propuestas."""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..models.campaign import (
    ApprovedCandidateMemory,
    Campaign,
    CampaignBrief,
    CampaignBriefPatchRequest,
    CampaignCreateRequest,
    CampaignDeleteResponse,
    CampaignSource,
    CampaignSourceRoleUpdateRequest,
    CampaignUpdateRequest,
    ClientUpdateRequest,
    CampaignSourcesResponse,
    CandidateDecisionRequest,
    CandidateDecisionResponse,
    ClientCreateRequest,
    ClientKnowledge,
    ClientProfile,
    GenerateBriefRequest,
    GenerateBriefResponse,
    ProductionAsset,
    ProductionAssetsResponse,
)
from ..models.campaign_production import (
    ProductionBatch,
    ProductionBatchList,
    ProductionJob,
    ProductionTaskList,
    ProductionTaskStatus,
)
from ..models.project import new_id, utcnow
from ..models.template import Brand
from ..services import (
    campaign_analysis,
    campaign_creative,
    campaign_ingestion,
    campaign_store,
    production_matrix,
    template_store,
)
from ..services.security import FileValidationError, slugify, validate_uuid
from .deps import bind_session

router = APIRouter(
    prefix="/clients",
    tags=["clientes y campanas"],
    dependencies=[Depends(bind_session)],
)

CHUNK = 1024 * 1024
MAX_FILES_PER_REQUEST = 50
PRODUCT_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"
}
# Nunca se conserva el Content-Type declarado por el navegador: es un dato no
# confiable y una imagen válida puede subir con ``text/html``. Pillow ya abre el
# binario para validar sus píxeles; usamos exactamente el formato que decodificó
# para servirlo después.
PRODUCT_IMAGE_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "TIFF": "image/tiff",
    "AVIF": "image/avif",
}


def _brand_or_404(client_id: str) -> Brand:
    try:
        return template_store.load_brand(client_id)
    except template_store.BrandNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese cliente.") from None


def _campaign_or_404(client_id: str, campaign_id: str) -> Campaign:
    _brand_or_404(client_id)
    try:
        return campaign_store.load_campaign(client_id, campaign_id)
    except campaign_store.CampaignNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No existe esa campana dentro del cliente."
        ) from None


def _profile(brand: Brand) -> ClientProfile:
    knowledge = campaign_store.load_knowledge(brand.brand_id)
    return ClientProfile(
        client_id=brand.brand_id,
        name=brand.name,
        social_urls=knowledge.social_urls,
        campaigns=len(campaign_store.list_campaigns(brand.brand_id)),
        templates=len(template_store.list_templates(brand.brand_id))
        + len(knowledge.approved_candidates),
        learned_rules=knowledge.learned_rules,
        approved_candidates=len(knowledge.approved_candidates),
        created_at=brand.created_at,
        updated_at=max(brand.updated_at, knowledge.updated_at),
    )


@router.post("", response_model=ClientProfile, status_code=status.HTTP_201_CREATED)
def create_client(request: ClientCreateRequest) -> ClientProfile:
    """Crea el contenedor permanente que comparte campanas y plantillas."""
    brand = Brand(
        name=request.name.strip(),
        slug=slugify(request.name, fallback="cliente"),
        meta={"social_urls": request.social_urls},
    )
    template_store.save_brand(brand)
    knowledge = ClientKnowledge(
        client_id=brand.brand_id,
        social_urls=list(dict.fromkeys(item.strip() for item in request.social_urls if item.strip())),
    )
    campaign_store.save_knowledge(knowledge)
    return _profile(brand)


@router.get("", response_model=list[ClientProfile])
def list_clients() -> list[ClientProfile]:
    # Tambien expone marcas creadas con la API anterior: no se pierde la
    # biblioteca existente al adoptar el nombre "cliente" en el nuevo flujo.
    return [_profile(brand) for brand in template_store.list_brands()]


@router.get("/{client_id}", response_model=ClientProfile)
def get_client(client_id: str) -> ClientProfile:
    return _profile(_brand_or_404(client_id))


@router.put("/{client_id}", response_model=ClientProfile)
def update_client(client_id: str, request: ClientUpdateRequest) -> ClientProfile:
    """Edita la ficha permanente sin alterar briefs ya aprobados."""

    brand = _brand_or_404(client_id)
    knowledge = campaign_store.load_knowledge(client_id)
    if request.name is not None:
        name = request.name.strip()
        if name and name != brand.name:
            brand.name = name
            brand.slug = slugify(name, fallback="cliente")
    if request.social_urls is not None:
        urls = list(dict.fromkeys(item.strip()[:500] for item in request.social_urls if item.strip()))
        knowledge.social_urls = urls
        brand.meta["social_urls"] = urls
    template_store.save_brand(brand)
    campaign_store.save_knowledge(knowledge)
    return _profile(brand)


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(client_id: str) -> None:
    """Borra cliente, campañas, fuentes y biblioteca cuando no hay trabajo vivo."""

    _brand_or_404(client_id)
    active: list[str] = []
    for campaign in campaign_store.list_campaigns(client_id):
        jobs = campaign_store.list_production_jobs(
            client_id, campaign.campaign_id, states={"PENDING", "STARTED", "PROGRESS"}
        )
        if jobs:
            active.append(campaign.name)
    if active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No se puede borrar el cliente mientras hay producción en curso: "
            + ", ".join(active[:4]) + ".",
        )
    if not template_store.delete_brand(client_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese cliente.")


@router.post(
    "/{client_id}/campaigns",
    response_model=Campaign,
    status_code=status.HTTP_201_CREATED,
)
def create_campaign(client_id: str, request: CampaignCreateRequest) -> Campaign:
    _brand_or_404(client_id)
    knowledge = campaign_store.load_knowledge(client_id)
    social_urls = list(
        dict.fromkeys(
            item.strip()
            for item in [*request.social_urls, *knowledge.social_urls]
            if item.strip()
        )
    )
    campaign = Campaign(
        client_id=client_id,
        name=request.name.strip(),
        objective=request.objective.strip(),
        social_urls=social_urls,
    )
    campaign_store.save_campaign(campaign)
    # Una URL nueva de campana pasa a ser conocimiento reutilizable del cliente.
    knowledge.social_urls = list(dict.fromkeys([*knowledge.social_urls, *social_urls]))
    campaign_store.save_knowledge(knowledge)
    return campaign


@router.get("/{client_id}/campaigns", response_model=list[Campaign])
def list_campaigns(client_id: str) -> list[Campaign]:
    _brand_or_404(client_id)
    return campaign_store.list_campaigns(client_id)


@router.get("/{client_id}/campaigns/{campaign_id}", response_model=Campaign)
def get_campaign(client_id: str, campaign_id: str) -> Campaign:
    return _campaign_or_404(client_id, campaign_id)


@router.put("/{client_id}/campaigns/{campaign_id}", response_model=Campaign)
def update_campaign(
    client_id: str, campaign_id: str, request: CampaignUpdateRequest
) -> Campaign:
    """Actualiza contexto sin obligar a crear otra campana."""

    campaign = _campaign_or_404(client_id, campaign_id)
    changed_context = False
    if request.name is not None:
        name = request.name.strip()
        if name != campaign.name:
            campaign.name = name
            changed_context = True
    if request.objective is not None and request.objective.strip() != campaign.objective:
        campaign.objective = request.objective.strip()
        changed_context = True
    if request.social_urls is not None:
        social_urls = list(
            dict.fromkeys(item.strip()[:500] for item in request.social_urls if item.strip())
        )
        if social_urls != campaign.social_urls:
            campaign.social_urls = social_urls
            campaign.meta.pop("social_evidence", None)
            changed_context = True
        knowledge = campaign_store.load_knowledge(client_id)
        knowledge.social_urls = list(
            dict.fromkeys([*knowledge.social_urls, *social_urls])
        )
        campaign_store.save_knowledge(knowledge)
    if changed_context:
        campaign.brief = None
        campaign.brief_reviewed_at = None
        campaign.template_candidates = []
        campaign.analysis_engine = "none"
        campaign.status = "ready_for_brief"
    campaign_store.save_campaign(campaign)
    return campaign


@router.delete(
    "/{client_id}/campaigns/{campaign_id}", response_model=CampaignDeleteResponse
)
def delete_campaign(client_id: str, campaign_id: str) -> CampaignDeleteResponse:
    """Borra una campana y todo lo que cuelga de ella. No hay papelera.

    El cliente y sus reglas aprendidas se quedan: esa memoria es el producto.
    Lo que sí se va con la campana es su plantilla aprobada, porque su vista
    previa y el arte del que salió viven dentro de la carpeta que se borra, y
    dejarla contaría plantillas que ya nadie puede abrir.
    """

    campaign = _campaign_or_404(client_id, campaign_id)
    active_jobs = campaign_store.list_production_jobs(
        client_id, campaign_id, states={"PENDING", "STARTED", "PROGRESS"}
    )
    if active_jobs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Hay una produccion en curso en esta campana. Espera a que termine "
            "antes de borrarla.",
        )
    batches = len(campaign_store.list_batches(client_id, campaign_id))
    sources = len(campaign.sources)

    knowledge = campaign_store.load_knowledge(client_id)
    restante = [
        item for item in knowledge.approved_candidates if item.campaign_id != campaign_id
    ]
    retiradas = len(knowledge.approved_candidates) - len(restante)

    if not campaign_store.delete_campaign(client_id, campaign_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No existe esa campana dentro del cliente."
        )
    # Solo despues de que la carpeta se haya ido: si el borrado falla, la
    # memoria del cliente no se queda mutilada apuntando a una campana viva.
    if retiradas:
        knowledge.approved_candidates = restante
        campaign_store.save_knowledge(knowledge)
    return CampaignDeleteResponse(
        deleted=True,
        campaign_id=campaign_id,
        name=campaign.name,
        sources_deleted=sources,
        batches_deleted=batches,
        approved_templates_removed=retiradas,
    )


async def _store_upload(
    client_id: str, campaign_id: str, upload: UploadFile
) -> tuple[str, Path, str, int]:
    filename = upload.filename or "archivo"
    extension = campaign_ingestion.validate_extension(filename)
    source_id = new_id()
    folder = campaign_store.source_dir(client_id, campaign_id, source_id)
    target = folder / f"original{extension}"
    digest = hashlib.sha256()
    total = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.campaign_max_source_bytes:
                    raise FileValidationError(
                        f"'{filename}' supera el limite de campaña de "
                        f"{settings.campaign_max_source_mb} MB."
                    )
                digest.update(chunk)
                handle.write(chunk)
        if total == 0:
            raise FileValidationError(f"'{filename}' esta vacio.")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await upload.close()
    return source_id, target, digest.hexdigest(), total


@router.post(
    "/{client_id}/campaigns/{campaign_id}/sources",
    response_model=CampaignSourcesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_sources(
    client_id: str,
    campaign_id: str,
    files: list[UploadFile] = File(..., description="Material de campana; repetir el campo files."),
) -> CampaignSourcesResponse:
    """Guarda conocimiento; no crea un Project por pagina ni por diapositiva."""
    campaign = _campaign_or_404(client_id, campaign_id)
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Sube al menos un archivo.")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Sube como maximo {MAX_FILES_PER_REQUEST} archivos por solicitud.",
        )
    added = []
    warnings: list[str] = []
    created_folders: list[Path] = []
    request_total = 0
    try:
        for upload in files:
            filename = upload.filename or "archivo"
            media_type = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            source_id, target, digest, total = await _store_upload(
                client_id, campaign_id, upload
            )
            created_folders.append(target.parent)
            request_total += total
            if request_total > settings.campaign_max_request_bytes:
                raise FileValidationError(
                    "La carga conjunta supera el limite de "
                    f"{settings.campaign_max_request_mb} MB por campaña. "
                    "Divide el material en varias cargas."
                )
            duplicate = next((item for item in campaign.sources if item.sha256 == digest), None)
            if duplicate is not None:
                shutil.rmtree(target.parent, ignore_errors=True)
                warnings.append(f"'{filename}' ya estaba en la campana y no se duplico.")
                continue
            # PDF, PPTX y PSD pueden requerir segundos de CPU. Ejecutarlos en
            # el pool evita congelar /health y el resto de la API mientras se
            # extrae el conocimiento de campaña.
            source = await run_in_threadpool(
                campaign_ingestion.inspect_source,
                client_id,
                campaign_id,
                source_id,
                target,
                filename,
                media_type,
                digest,
                total,
            )
            campaign.sources.append(source)
            added.append(source)
    except Exception:
        # La solicitud es atomica desde el punto de vista del manifiesto.
        for folder in created_folders:
            shutil.rmtree(folder, ignore_errors=True)
        raise

    if added:
        campaign.status = "ready_for_brief"
        campaign.brief = None
        campaign.brief_reviewed_at = None
        campaign.analysis_engine = "none"
        if campaign.template_candidates:
            warnings.append(
                "Se agrego evidencia nueva: vuelve a generar el brief y aprueba las plantillas actualizadas."
            )
            # La aprobación corresponde al material que una persona revisó.
            # Conservarla tras añadir evidencia permitiría producir usando un
            # sistema desactualizado. La memoria del cliente sí permanece y
            # alimenta las propuestas nuevas, pero no las autoaprueba.
            campaign.template_candidates = []
    campaign_store.save_campaign(campaign)
    return CampaignSourcesResponse(
        campaign_id=campaign_id,
        sources=added,
        warnings=warnings + [warning for source in added for warning in source.warnings],
    )


@router.get(
    "/{client_id}/campaigns/{campaign_id}/sources/{source_id}/files/{relative_path:path}",
    response_class=FileResponse,
)
def campaign_source_file(
    client_id: str,
    campaign_id: str,
    source_id: str,
    relative_path: str,
) -> FileResponse:
    campaign = _campaign_or_404(client_id, campaign_id)
    source = next((item for item in campaign.sources if item.source_id == source_id), None)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa fuente en la campana.")
    allowed = {source.stored_path, *source.preview_files, *source.asset_files}
    if relative_path not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El archivo no pertenece a esa fuente.")
    path = campaign_store.campaign_path(client_id, campaign_id, relative_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El archivo ya no esta disponible.")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=Path(source.filename).stem + path.suffix)


@router.delete(
    "/{client_id}/campaigns/{campaign_id}/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_campaign_source(client_id: str, campaign_id: str, source_id: str) -> None:
    """Retira evidencia equivocada sin dejarla contaminando el siguiente análisis."""

    campaign = _campaign_or_404(client_id, campaign_id)
    source = next((item for item in campaign.sources if item.source_id == source_id), None)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa fuente en la campana.")
    # El job contiene un snapshot del brief/plantillas, pero los recursos de
    # campaña (logos, fondos y activos PSD) siguen siendo archivos locales.
    # Borrarlos mientras el worker espera alteraría silenciosamente una tanda
    # ya aprobada. Al terminar se puede quitar la fuente con normalidad.
    active_jobs = campaign_store.list_production_jobs(
        client_id,
        campaign_id,
        states={"PENDING", "STARTED", "PROGRESS"},
    )
    if active_jobs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Hay una producción en curso que usa este contexto. Espera a que termine antes de quitar la fuente.",
        )
    source_root = campaign_store.campaign_path(
        client_id, campaign_id, f"sources/{source.source_id}"
    )
    campaign.sources = [item for item in campaign.sources if item.source_id != source_id]
    # El brief y las propuestas fueron construidos con el archivo que se retira;
    # conservarlos sería peor que pedir un reanálisis explícito.
    campaign.brief = None
    campaign.brief_reviewed_at = None
    campaign.template_candidates = []
    campaign.analysis_engine = "none"
    campaign.status = "ready_for_brief"
    campaign_store.save_campaign(campaign)
    shutil.rmtree(source_root, ignore_errors=True)


@router.put(
    "/{client_id}/campaigns/{campaign_id}/sources/{source_id}/role",
    response_model=CampaignSource,
)
def set_campaign_source_role(
    client_id: str,
    campaign_id: str,
    source_id: str,
    request: CampaignSourceRoleUpdateRequest,
) -> CampaignSource:
    """Confirma si una fuente debe actuar como branding fijo.

    El rol automático es una hipótesis. Esta elección humana evita que un logo
    o un fondo limpio se trate como inspiración genérica y se difumine en las
    plantillas; también invalida el brief anterior para no mezclar contextos.
    """

    campaign = _campaign_or_404(client_id, campaign_id)
    source = next((item for item in campaign.sources if item.source_id == source_id), None)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa fuente en la campana.")
    source.roles = [request.role]
    source.meta["user_role"] = request.role.value
    campaign.brief = None
    campaign.brief_reviewed_at = None
    campaign.template_candidates = []
    campaign.analysis_engine = "none"
    campaign.status = "ready_for_brief"
    campaign_store.save_campaign(campaign)
    return source


@router.post(
    "/{client_id}/campaigns/{campaign_id}/brief/generate",
    response_model=GenerateBriefResponse,
)
def generate_brief(
    client_id: str,
    campaign_id: str,
    request: GenerateBriefRequest = GenerateBriefRequest(),
) -> GenerateBriefResponse:
    brand = _brand_or_404(client_id)
    campaign = _campaign_or_404(client_id, campaign_id)
    if not campaign.sources and not campaign.social_urls and not campaign.objective:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "La campana necesita al menos un archivo, una red social o un objetivo.",
        )
    knowledge = campaign_store.load_knowledge(client_id)
    if not request.preserve_review:
        campaign.brief_reviewed_at = None
        for previous in campaign.template_candidates:
            previous.status = "proposed"
            previous.approved = False
            previous.approved_at = None
    brief, candidates, engine, warnings = campaign_analysis.generate(
        campaign, brand, knowledge, use_ai=request.use_ai
    )
    campaign.brief = brief
    campaign.template_candidates = candidates
    try:
        campaign_creative.render_candidate_previews(campaign, brand.name, candidates)
    except Exception:  # noqa: BLE001 - el brief sigue siendo util sin preview
        warnings.append(
            "El brief quedo listo, pero no se pudieron dibujar las vistas previas de plantilla."
        )
    campaign.analysis_engine = engine
    campaign.status = (
        "templates_approved" if any(item.approved for item in candidates) else "templates_proposed"
    )
    campaign.warnings = list(dict.fromkeys([*campaign.warnings, *warnings]))
    campaign_store.save_campaign(campaign)
    return GenerateBriefResponse(
        campaign_id=campaign_id,
        brief=brief,
        template_candidates=candidates,
        engine=engine,
        warnings=warnings,
    )


@router.put(
    "/{client_id}/campaigns/{campaign_id}/brief",
    response_model=CampaignBrief,
)
def revise_brief(
    client_id: str,
    campaign_id: str,
    request: CampaignBriefPatchRequest,
) -> CampaignBrief:
    """Persiste la correccion humana y la reutiliza en futuros reanalisis."""

    campaign = _campaign_or_404(client_id, campaign_id)
    if campaign.brief is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Genera el brief antes de guardar correcciones.",
        )
    values = request.model_dump(exclude_unset=True)
    feedback = str(values.pop("feedback", "") or "").strip()
    values = {key: value for key, value in values.items() if value is not None}
    if not values and not feedback:
        return campaign.brief
    try:
        campaign.brief = CampaignBrief.model_validate(
            {**campaign.brief.model_dump(mode="json"), **values}
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    overrides = campaign.meta.get("brief_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides.update(values)
    campaign.meta["brief_overrides"] = overrides
    campaign.brief_reviewed_at = utcnow()
    campaign.template_candidates = []
    campaign.status = "ready_for_brief"
    campaign_store.save_campaign(campaign)

    knowledge = campaign_store.load_knowledge(client_id)
    # Una plantilla aprobada contra la versión anterior del brief deja de ser
    # una aprobación vigente. Sus reglas aprendidas permanecen, pero la nueva
    # candidata necesita otra revisión humana.
    knowledge.approved_candidates = [
        item for item in knowledge.approved_candidates if item.campaign_id != campaign_id
    ]
    if feedback:
        learned = f"Correccion de brief en {campaign.name}: {feedback}"[:1000]
    else:
        summary = "; ".join(
            f"{key}={value if isinstance(value, str) else ', '.join(value)}"
            for key, value in values.items()
        )
        learned = f"Correccion manual en {campaign.name}: {summary}"[:1000]
    if learned and learned not in knowledge.learned_rules:
        knowledge.learned_rules.append(learned)
        knowledge.learned_rules = knowledge.learned_rules[-80:]
        campaign_store.save_knowledge(knowledge)
    return campaign.brief


def _decide_candidate(
    client_id: str,
    campaign_id: str,
    candidate_id: str,
    request: CandidateDecisionRequest,
    *,
    approve: bool,
) -> CandidateDecisionResponse:
    campaign = _campaign_or_404(client_id, campaign_id)
    if not campaign.brief_reviewed_at:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Revisa y guarda el brief antes de aprobar una plantilla.",
        )
    candidate = next(
        (item for item in campaign.template_candidates if item.candidate_id == candidate_id), None
    )
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No existe esa propuesta de plantilla en la campana."
        )
    candidate.status = "approved" if approve else "rejected"
    candidate.approved = approve
    candidate.approved_at = utcnow() if approve else None
    candidate.decision_notes = request.notes.strip()
    campaign.status = (
        "templates_approved"
        if any(item.approved for item in campaign.template_candidates)
        else "templates_proposed"
    )
    campaign_store.save_campaign(campaign)

    knowledge = campaign_store.load_knowledge(client_id)
    knowledge.approved_candidates = [
        item
        for item in knowledge.approved_candidates
        if not (
            item.campaign_id == campaign_id and item.candidate_id == candidate.candidate_id
        )
    ]
    if approve:
        knowledge.approved_candidates.append(
            ApprovedCandidateMemory(
                campaign_id=campaign_id,
                candidate_id=candidate.candidate_id,
                name=candidate.name,
                category=candidate.category,
                slots=[slot.key for slot in candidate.slots],
                layout_intent=candidate.layout_intent,
                candidate_snapshot=candidate.model_dump(mode="json"),
                approved_at=candidate.approved_at or utcnow(),
            )
        )
    if request.notes.strip():
        learned = request.notes.strip()[:1000]
        if approve:
            learned = f"Aprobado: {learned}"
        else:
            learned = f"Evitar: {learned}"
        if learned not in knowledge.learned_rules:
            knowledge.learned_rules.append(learned)
            knowledge.learned_rules = knowledge.learned_rules[-80:]
    campaign_store.save_knowledge(knowledge)
    return CandidateDecisionResponse(
        campaign_id=campaign_id,
        candidate=candidate,
        campaign_status=campaign.status,
    )


@router.post(
    "/{client_id}/campaigns/{campaign_id}/template-candidates/{candidate_id}/approve",
    response_model=CandidateDecisionResponse,
)
def approve_candidate(
    client_id: str,
    campaign_id: str,
    candidate_id: str,
    request: CandidateDecisionRequest = CandidateDecisionRequest(),
) -> CandidateDecisionResponse:
    return _decide_candidate(
        client_id, campaign_id, candidate_id, request, approve=True
    )


@router.post(
    "/{client_id}/campaigns/{campaign_id}/template-candidates/{candidate_id}/reject",
    response_model=CandidateDecisionResponse,
)
def reject_candidate(
    client_id: str,
    campaign_id: str,
    candidate_id: str,
    request: CandidateDecisionRequest = CandidateDecisionRequest(),
) -> CandidateDecisionResponse:
    return _decide_candidate(
        client_id, campaign_id, candidate_id, request, approve=False
    )


@router.post(
    "/{client_id}/campaigns/{campaign_id}/template-candidates/{candidate_id}/revise",
    response_model=GenerateBriefResponse,
)
def revise_candidate(
    client_id: str,
    campaign_id: str,
    candidate_id: str,
    request: CandidateDecisionRequest,
) -> GenerateBriefResponse:
    """Regenera propuestas usando una corrección visible antes de aprobar."""

    brand = _brand_or_404(client_id)
    campaign = _campaign_or_404(client_id, campaign_id)
    if campaign.brief is None or not campaign.brief_reviewed_at:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Revisa y guarda el brief antes de corregir las plantillas.",
        )
    note = request.notes.strip()
    if not note:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Escribe la correccion que debe aplicar la IA.",
        )
    current = next(
        (item for item in campaign.template_candidates if item.candidate_id == candidate_id),
        None,
    )
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa propuesta.")
    feedback = campaign.meta.get("template_feedback", {})
    if not isinstance(feedback, dict):
        feedback = {}
    feedback[current.category] = note[:1000]
    campaign.meta["template_feedback"] = feedback
    # La propuesta corregida y cualquier salida materialmente distinta vuelven
    # a estado propuesto. `_finalise_candidates` solo conserva decisiones cuya
    # firma sea exactamente igual.
    current.status = "proposed"
    current.approved = False
    current.approved_at = None
    knowledge = campaign_store.load_knowledge(client_id)
    knowledge.approved_candidates = [
        item
        for item in knowledge.approved_candidates
        if not (item.campaign_id == campaign_id and item.category == current.category)
    ]
    learned = f"Correccion solicitada para {current.category}: {note}"[:1000]
    if learned not in knowledge.learned_rules:
        knowledge.learned_rules.append(learned)
        knowledge.learned_rules = knowledge.learned_rules[-80:]
    campaign_store.save_knowledge(knowledge)

    brief, candidates, engine, warnings = campaign_analysis.generate(
        campaign, brand, knowledge, use_ai=True
    )
    campaign.brief = brief
    campaign.template_candidates = candidates
    try:
        campaign_creative.render_candidate_previews(campaign, brand.name, candidates)
    except Exception:  # noqa: BLE001
        warnings.append("La correccion se guardo, pero no se pudieron redibujar los previews.")
    campaign.analysis_engine = engine
    campaign.status = "templates_proposed"
    campaign.warnings = list(dict.fromkeys([*campaign.warnings, *warnings]))
    campaign_store.save_campaign(campaign)
    return GenerateBriefResponse(
        campaign_id=campaign_id,
        brief=brief,
        template_candidates=candidates,
        engine=engine,
        warnings=warnings,
    )


@router.get(
    "/{client_id}/campaigns/{campaign_id}/template-candidates/{candidate_id}/preview",
    response_class=FileResponse,
)
def candidate_preview(
    client_id: str,
    campaign_id: str,
    candidate_id: str,
    aspect: str = "portrait",
) -> FileResponse:
    campaign = _campaign_or_404(client_id, campaign_id)
    candidate = next(
        (item for item in campaign.template_candidates if item.candidate_id == candidate_id),
        None,
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa propuesta.")
    sizes = {"portrait", "square", "story", "landscape"}
    if aspect not in sizes:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "aspect debe ser portrait, square, story o landscape.",
        )
    target = campaign_store.campaign_path(
        client_id,
        campaign_id,
        f"analysis/templates/{candidate_id}-{aspect}.png",
    )
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La vista previa no esta disponible.")
    return FileResponse(target, media_type="image/png")


async def _matrix_payload(upload: UploadFile) -> tuple[str, bytes]:
    name = (upload.filename or "matriz.csv")[:240]
    payload = await upload.read(5 * 1024 * 1024 + 1)
    await upload.close()
    if len(payload) > 5 * 1024 * 1024:
        raise FileValidationError("La matriz supera 5 MB.")
    if not payload:
        raise FileValidationError("La matriz esta vacia.")
    return name, payload


def _default_format_list(raw: str) -> list[str]:
    """Comparte el contrato JSON entre previsualización y producción."""

    try:
        decoded = json.loads(raw or "[]")
        if not isinstance(decoded, list):
            raise ValueError
        return [str(item) for item in decoded if str(item).strip()]
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "default_formats debe ser una lista JSON.",
        ) from exc


def _planned_matrix_pieces(
    rows: list[production_matrix.MatrixRow], default_formats: list[str]
) -> int:
    """Cuenta exactamente lo que se produciría, resolviendo aliases y duplicados."""

    defaults = default_formats or ["meta_feed_4_5"]
    try:
        return sum(
            len(campaign_creative.resolve_formats(row.formatos or defaults))
            * row.cantidad_propuestas
            for row in rows
        )
    except campaign_creative.CampaignProductionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


_MATRIX_FIELD_LABELS = {
    "producto": "producto",
    "titular": "titular",
    "subtitulo": "subtítulo",
    "precio_anterior": "precio anterior",
    "precio": "precio",
    "cuota": "cuota",
    "descuento": "descuento",
    "cta": "CTA",
    "legal": "legal",
    "vigencia": "vigencia",
}


def _matrix_requested_fields(row: production_matrix.MatrixRow) -> list[str]:
    """Campos que una plantilla debe poder representar para esta fila."""

    fields: list[str] = []
    if production_matrix.product_count(row):
        fields.append("producto")
    for field in (
        "titular",
        "subtitulo",
        "precio_anterior",
        "precio_actual",
        "cuota",
        "descuento",
        "cta",
        "legal",
        "vigencia",
    ):
        if getattr(row, field) not in (None, ""):
            # La matriz puede llamarlo ``precio_actual``, pero las plantillas
            # guardan el slot canónico ``precio``. El plan expone ese mismo
            # nombre para que UI y aprobación hablen del mismo campo.
            fields.append("precio" if field == "precio_actual" else field)
    return fields


def _matrix_preview_plans(
    campaign: Campaign, rows: list[production_matrix.MatrixRow]
) -> list[dict[str, object]]:
    """Explica la selección antes de guardar/producir una matriz.

    Esta comprobación no mira fotos: la pantalla permite cargar la matriz antes
    de subir sus productos. Sí usa exactamente el selector de producción, por
    lo que una incompatibilidad de slots, combo o plantilla forzada se ve antes
    de iniciar la tanda y sin descartar el borrador válido.
    """

    approved = [candidate for candidate in campaign.template_candidates if candidate.approved]
    plans: list[dict[str, object]] = []
    for row in rows:
        fields = _matrix_requested_fields(row)
        candidate = production_matrix.select_template(row, approved)
        product_total = production_matrix.product_count(row)
        if candidate is not None:
            plans.append(
                {
                    "row_number": row.row_number,
                    "product_count": product_total,
                    "status": "ready",
                    "template": {
                        "candidate_id": candidate.candidate_id,
                        "name": candidate.name,
                    },
                    "required_fields": fields,
                    "ai_fillable_fields": sorted(production_matrix.ai_fillable_fields(candidate)),
                    "message": f"Se usará «{candidate.name}» para esta fila.",
                }
            )
            continue

        content = [
            (f"{product_total} productos" if product_total > 1 else "un producto")
            if field == "producto" else _MATRIX_FIELD_LABELS[field]
            for field in fields
        ]
        description = ", ".join(content) or "el contenido de la fila"
        sugerida = _auto_template_for_row(row, approved)
        sin_hueco = _fields_without_slot(fields, approved)
        if not approved:
            plan_status = "needs_approval"
            message = (
                "Aún no hay una plantilla aprobada. Revisa y aprueba una propuesta antes de producir."
            )
        elif row.plantilla and sugerida is not None:
            # El único problema es la plantilla forzada: decir cuál sirve evita
            # mandar a alguien a probar una por una, o a buscar una alternativa
            # que puede no existir.
            plan_status = "incompatible"
            message = (
                f"La plantilla «{row.plantilla}» no admite {description}, pero "
                f"«{sugerida.name}» sí. Déjala en automática y se usará esa."
            )
        elif sin_hueco:
            etiquetas = ", ".join(_MATRIX_FIELD_LABELS[field] for field in sin_hueco)
            plan_status = "incompatible"
            message = (
                f"Ninguna plantilla aprobada tiene hueco para {etiquetas}. "
                "Vacía esas casillas o aprueba una plantilla que las incluya: "
                "escribirlas sin sitio dejaría el arte incompleto."
            )
        elif row.plantilla:
            plan_status = "incompatible"
            message = (
                f"La plantilla «{row.plantilla}» no admite esta combinación "
                f"({description}) y ninguna otra aprobada la admite entera."
            )
        else:
            plan_status = "incompatible"
            message = (
                f"Ninguna plantilla aprobada admite {description} a la vez. "
                "Aprueba una propuesta compatible o divide la fila."
            )
        plans.append(
            {
                "row_number": row.row_number,
                "product_count": product_total,
                "status": plan_status,
                "template": None,
                "required_fields": fields,
                "ai_fillable_fields": [],
                "message": message,
                "suggested_template": (
                    {"candidate_id": sugerida.candidate_id, "name": sugerida.name}
                    if sugerida is not None
                    else None
                ),
                "blocking_fields": sin_hueco,
            }
        )
    return plans


def _auto_template_for_row(row: production_matrix.MatrixRow, approved: list) -> object | None:
    """La plantilla que se elegiría si la fila no forzara ninguna."""

    if not row.plantilla:
        return None
    libre = row.model_copy(update={"plantilla": ""})
    return production_matrix.select_template(libre, approved)


def _fields_without_slot(fields: list[str], approved: list) -> list[str]:
    """Campos escritos en la fila que ninguna plantilla aprobada sabe colocar.

    Es la diferencia entre «prueba otra plantilla» y «esto no cabe en ninguna».
    Lo segundo solo se arregla vaciando la casilla o aprobando otra propuesta,
    y decirlo por su nombre ahorra el recorrido a ciegas por el desplegable.
    """

    # El mismo lector de slots que usa el selector: si divergen, la UI diría
    # que un campo no cabe en una plantilla que sí lo acepta.
    huecos = {
        production_matrix._slot_id(slot)
        for candidate in approved
        for slot in getattr(candidate, "slots", []) or []
    }
    huecos.discard("")
    sin_sitio: list[str] = []
    for field in fields:
        if field == "producto":
            continue
        alias = {field, "precio"} if field == "precio" else {field}
        if not alias & huecos:
            sin_sitio.append(field)
    return sin_sitio


def _save_matrix_draft(
    campaign: Campaign,
    *,
    matrix_name: str,
    payload: bytes,
) -> str:
    """Guarda la matriz ya validada sin exponer una ruta del servidor.

    ``File`` no sobrevive a un F5, pero una fila ya validada no debería exigir
    volver a subir el XLSX/CSV. El identificador opaco queda ligado a la
    campaña actual y la producción lo vuelve a parsear al arrancar, de modo que
    no confía en las filas que el navegador guardó en sessionStorage.
    """

    suffix = Path(matrix_name).suffix.lower()
    if suffix not in {".csv", ".tsv", ".xlsx"}:
        suffix = ".csv"
    draft_id = new_id()
    folder = campaign_store.production_draft_dir(
        campaign.client_id, campaign.campaign_id, draft_id
    )
    target = folder / f"matrix{suffix}"
    try:
        target.write_bytes(payload)
        campaign.meta["production_matrix_draft"] = {
            "draft_id": draft_id,
            "filename": matrix_name[:240],
            "stored_path": campaign_store.relative_path(
                campaign.client_id, campaign.campaign_id, target
            ),
        }
        campaign_store.save_campaign(campaign)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return draft_id


def _load_matrix_draft(
    campaign: Campaign, raw_draft_id: str
) -> tuple[str, bytes]:
    """Lee exclusivamente la última matriz guardada para esta campaña."""

    draft_id = raw_draft_id.strip()
    try:
        validate_uuid(draft_id, "matrix_draft_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    draft = campaign.meta.get("production_matrix_draft")
    if not isinstance(draft, dict) or draft.get("draft_id") != draft_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "La matriz guardada ya no está disponible. Vuelve a validarla.",
        )
    relative = draft.get("stored_path")
    filename = str(draft.get("filename") or "matriz.csv")[:240]
    if not isinstance(relative, str) or not relative:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "La matriz guardada está incompleta. Vuelve a validarla.",
        )
    try:
        target = campaign_store.campaign_path(
            campaign.client_id, campaign.campaign_id, relative
        )
        payload = target.read_bytes()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No se encontró el archivo de matriz guardado. Vuelve a validarlo.",
        ) from exc
    if not payload or len(payload) > 5 * 1024 * 1024:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "La matriz guardada ya no es válida. Vuelve a subirla.",
        )
    return filename, payload


def _preflight_production_order(
    campaign: Campaign,
    rows: list[production_matrix.MatrixRow],
    products: dict[str, Path],
    planned_pieces: int,
) -> None:
    """Rechaza errores deterministas antes de dejar una orden en cola."""

    approved = [candidate for candidate in campaign.template_candidates if candidate.approved]
    if not approved:
        raise campaign_creative.CampaignProductionError(
            "Aprueba al menos una plantilla antes de producir."
        )
    if planned_pieces > settings.campaign_max_pieces:
        raise campaign_creative.CampaignProductionError(
            f"La tanda pide {planned_pieces} artes; el limite por tanda es "
            f"{settings.campaign_max_pieces}. Dividela en varias matrices."
        )
    missing: list[tuple[production_matrix.MatrixRow, int, int]] = []
    for row in rows:
        expected = production_matrix.product_count(row)
        # ``match_product_paths`` tiene un fallback útil para una sola foto
        # cargada. No debe activarse cuando la matriz no pidió producto: de lo
        # contrario una pieza institucional acabaría mostrando esa foto.
        found = (
            len(campaign_creative.match_product_paths(row, products))
            if expected
            else 0
        )
        if found < expected:
            missing.append((row, expected, found))
    if missing:
        labels = ", ".join(
            f"fila {row.row_number} ({found}/{expected} imagenes: "
            f"{row.imagen or row.producto or 'sin imagen'})"
            for row, expected, found in missing[:8]
        )
        raise campaign_creative.CampaignProductionError(
            "Faltan imagenes de producto para " + labels
            + ". Cada producto del combo necesita un archivo que coincida con la columna imagen."
        )
    # Solo después de comprobar las fotos se resuelve la plantilla. Así una
    # fila de combo con una imagen faltante recibe la corrección útil (en vez
    # de ocultarla detrás de otra incompatibilidad), mientras las filas sin
    # producto siguen siendo válidas si una institucional las soporta.
    for row in rows:
        if production_matrix.select_template(row, approved) is None:
            raise campaign_creative.CampaignProductionError(
                f"Fila {row.row_number}: ninguna plantilla aprobada soporta ese contenido o combo."
            )


async def _read_product_upload(
    upload: UploadFile, index: int
) -> tuple[str, str, str, bytes, int, int]:
    """Lee una foto una vez y la valida antes de guardarla en cualquier sitio."""

    filename = (upload.filename or f"producto-{index + 1}.png")[:240]
    suffix = Path(filename).suffix.lower()
    if suffix not in PRODUCT_IMAGE_EXTENSIONS:
        await upload.close()
        raise FileValidationError(
            f"'{filename}' no es una imagen compatible. Usa PNG, JPG, WEBP, BMP, GIF, TIFF o AVIF."
        )
    payload = await upload.read(min(settings.max_upload_bytes, 100 * 1024 * 1024) + 1)
    await upload.close()
    if len(payload) > min(settings.max_upload_bytes, 100 * 1024 * 1024):
        raise FileValidationError(f"'{filename}' supera el limite de imagen de producto.")
    decoded_format = ""
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(payload)) as probe:
            width, height = probe.size
            decoded_format = str(probe.format or "").upper()
            if width * height > settings.campaign_max_product_pixels:
                raise FileValidationError(f"'{filename}' declara demasiados pixeles.")
    except FileValidationError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise FileValidationError(f"'{filename}' no se puede decodificar.") from exc
    media_type = PRODUCT_IMAGE_MEDIA_TYPES.get(decoded_format)
    if media_type is None:
        raise FileValidationError(
            f"'{filename}' usa un formato de imagen que no se puede servir de forma segura."
        )
    return filename, suffix, media_type, payload, width, height


async def _store_production_assets(
    campaign: Campaign, uploads: list[UploadFile]
) -> tuple[list[ProductionAsset], list[str]]:
    """Persiste fotos para producción sin convertirlas en fuentes del brief."""

    if not uploads:
        return campaign.production_assets, []
    if len(uploads) > 200:
        raise FileValidationError("Sube como maximo 200 imagenes de producto por tanda.")
    known_names = {asset.filename.casefold(): asset for asset in campaign.production_assets}
    total_bytes = sum(asset.size_bytes for asset in campaign.production_assets)
    created: list[ProductionAsset] = []
    created_folders: list[Path] = []
    warnings: list[str] = []
    try:
        for index, upload in enumerate(uploads):
            filename, suffix, media_type, payload, width, height = await _read_product_upload(
                upload, index
            )
            digest = hashlib.sha256(payload).hexdigest()
            existing = known_names.get(filename.casefold())
            if existing is not None:
                if existing.sha256 == digest:
                    warnings.append(f"'{filename}' ya estaba cargada y se conservara esa imagen.")
                    continue
                raise FileValidationError(
                    f"Ya existe una imagen llamada '{filename}'. Renombra una de las dos para evitar cruces ambiguos."
                )
            total_bytes += len(payload)
            if total_bytes > settings.campaign_max_product_batch_bytes:
                raise FileValidationError(
                    "Las imagenes de producto superan el limite conjunto de "
                    f"{settings.campaign_max_product_batch_mb} MB por campaña."
                )
            asset = ProductionAsset(
                filename=filename,
                media_type=media_type,
                extension=suffix,
                size_bytes=len(payload),
                sha256=digest,
                stored_path="",
                width=width,
                height=height,
            )
            folder = campaign_store.production_asset_dir(
                campaign.client_id, campaign.campaign_id, asset.asset_id
            )
            created_folders.append(folder)
            target = folder / f"original{suffix}"
            target.write_bytes(payload)
            asset.stored_path = campaign_store.relative_path(
                campaign.client_id, campaign.campaign_id, target
            )
            created.append(asset)
            known_names[filename.casefold()] = asset
    except Exception:
        for folder in created_folders:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    if created:
        campaign.production_assets.extend(created)
        campaign_store.save_campaign(campaign)
    return campaign.production_assets, warnings


def _stored_product_assets(campaign: Campaign, asset_ids: list[str]) -> dict[str, Path]:
    """Resuelve únicamente activos declarados por el cliente, sin rutas libres."""

    selected = list(dict.fromkeys(item.strip() for item in asset_ids if item.strip()))
    available = {asset.asset_id: asset for asset in campaign.production_assets}
    products: dict[str, Path] = {}
    for asset_id in selected:
        asset = available.get(asset_id)
        if asset is None:
            raise FileValidationError("Una imagen seleccionada ya no existe en esta campaña. Vuelve a cargarla.")
        target = campaign_store.campaign_path(
            campaign.client_id, campaign.campaign_id, asset.stored_path
        )
        if not target.exists() or not target.is_file():
            raise FileValidationError(
                f"La imagen '{asset.filename}' ya no está disponible. Vuelve a cargarla."
            )
        if asset.filename.casefold() in {name.casefold() for name in products}:
            raise FileValidationError(
                f"Hay dos imágenes llamadas '{asset.filename}'. Elimina o renombra una antes de producir."
            )
        products[asset.filename] = target
    return products


def _persist_production_job(
    campaign: Campaign,
    brand_name: str,
    rows: list[production_matrix.MatrixRow],
    products: dict[str, Path],
    *,
    matrix_name: str,
    matrix_payload: bytes,
    asset_ids: list[str],
    default_formats: list[str],
    use_ai_copy: bool,
    planned_pieces: int,
) -> ProductionJob:
    """Guarda todo lo que necesita el worker antes de tocar el broker.

    Los activos se copian al directorio de la orden, en vez de referenciarlos
    desde la biblioteca de productos. Así el usuario puede limpiar o reemplazar
    una foto mientras la tarea espera sin corromper una tanda que ya aprobó.
    """

    job = ProductionJob(
        client_id=campaign.client_id,
        campaign_id=campaign.campaign_id,
        matrix_filename=matrix_name[:240],
        product_asset_ids=list(dict.fromkeys(asset_ids)),
        rows=[row.model_dump(mode="json") for row in rows],
        default_formats=default_formats,
        use_ai_copy=use_ai_copy,
        brand_name=brand_name[:240],
        campaign_snapshot=campaign.model_dump(mode="json"),
        planned_pieces=planned_pieces,
    )
    job_dir = campaign_store.production_job_dir(
        campaign.client_id, campaign.campaign_id, job.task_id
    )
    try:
        # No se usa el nombre suministrado por el navegador como ruta. Solo se
        # conserva para mostrarlo y el worker recibe archivos de nombres fijos.
        suffix = Path(matrix_name).suffix.lower()
        if suffix not in {".csv", ".tsv", ".xlsx"}:
            suffix = ".csv"
        matrix_target = job_dir / f"matrix{suffix}"
        matrix_target.write_bytes(matrix_payload)
        job.matrix_path = campaign_store.relative_path(
            campaign.client_id, campaign.campaign_id, matrix_target
        )

        products_dir = job_dir / "products"
        products_dir.mkdir(parents=True, exist_ok=True)
        for index, (filename, source) in enumerate(products.items()):
            extension = Path(filename).suffix.lower()
            # Todos vienen de ProductionAsset o de _read_product_upload, pero
            # mantenemos esta guarda para que una biblioteca antigua no cree
            # una ruta impredecible dentro de la orden.
            if extension not in PRODUCT_IMAGE_EXTENSIONS:
                raise FileValidationError(
                    f"La imagen '{filename}' no tiene una extensión compatible."
                )
            target = products_dir / f"{index:03d}{extension}"
            shutil.copyfile(source, target)
            job.product_files[filename] = campaign_store.relative_path(
                campaign.client_id, campaign.campaign_id, target
            )
        campaign_store.save_production_job(job)
        return job
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


@router.post(
    "/{client_id}/campaigns/{campaign_id}/production/assets",
    response_model=ProductionAssetsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_production_assets(
    client_id: str,
    campaign_id: str,
    files: list[UploadFile] = File(...),
) -> ProductionAssetsResponse:
    campaign = _campaign_or_404(client_id, campaign_id)
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Sube al menos una imagen.")
    assets, warnings = await _store_production_assets(campaign, files)
    return ProductionAssetsResponse(
        campaign_id=campaign_id,
        assets=assets,
        warnings=warnings,
    )


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production/assets/{asset_id}/file",
    response_class=FileResponse,
)
def production_asset_file(client_id: str, campaign_id: str, asset_id: str) -> FileResponse:
    campaign = _campaign_or_404(client_id, campaign_id)
    asset = next((item for item in campaign.production_assets if item.asset_id == asset_id), None)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa imagen de producto.")
    target = campaign_store.campaign_path(client_id, campaign_id, asset.stored_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La imagen ya no esta disponible.")
    # Es una URL de vista previa dentro de la interfaz, no una descarga: con
    # Content-Disposition attachment algunos navegadores no muestran la foto
    # en el inventario aunque el archivo sí se hubiera guardado.
    return FileResponse(target, media_type=asset.media_type)


@router.delete(
    "/{client_id}/campaigns/{campaign_id}/production/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_production_asset(client_id: str, campaign_id: str, asset_id: str) -> None:
    campaign = _campaign_or_404(client_id, campaign_id)
    asset = next((item for item in campaign.production_assets if item.asset_id == asset_id), None)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa imagen de producto.")
    target = campaign_store.campaign_path(client_id, campaign_id, asset.stored_path)
    campaign.production_assets = [item for item in campaign.production_assets if item.asset_id != asset_id]
    campaign_store.save_campaign(campaign)
    shutil.rmtree(target.parent, ignore_errors=True)


@router.post(
    "/{client_id}/campaigns/{campaign_id}/production/preview",
)
async def preview_production_matrix(
    client_id: str,
    campaign_id: str,
    matrix: UploadFile = File(...),
    default_formats: str = Form("[]"),
) -> dict[str, object]:
    """Parsea la matriz con el mismo contrato que usara produccion.

    La UI no mantiene un segundo parser: CSV, TSV y XLSX se validan aqui para
    que la revision que ve el usuario sea exactamente la tanda que se generara.
    """
    campaign = _campaign_or_404(client_id, campaign_id)
    matrix_name, payload = await _matrix_payload(matrix)
    try:
        rows = production_matrix.parse_matrix(payload, matrix_name)
    except production_matrix.MatrixParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    formats = _default_format_list(default_formats)
    planned = _planned_matrix_pieces(rows, formats)
    draft_id = _save_matrix_draft(
        campaign, matrix_name=matrix_name, payload=payload
    )
    return {
        "rows": [row.model_dump(mode="json") for row in rows],
        "total_rows": len(rows),
        "requested_pieces": planned,
        "matrix_draft_id": draft_id,
        "plans": _matrix_preview_plans(campaign, rows),
    }


def _row_preview_path(campaign: Campaign, row_number: int) -> Path:
    carpeta = campaign_store.production_root(campaign.client_id, campaign.campaign_id) / "row-previews"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"fila-{max(0, min(9999, row_number)):04d}.jpg"


@router.post("/{client_id}/campaigns/{campaign_id}/production/row-preview")
async def preview_production_row(
    client_id: str,
    campaign_id: str,
    matrix: UploadFile = File(...),
    row_number: int = Form(...),
    piece_format: str = Form(""),
) -> dict[str, object]:
    """Compone una sola fila para verla antes de lanzar la tanda.

    Hasta ahora la unica forma de ver como queda un arte era producir la tanda
    entera y abrir el ZIP. Esto usa el mismo renderer y las fotos ya cargadas
    en la campana, a resolucion de pantalla, y dice en voz alta lo que la vista
    no puede prometer: que el copy que la IA rellenara al producir aqui sale
    vacio.
    """

    campaign = _campaign_or_404(client_id, campaign_id)
    brand = _brand_or_404(client_id)
    matrix_name, payload = await _matrix_payload(matrix)
    try:
        rows = production_matrix.parse_matrix(payload, matrix_name)
    except production_matrix.MatrixParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    row = next((item for item in rows if item.row_number == row_number), None)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"La matriz no trae una fila {row_number}."
        )

    approved = [item for item in campaign.template_candidates if item.approved]
    if not approved:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Aprueba una plantilla antes de previsualizar: sin ella no hay composicion que mostrar.",
        )
    warnings: list[str] = []
    candidate = production_matrix.select_template(row, approved)
    if candidate is None and row.plantilla:
        # La fila fuerza una plantilla que no le sirve. En vez de negarse, se
        # ensena la que se usaria en automatica y se dice por que.
        candidate = _auto_template_for_row(row, approved)
        if candidate is not None:
            warnings.append(
                f"«{row.plantilla}» no admite esta fila; la vista usa «{candidate.name}», "
                "que es la que se aplicaria dejando la plantilla en automatica."
            )
    if candidate is None:
        plan = next(iter(_matrix_preview_plans(campaign, [row])), {})
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            str(plan.get("message") or "Ninguna plantilla aprobada admite esta fila."),
        )

    # Todas las fotos de la campana: la vista previa no debe obligar a marcar
    # nada, solo a mirar. Si la que pide la fila no esta, se compone el arte
    # sin producto y se avisa; ese hueco tambien es informacion util.
    todas = _stored_product_assets(
        campaign, [asset.asset_id for asset in campaign.production_assets]
    )
    esperadas = production_matrix.product_count(row)
    product_paths = campaign_creative.match_product_paths(row, todas) if esperadas else []
    if esperadas and len(product_paths) < esperadas:
        warnings.append(
            f"Faltan {esperadas - len(product_paths)} de {esperadas} fotos de esta fila: "
            "la vista deja el hueco del producto vacio."
        )
    faltan_copy = sorted(
        production_matrix.ai_fillable_fields(candidate)
        & {
            field
            for field in ("titular", "subtitulo", "cta")
            if getattr(row, field, None) in (None, "")
        }
    )
    if faltan_copy:
        warnings.append(
            "Aqui se ve vacio " + ", ".join(faltan_copy)
            + ": la IA lo redacta al producir, no en la vista previa."
        )

    target = _row_preview_path(campaign, row.row_number)
    try:
        format_id, width, height = await run_in_threadpool(
            campaign_creative.render_row_preview,
            campaign, brand.name, row, candidate, product_paths, target,
            format_token=piece_format,
        )
    except campaign_creative.CampaignProductionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {
        "row_number": row.row_number,
        "template": {"candidate_id": candidate.candidate_id, "name": candidate.name},
        "format": format_id,
        "width": width,
        "height": height,
        "preview_url": (
            f"/clients/{client_id}/campaigns/{campaign_id}/production/"
            f"row-preview/{row.row_number}?v={int(target.stat().st_mtime)}"
        ),
        "warnings": warnings,
    }


@router.get("/{client_id}/campaigns/{campaign_id}/production/row-preview/{row_number}")
def get_production_row_preview(
    client_id: str, campaign_id: str, row_number: int
) -> FileResponse:
    campaign = _campaign_or_404(client_id, campaign_id)
    target = _row_preview_path(campaign, row_number)
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Esa vista previa aun no se ha generado."
        )
    return FileResponse(target, media_type="image/jpeg")


@router.post(
    "/{client_id}/campaigns/{campaign_id}/production",
    response_model=ProductionBatch | ProductionTaskStatus,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_201_CREATED: {"model": ProductionBatch},
        status.HTTP_202_ACCEPTED: {"model": ProductionTaskStatus},
    },
)
async def produce_campaign(
    client_id: str,
    campaign_id: str,
    response: Response,
    matrix: UploadFile | None = File(None),
    matrix_draft_id: str = Form(""),
    product_files: list[UploadFile] = File(default=[]),
    product_asset_ids: str = Form("[]"),
    default_formats: str = Form("[]"),
    use_ai_copy: bool = Form(True),
) -> ProductionBatch | ProductionTaskStatus:
    """Persiste y encola una tanda sin mantener la petición HTTP abierta.

    En modo ``CELERY_TASK_ALWAYS_EAGER`` (pruebas y desarrollo) se conserva el
    contrato histórico: devuelve la tanda terminada con HTTP 201. En producción
    devuelve HTTP 202 y una orden consultable con ``/production/tasks/{id}``.
    """

    brand = _brand_or_404(client_id)
    campaign = _campaign_or_404(client_id, campaign_id)
    if not campaign.brief_reviewed_at:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Revisa y guarda el brief antes de producir.",
        )
    if matrix is not None and matrix_draft_id.strip():
        await matrix.close()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Envía una matriz nueva o una matriz guardada, no las dos.",
        )
    if matrix is not None:
        matrix_name, payload = await _matrix_payload(matrix)
    elif matrix_draft_id.strip():
        matrix_name, payload = _load_matrix_draft(campaign, matrix_draft_id)
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Sube o valida una matriz antes de producir.",
        )
    try:
        rows = production_matrix.parse_matrix(payload, matrix_name)
    except production_matrix.MatrixParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    formats = _default_format_list(default_formats)
    try:
        decoded_asset_ids = json.loads(product_asset_ids or "[]")
        if not isinstance(decoded_asset_ids, list) or not all(
            isinstance(item, str) for item in decoded_asset_ids
        ):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "product_asset_ids debe ser una lista JSON.",
        ) from exc

    # Los archivos enviados junto a la matriz también pasan a ser activos de la
    # campaña antes de encolar. Dejarían de existir al cerrar esta petición, que
    # era la causa de tandas que «empezaban» pero no tenían fotos en el worker.
    direct_name_list = [
        (upload.filename or f"producto-{index + 1}.png")[:240].casefold()
        for index, upload in enumerate(product_files)
    ]
    direct_names = set(direct_name_list)
    if len(direct_names) != len(direct_name_list):
        raise FileValidationError(
            "Hay dos imágenes nuevas con el mismo nombre. Renombra una antes de producir."
        )
    try:
        staged = _stored_product_assets(campaign, decoded_asset_ids)
        selected_names = {name.casefold() for name in staged}
        collision = next((name for name in direct_names if name in selected_names), None)
        if collision:
            raise FileValidationError(
                f"La imagen '{collision}' esta repetida entre las ya cargadas y las nuevas."
            )
        assets, _warnings = await _store_production_assets(campaign, product_files)
        direct_ids = [
            asset.asset_id
            for asset in assets
            if asset.filename.casefold() in direct_names
        ]
        selected_asset_ids = list(dict.fromkeys([*decoded_asset_ids, *direct_ids]))
        products = _stored_product_assets(campaign, selected_asset_ids)
        planned = _planned_matrix_pieces(rows, formats)
        _preflight_production_order(campaign, rows, products, planned)
        job = _persist_production_job(
            campaign,
            brand.name,
            rows,
            products,
            matrix_name=matrix_name,
            matrix_payload=payload,
            asset_ids=selected_asset_ids,
            default_formats=formats,
            use_ai_copy=use_ai_copy,
            planned_pieces=planned,
        )
    except campaign_creative.CampaignProductionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    try:
        from app.worker import produce_campaign_batch_task

        # La clave del job y la de Celery son una sola. No hay que guardar una
        # tabla de mapeo y una consulta sigue funcionando si Redis se reinicia.
        produce_campaign_batch_task.apply_async(  # type: ignore[attr-defined]
            args=(client_id, campaign_id, job.task_id), task_id=job.task_id
        )
    except campaign_creative.CampaignProductionError as exc:
        # En eager la excepción sale de apply_async; el worker ya dejó el error
        # durable en job.json y mantenemos el 422 claro que tenía la API previa.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        # Un Redis caído no debe parecer una tanda aceptada. Se conserva el job
        # fallido para soporte, pero el usuario recibe un 503 accionable.
        job.state = "FAILED"
        job.detail = "No se pudo poner la tanda en cola."
        job.error = str(exc)[:2000]
        campaign_store.save_production_job(job)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No se pudo iniciar el worker de producción. Inténtalo de nuevo en un momento.",
        ) from exc

    refreshed = campaign_store.load_production_job(client_id, campaign_id, job.task_id)
    if refreshed.state == "COMPLETED" and refreshed.batch_id:
        try:
            batch = campaign_store.load_batch(client_id, campaign_id, refreshed.batch_id)
        except campaign_store.CampaignNotFoundError as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "La tarea terminó pero no se encontró su tanda de entregables.",
            ) from exc
        response.status_code = status.HTTP_201_CREATED
        return batch
    if refreshed.state == "FAILED":
        # Un backend eager sin propagación sigue devolviendo el detalle útil.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            refreshed.error or "La producción no pudo completarse.",
        )
    return _production_task_status(client_id, campaign_id, refreshed)


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production",
    response_model=ProductionBatchList,
)
def list_production_batches(client_id: str, campaign_id: str) -> ProductionBatchList:
    _campaign_or_404(client_id, campaign_id)
    return ProductionBatchList(batches=campaign_store.list_batches(client_id, campaign_id))


def _production_task_status(
    client_id: str, campaign_id: str, job: ProductionJob
) -> ProductionTaskStatus:
    """Convierte el manifiesto privado en el contrato público de polling."""

    result: ProductionBatch | None = None
    if job.state == "COMPLETED" and job.batch_id:
        try:
            result = campaign_store.load_batch(client_id, campaign_id, job.batch_id)
        except campaign_store.CampaignNotFoundError:
            # Un estado de tarea concluida sin archivo final no es un éxito
            # utilizable; queda explícito en vez de devolver un resultado vacío.
            job.state = "FAILED"
            job.detail = "La tarea terminó sin encontrar sus entregables."
            job.error = "No se encontró la tanda final en el almacenamiento."
            campaign_store.save_production_job(job)
    return ProductionTaskStatus(
        task_id=job.task_id,
        state=job.state,
        result=result,
        error=job.error if job.state == "FAILED" else None,
        meta={
            "progress": job.progress,
            "status": job.detail,
            "planned_pieces": job.planned_pieces,
            "batch_id": job.batch_id,
        },
    )


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production/tasks",
    response_model=ProductionTaskList,
)
def list_pending_production_tasks(
    client_id: str, campaign_id: str
) -> ProductionTaskList:
    """Devuelve tandas que siguen vivas, incluso desde otra sesión."""

    _campaign_or_404(client_id, campaign_id)
    active = campaign_store.list_production_jobs(
        client_id,
        campaign_id,
        states={"PENDING", "STARTED", "PROGRESS"},
    )
    return ProductionTaskList(
        tasks=[_production_task_status(client_id, campaign_id, job) for job in active]
    )


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production/tasks/{task_id}",
    response_model=ProductionTaskStatus,
)
def get_production_task(
    client_id: str, campaign_id: str, task_id: str
) -> ProductionTaskStatus:
    """Estado durable de una tanda asíncrona.

    Se lee el manifiesto propio, no solo ``AsyncResult``: Redis puede expirar
    resultados, pero el equipo debe poder volver a esta pantalla horas después
    y saber si la tanda terminó o cuál fue el error.
    """

    _campaign_or_404(client_id, campaign_id)
    try:
        job = campaign_store.load_production_job(client_id, campaign_id, task_id)
    except campaign_store.CampaignNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa orden de producción.") from None
    return _production_task_status(client_id, campaign_id, job)


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production/{batch_id}",
    response_model=ProductionBatch,
)
def get_production_batch(
    client_id: str, campaign_id: str, batch_id: str
) -> ProductionBatch:
    _campaign_or_404(client_id, campaign_id)
    try:
        return campaign_store.load_batch(client_id, campaign_id, batch_id)
    except campaign_store.CampaignNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa tanda.") from None


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production/{batch_id}/files/{relative_path:path}",
    response_class=FileResponse,
)
def production_file(
    client_id: str,
    campaign_id: str,
    batch_id: str,
    relative_path: str,
) -> FileResponse:
    _campaign_or_404(client_id, campaign_id)
    try:
        campaign_store.load_batch(client_id, campaign_id, batch_id)
        target = campaign_store.batch_path(client_id, campaign_id, batch_id, relative_path)
    except campaign_store.CampaignNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa tanda.") from None
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese entregable.")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)
