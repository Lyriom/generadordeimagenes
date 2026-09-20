"""Flujo cliente -> conocimiento de campana -> brief -> plantillas propuestas."""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
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
    CampaignUpdateRequest,
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
from ..models.campaign_production import ProductionBatch, ProductionBatchList
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
from ..services.security import FileValidationError, slugify
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
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(payload)) as probe:
            width, height = probe.size
            if width * height > settings.campaign_max_product_pixels:
                raise FileValidationError(f"'{filename}' declara demasiados pixeles.")
    except FileValidationError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise FileValidationError(f"'{filename}' no se puede decodificar.") from exc
    media_type = (upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream")[:160]
    return filename, suffix, media_type, payload, width, height


async def _temporary_products(uploads: list[UploadFile], root: Path) -> dict[str, Path]:
    if len(uploads) > 200:
        raise FileValidationError("Sube como maximo 200 imagenes de producto por tanda.")
    stored: dict[str, Path] = {}
    seen_names: set[str] = set()
    total_bytes = 0
    for index, upload in enumerate(uploads):
        filename, suffix, _media_type, payload, _width, _height = await _read_product_upload(
            upload, index
        )
        filename_key = filename.casefold()
        if filename_key in seen_names:
            raise FileValidationError(
                f"La imagen de producto '{filename}' aparece mas de una vez en la tanda."
            )
        seen_names.add(filename_key)
        total_bytes += len(payload)
        if total_bytes > settings.campaign_max_product_batch_bytes:
            raise FileValidationError(
                "Las imagenes de producto superan el limite conjunto de "
                f"{settings.campaign_max_product_batch_mb} MB por tanda."
            )
        target = root / f"{index:03d}{suffix}"
        target.write_bytes(payload)
        stored[filename] = target
    return stored


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
) -> dict[str, object]:
    """Parsea la matriz con el mismo contrato que usara produccion.

    La UI no mantiene un segundo parser: CSV, TSV y XLSX se validan aqui para
    que la revision que ve el usuario sea exactamente la tanda que se generara.
    """
    _campaign_or_404(client_id, campaign_id)
    matrix_name, payload = await _matrix_payload(matrix)
    try:
        rows = production_matrix.parse_matrix(payload, matrix_name)
    except production_matrix.MatrixParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {
        "rows": [row.model_dump(mode="json") for row in rows],
        "total_rows": len(rows),
        "requested_pieces": production_matrix.requested_piece_count(rows),
    }


@router.post(
    "/{client_id}/campaigns/{campaign_id}/production",
    response_model=ProductionBatch,
    status_code=status.HTTP_201_CREATED,
)
async def produce_campaign(
    client_id: str,
    campaign_id: str,
    matrix: UploadFile = File(...),
    product_files: list[UploadFile] = File(default=[]),
    product_asset_ids: str = Form("[]"),
    default_formats: str = Form("[]"),
    use_ai_copy: bool = Form(True),
) -> ProductionBatch:
    brand = _brand_or_404(client_id)
    campaign = _campaign_or_404(client_id, campaign_id)
    if not campaign.brief_reviewed_at:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Revisa y guarda el brief antes de producir.",
        )
    matrix_name, payload = await _matrix_payload(matrix)
    try:
        rows = production_matrix.parse_matrix(payload, matrix_name)
    except production_matrix.MatrixParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    try:
        decoded_formats = json.loads(default_formats or "[]")
        if not isinstance(decoded_formats, list):
            raise ValueError
        formats = [str(item) for item in decoded_formats if str(item).strip()]
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "default_formats debe ser una lista JSON.",
        ) from exc
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
    with tempfile.TemporaryDirectory(prefix="creative-products-") as temporary:
        staged = _stored_product_assets(campaign, decoded_asset_ids)
        uploaded = await _temporary_products(product_files, Path(temporary))
        collision = next(
            (
                name
                for name in uploaded
                if name.casefold() in {item.casefold() for item in staged}
            ),
            None,
        )
        if collision:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"La imagen '{collision}' esta repetida entre las ya cargadas y las nuevas.",
            )
        products = {**staged, **uploaded}
        try:
            return await run_in_threadpool(
                campaign_creative.produce_batch,
                campaign,
                brand.name,
                rows,
                products,
                matrix_filename=matrix_name,
                default_formats=formats,
                use_ai_copy=use_ai_copy,
            )
        except campaign_creative.CampaignProductionError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get(
    "/{client_id}/campaigns/{campaign_id}/production",
    response_model=ProductionBatchList,
)
def list_production_batches(client_id: str, campaign_id: str) -> ProductionBatchList:
    _campaign_or_404(client_id, campaign_id)
    return ProductionBatchList(batches=campaign_store.list_batches(client_id, campaign_id))


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
