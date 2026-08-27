"""Endpoints de proyectos: creación, análisis, capas, fondo, variantes y export."""
from __future__ import annotations

import io
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from ..config import settings
from ..models import (
    AnalysisInfo,
    AnalyzeRequest,
    AnalyzeResponse,
    AutoRequest,
    AutoResponse,
    Canvas,
    DeleteResponse,
    ExtractRequest,
    ExtractResponse,
    GenerateRequest,
    GenerateResponse,
    Layer,
    LayerCategory,
    LayerCreateRequest,
    LayersUpdateRequest,
    LayerType,
    MaskEditRequest,
    Project,
    ProjectReferences,
    IngestImportRequest,
    ProjectSummary,
    ReconstructBackgroundRequest,
    ReconstructBackgroundResponse,
    ReplaceableLayer,
    ReplaceableLayersResponse,
    ReplaceProductResponse,
    SourceImage,
    VariantListResponse,
    new_id,
    utcnow,
)
from ..models.project import CATEGORY_LABELS_ES
from ..services import (
    analysis,
    autopilot,
    psd_import,
    replacement,
    export as export_service,
    inpainting,
    layer_extraction,
    renderer,
    segmentation as seg_service,
    storage,
    variants as variants_service,
)
from ..services.analysis import Z_ORDER
from ..services.imaging import box_mask
from ..services.security import (
    FileValidationError,
    resolve_inside,
    safe_stored_name,
    validate_font_bytes,
    validate_image_path,
)
from .deps import as_http_error, bad_request, load_project_or_404

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["proyectos"])

CHUNK = 1024 * 1024


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Lee un archivo pequeño (tipografías) respetando el límite de tamaño."""
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FileValidationError(
                f"'{file.filename}' supera el límite de {max_bytes // (1024 * 1024)} MB."
            )
        buffer.write(chunk)
    await file.close()
    return buffer.getvalue()


async def _stream_upload(project_id: str, file: UploadFile, max_bytes: int) -> Path:
    """Escribe el archivo subido a `tmp/` por bloques (los PSD pesan ~100 MB)."""
    storage.ensure_project_dirs(project_id)
    target = storage.abs_path(project_id, f"tmp/{uuid.uuid4().hex}.upload")
    total = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FileValidationError(
                        f"'{file.filename}' supera el límite de "
                        f"{max_bytes // (1024 * 1024)} MB."
                    )
                handle.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return target


def _ingest_source(project_id: str, temp_path: Path, filename: str, folder: str, *, allow_psd: bool):
    """Valida el archivo temporal, lo coloca en su carpeta y devuelve (SourceImage, psd_rel).

    Si es un PSD, la fuente del proyecto pasa a ser su versión aplanada en PNG:
    todo el resto del pipeline trabaja sobre un raster.
    """
    image_format, width, height = validate_image_path(
        temp_path, filename, allow_psd=allow_psd
    )
    size = temp_path.stat().st_size
    stored_name = safe_stored_name(filename, image_format)
    rel = f"{folder}/{stored_name}"
    final = storage.abs_path(project_id, rel)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(final)

    if image_format != "PSD":
        return (
            SourceImage(
                path=rel,
                width=width,
                height=height,
                format=image_format,
                original_filename=(filename or "sin-nombre")[:180],
                bytes=size,
            ),
            None,
        )

    flat_rel = f"{folder}/{Path(stored_name).stem}_flat.png"
    flat_width, flat_height = psd_import.flatten_psd(
        final, storage.abs_path(project_id, flat_rel)
    )
    return (
        SourceImage(
            path=flat_rel,
            width=flat_width,
            height=flat_height,
            format="PNG",
            original_filename=(filename or "sin-nombre")[:180],
            bytes=storage.abs_path(project_id, flat_rel).stat().st_size,
        ),
        rel,
    )


async def _store_image(project_id: str, file: UploadFile, folder: str, *, allow_psd: bool = False):
    """Guarda una imagen subida. Devuelve (SourceImage, ruta_psd|None)."""
    temp_path = await _stream_upload(project_id, file, settings.max_upload_bytes)
    try:
        return _ingest_source(
            project_id, temp_path, file.filename or "", folder, allow_psd=allow_psd
        )
    finally:
        temp_path.unlink(missing_ok=True)


def _apply_psd_layers(project: Project, psd_path: Path) -> list[str]:
    """Importa las capas del PSD al proyecto y devuelve las advertencias."""
    layers, warnings = psd_import.import_psd_layers(project, psd_path)
    if layers:
        project.layers = [
            Layer(
                name=CATEGORY_LABELS_ES[LayerCategory.BACKGROUND],
                type=LayerType.IMAGE,
                category=LayerCategory.BACKGROUND,
                x=0,
                y=0,
                width=project.canvas.width,
                height=project.canvas.height,
                z_index=-1,
                movable=False,
                resizable=False,
                reorderable=False,
                confidence=1.0,
                source="upload",
            ),
            *layers,
        ]
        project.analysis = AnalysisInfo(
            ran_at=utcnow(),
            segmentation_provider="psd",
            ocr_provider=None,
            warnings=warnings,
            detections=sum(1 for layer in layers if layer.type == LayerType.IMAGE),
            text_regions=sum(1 for layer in layers if layer.type == LayerType.TEXT),
        )
    return warnings


# --------------------------------------------------------------------- creación
@router.post(
    "",
    response_model=Project,
    status_code=status.HTTP_201_CREATED,
    summary="Crear proyecto subiendo el arte original",
)
async def create_project(
    name: str = Form("Proyecto sin título"),
    artwork: UploadFile = File(..., description="Arte publicitario JPG o PNG (obligatorio)"),
    kv: UploadFile | None = File(None, description="KV de referencia (opcional)"),
    logo: UploadFile | None = File(None, description="Logo original (opcional)"),
    font: UploadFile | None = File(None, description="Tipografía .ttf/.otf (opcional)"),
    import_layers: bool = Form(
        True, description="Si el arte es un PSD, importar sus capas reales."
    ),
) -> Project:
    project_id = new_id()
    storage.ensure_project_dirs(project_id)
    try:
        source, psd_rel = await _store_image(
            project_id, artwork, "original", allow_psd=True
        )
        references = ProjectReferences()
        if kv is not None and kv.filename:
            references.kv, _ = await _store_image(
                project_id, kv, "references", allow_psd=True
            )
        if logo is not None and logo.filename:
            references.logo, _ = await _store_image(project_id, logo, "references")
        if font is not None and font.filename:
            payload = await _read_upload(font, 10 * 1024 * 1024)
            suffix = validate_font_bytes(payload, font.filename)
            rel = f"references/font{suffix}"
            storage.write_bytes(project_id, rel, payload)
            references.font = rel

        project = Project(
            project_id=project_id,
            name=(name or "Proyecto sin título").strip()[:120],
            canvas=Canvas(width=source.width, height=source.height),
            source=source,
            references=references,
        )
        if psd_rel:
            project.meta["psd_source"] = psd_rel
            available, reason = psd_import.psd_available()
            if import_layers and available:
                project.warnings.extend(
                    _apply_psd_layers(project, storage.abs_path(project_id, psd_rel))
                )
            elif import_layers:
                project.warnings.append(reason or "No se pudieron importar las capas del PSD.")
            else:
                project.warnings.append(
                    "PSD aplanado sin importar capas: marque los elementos en Ajustes finos."
                )
        else:
            project.warnings.append(
                "Un arte aplanado no contiene capas: la separación es aproximada y "
                "editable en Ajustes finos."
            )
        storage.save_project(project)
        return project
    except Exception as exc:  # noqa: BLE001
        storage.delete_project(project_id)  # limpieza segura si algo falla
        raise as_http_error(exc) from exc


def _resolve_ingest(relative: str) -> Path:
    """Ruta absoluta dentro de la carpeta de ingesta (bloquea path traversal)."""
    path = resolve_inside(settings.ingest_dir, relative)
    if not path.exists() or not path.is_file():
        raise bad_request(f"No existe el archivo en la carpeta de ingesta: {relative}")
    return path


def _source_from_ingest(project_id: str, source_path: Path, folder: str):
    """Crea la fuente del proyecto desde un archivo de la ingesta.

    Un PSD no se copia al proyecto (pesa demasiado): se aplana dentro del proyecto y
    las capas se importan leyendo el original en su sitio.
    """
    image_format, width, height = validate_image_path(
        source_path, source_path.name, allow_psd=True
    )
    if image_format == "PSD":
        flat_rel = f"{folder}/{uuid.uuid4().hex}_flat.png"
        flat_width, flat_height = psd_import.flatten_psd(
            source_path, storage.abs_path(project_id, flat_rel)
        )
        return (
            SourceImage(
                path=flat_rel,
                width=flat_width,
                height=flat_height,
                format="PNG",
                original_filename=source_path.name[:180],
                bytes=storage.abs_path(project_id, flat_rel).stat().st_size,
            ),
            source_path,
        )

    rel = f"{folder}/{safe_stored_name(source_path.name, image_format)}"
    target = storage.abs_path(project_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)
    return (
        SourceImage(
            path=rel,
            width=width,
            height=height,
            format=image_format,
            original_filename=source_path.name[:180],
            bytes=target.stat().st_size,
        ),
        None,
    )


@router.post(
    "/from-ingest",
    response_model=Project,
    status_code=status.HTTP_201_CREATED,
    summary="Crear proyecto desde la carpeta de ingesta (ideal para PSD grandes)",
)
def create_project_from_ingest(request: IngestImportRequest) -> Project:
    source_path = _resolve_ingest(request.source)
    project_id = new_id()
    storage.ensure_project_dirs(project_id)
    try:
        source, psd_path = _source_from_ingest(project_id, source_path, "original")
        references = ProjectReferences()
        if request.kv:
            kv_source, _ = _source_from_ingest(
                project_id, _resolve_ingest(request.kv), "references"
            )
            references.kv = kv_source

        project = Project(
            project_id=project_id,
            name=(request.name or source_path.stem).strip()[:120],
            canvas=Canvas(width=source.width, height=source.height),
            source=source,
            references=references,
        )
        project.meta["ingest_source"] = request.source
        if psd_path is not None:
            available, reason = psd_import.psd_available()
            if request.import_layers and available:
                project.warnings.extend(_apply_psd_layers(project, psd_path))
            elif request.import_layers:
                project.warnings.append(reason or "No se importaron las capas del PSD.")
        else:
            project.warnings.append(
                "Un arte aplanado no contiene capas: la separación es aproximada y "
                "editable en Ajustes finos."
            )
        storage.save_project(project)
        return project
    except Exception as exc:  # noqa: BLE001
        storage.delete_project(project_id)
        raise as_http_error(exc) from exc


@router.get("", response_model=list[ProjectSummary], summary="Listar proyectos")
def list_projects() -> list[ProjectSummary]:
    return [
        ProjectSummary(
            project_id=project.project_id,
            name=project.name,
            created_at=project.created_at,
            updated_at=project.updated_at,
            canvas=project.canvas,
            layers=len(project.layers),
            variants=len(project.variants),
        )
        for project in storage.list_projects()
    ]


@router.get("/{project_id}", response_model=Project, summary="Obtener un proyecto")
def get_project(project_id: str) -> Project:
    return load_project_or_404(project_id)


@router.delete("/{project_id}", response_model=DeleteResponse, summary="Eliminar un proyecto")
def delete_project(project_id: str) -> DeleteResponse:
    project = load_project_or_404(project_id)
    deleted = storage.delete_project(project.project_id)
    return DeleteResponse(project_id=project.project_id, deleted=deleted)


# --------------------------------------------------------------------- análisis
@router.post(
    "/{project_id}/analyze",
    response_model=AnalyzeResponse,
    summary="Detectar componentes (segmentación + OCR)",
)
def analyze(project_id: str, request: AnalyzeRequest | None = None) -> AnalyzeResponse:
    project = load_project_or_404(project_id)
    request = request or AnalyzeRequest()
    try:
        layers, warnings, seg_provider, ocr_provider = analysis.analyze_project(
            project,
            run_segmentation=request.run_segmentation,
            run_ocr=request.run_ocr,
            max_regions=request.max_regions,
            extract=request.extract,
        )
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return AnalyzeResponse(
        project_id=project.project_id,
        layers=layers,
        warnings=warnings,
        segmentation_provider=seg_provider,
        ocr_provider=ocr_provider,
    )


# ----------------------------------------------------------------------- capas
@router.put(
    "/{project_id}/layers",
    response_model=Project,
    summary="Actualizar, reordenar o eliminar capas",
)
def update_layers(project_id: str, request: LayersUpdateRequest) -> Project:
    project = load_project_or_404(project_id)
    try:
        for patch in request.updates:
            layer = project.layer_by_id(patch.id)
            if layer is None:
                raise bad_request(f"Capa inexistente: {patch.id}")
            data = patch.model_dump(exclude_unset=True, exclude_none=True, exclude={"id"})
            geometry_changed = any(key in data for key in ("x", "y", "width", "height"))
            category_changed = "category" in data and data["category"] != layer.category

            for field, value in data.items():
                setattr(layer, field, value)

            if category_changed:
                layer.z_index = Z_ORDER.get(layer.category, layer.z_index)
                if "name" not in data:
                    layer.name = CATEGORY_LABELS_ES.get(layer.category, layer.name)
                layer.source = "manual"
            if "category" in data:
                # La revisión humana manda sobre la clasificación automática.
                layer.meta["mandatory_art"] = (
                    layer.category in psd_import.MANDATORY_ART_CATEGORIES
                    and bool(layer.src)
                )
                layer.meta["role_confirmed"] = True
            if layer.type == LayerType.TEXT and not (layer.content or "").strip():
                layer.content = layer.name
            if geometry_changed and not layer.meta.get("mask_edited"):
                # La máscara sigue al rectángulo salvo que el usuario la haya pintado.
                mask = box_mask(
                    (project.canvas.height, project.canvas.width),
                    (layer.x, layer.y, layer.width, layer.height),
                )
                layer_extraction.write_mask(project, layer, mask)
                layer.extracted = False

        if request.delete:
            remove = set(request.delete)
            project.layers = [layer for layer in project.layers if layer.id not in remove]

        if request.order:
            order_map = {layer_id: index for index, layer_id in enumerate(request.order)}
            for layer in project.layers:
                if layer.id in order_map:
                    layer.z_index = order_map[layer.id]

        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return project


@router.post(
    "/{project_id}/layers",
    response_model=Layer,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una capa manualmente (rectángulo o texto)",
)
def create_layer(project_id: str, request: LayerCreateRequest) -> Layer:
    project = load_project_or_404(project_id)
    canvas_w, canvas_h = project.canvas.width, project.canvas.height
    if request.x >= canvas_w or request.y >= canvas_h:
        raise bad_request("El rectángulo está fuera del lienzo.")

    width = min(request.width, canvas_w - request.x)
    height = min(request.height, canvas_h - request.y)
    if width < 4 or height < 4:
        raise bad_request("El rectángulo es demasiado pequeño.")

    category = request.category
    layer = Layer(
        name=request.name or CATEGORY_LABELS_ES.get(category, "Capa"),
        type=request.type,
        category=category,
        x=request.x,
        y=request.y,
        width=width,
        height=height,
        z_index=Z_ORDER.get(category, 5),
        locked=request.locked,
        confidence=1.0,
        source="manual",
        content=request.content
        or (CATEGORY_LABELS_ES.get(category, "Texto") if request.type == LayerType.TEXT else None),
        font_size=request.font_size or max(12, int(height * 0.7)),
        color=request.color or "#FFFFFF",
    )

    try:
        image_path = str(storage.abs_path(project.project_id, project.source.path))
        mask = None
        if request.type == LayerType.IMAGE and request.auto_segment:
            try:
                mask, provider_name, seg_warnings = seg_service.segment_box(
                    image_path, box=(layer.x, layer.y, layer.width, layer.height)
                )
                layer.meta["segmented_with"] = provider_name
                layer.meta["mask_edited"] = True
                layer.warnings.extend(seg_warnings[:2])
            except Exception as exc:  # noqa: BLE001
                layer.warnings.append(
                    f"No se pudo segmentar automáticamente ({exc}); se usa el rectángulo."
                )
        if mask is None:
            mask = box_mask((canvas_h, canvas_w), (layer.x, layer.y, layer.width, layer.height))
        layer_extraction.write_mask(project, layer, mask)

        if request.type == LayerType.IMAGE:
            ok, warning = layer_extraction.extract_layer(project, layer, feather=2, force=True)
            if not ok and warning:
                layer.warnings.append(warning)

        project.layers.append(layer)
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return layer


@router.post(
    "/{project_id}/layers/mask",
    response_model=Layer,
    summary="Corregir la máscara de una capa (pincel add/subtract)",
)
def edit_mask(project_id: str, request: MaskEditRequest) -> Layer:
    project = load_project_or_404(project_id)
    layer = project.layer_by_id(request.layer_id)
    if layer is None:
        raise bad_request(f"Capa inexistente: {request.layer_id}")

    try:
        shape = (project.canvas.height, project.canvas.width)
        image_path = str(storage.abs_path(project.project_id, project.source.path))

        if request.auto_segment:
            mask, provider_name, warnings = seg_service.segment_box(
                image_path, box=(layer.x, layer.y, layer.width, layer.height)
            )
            layer.meta["segmented_with"] = provider_name
            layer.warnings = (layer.warnings + warnings[:2])[-5:]
        elif request.reset_from_box:
            mask = box_mask(shape, (layer.x, layer.y, layer.width, layer.height))
        else:
            mask = layer_extraction.ensure_mask(project, layer, persist=False)

        if request.operations:
            mask = seg_service.apply_mask_operations(mask, request.operations)

        mask = layer_extraction.refine_layer_mask(
            mask, refine=request.refine, feather=0
        )
        layer_extraction.write_mask(project, layer, mask)
        layer.meta["mask_edited"] = True
        layer.extracted = False

        if request.re_extract and layer.category != LayerCategory.BACKGROUND:
            ok, warning = layer_extraction.extract_layer(
                project, layer, feather=request.feather, force=True
            )
            if not ok and warning:
                layer.warnings.append(warning)
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return layer


@router.get(
    "/{project_id}/layers/replaceable",
    response_model=ReplaceableLayersResponse,
    summary="Capas que pueden recibir otro producto (producto primero, mayor primero)",
)
def replaceable_layers(project_id: str) -> ReplaceableLayersResponse:
    project = load_project_or_404(project_id)
    area = max(1, project.canvas.width * project.canvas.height)
    return ReplaceableLayersResponse(
        project_id=project.project_id,
        layers=[
            ReplaceableLayer(
                id=layer.id,
                name=layer.name,
                category=layer.category,
                width=layer.width,
                height=layer.height,
                area_ratio=round(layer.width * layer.height / area, 4),
                src=layer.src,
            )
            for layer in replacement.candidate_layers(project)
        ],
    )


@router.post(
    "/{project_id}/layers/replace",
    response_model=ReplaceProductResponse,
    summary="Cambiar el producto: sustituye el PNG de una capa por otro recorte",
)
async def replace_product(
    project_id: str,
    image: UploadFile = File(..., description="Recorte PNG (idealmente con transparencia)"),
    layer_id: str | None = Form(
        None, description="Capa a reemplazar. Si se omite, el producto más grande."
    ),
    hide_others: bool = Form(
        False, description="Ocultar los demás productos del KV original."
    ),
    append: bool = Form(
        False, description="Añadir como otra capa de producto en vez de sustituir."
    ),
) -> ReplaceProductResponse:
    project = load_project_or_404(project_id)
    temp_path = await _stream_upload(project.project_id, image, settings.max_upload_bytes)
    try:
        validate_image_path(temp_path, image.filename or "", allow_psd=False)
        layer = replacement.resolve_target(project, layer_id)
        if append:
            layer, warnings = replacement.append_product_image(project, layer, temp_path)
        else:
            warnings = replacement.replace_layer_image(
                project, layer, temp_path, hide_others=hide_others
            )
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    finally:
        temp_path.unlink(missing_ok=True)
    return ReplaceProductResponse(
        project_id=project.project_id, layer=layer, warnings=warnings
    )


@router.post(
    "/{project_id}/extract",
    response_model=ExtractResponse,
    summary="Extraer capas como PNG transparentes",
)
def extract(project_id: str, request: ExtractRequest | None = None) -> ExtractResponse:
    project = load_project_or_404(project_id)
    request = request or ExtractRequest()
    try:
        extracted, skipped, warnings = layer_extraction.extract_layers(
            project, request.layer_ids, feather=request.feather, force=request.force
        )
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return ExtractResponse(
        project_id=project.project_id,
        extracted=extracted,
        skipped=skipped,
        warnings=warnings,
    )


# ------------------------------------------------------------------------ fondo
@router.post(
    "/{project_id}/reconstruct-background",
    response_model=ReconstructBackgroundResponse,
    summary="Reconstruir el fondo detrás de las capas extraídas",
)
def reconstruct_background(
    project_id: str, request: ReconstructBackgroundRequest | None = None
) -> ReconstructBackgroundResponse:
    project = load_project_or_404(project_id)
    payload = request or ReconstructBackgroundRequest()
    try:
        rel, provider_name, warnings = inpainting.reconstruct_background(
            project,
            layer_ids=payload.layer_ids,
            prompt=payload.prompt,
            dilate=payload.dilate,
            preferred_provider=payload.provider,
        )
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return ReconstructBackgroundResponse(
        project_id=project.project_id,
        background=rel,
        provider=provider_name,
        warnings=warnings,
    )


# --------------------------------------------------------------------- variantes
@router.post(
    "/{project_id}/generate",
    response_model=GenerateResponse,
    summary="Generar variantes de composición",
)
def generate(project_id: str, request: GenerateRequest | None = None) -> GenerateResponse:
    project = load_project_or_404(project_id)
    request = request or GenerateRequest()
    try:
        generated, warnings = variants_service.generate_variants(project, request)
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    if not generated:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "No se pudo generar ninguna variante.",
                "warnings": warnings,
            },
        )
    return GenerateResponse(
        project_id=project.project_id, variants=generated, warnings=warnings
    )


@router.post(
    "/{project_id}/auto",
    response_model=AutoResponse,
    summary="Modo automático: detectar, recortar, rellenar el fondo y generar en un paso",
)
def auto(project_id: str, request: AutoRequest | None = None) -> AutoResponse:
    project = load_project_or_404(project_id)
    request = request or AutoRequest()
    try:
        steps, generated, warnings = autopilot.run(project, request)
        storage.save_project(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    # En una campaña por lotes, una tanda sin aprobados no debe cancelar los demás
    # productos/KV. El cliente recibe la lista vacía y continúa con la siguiente.
    return AutoResponse(
        project_id=project.project_id,
        steps=steps,
        variants=generated,
        warnings=warnings,
    )


@router.get(
    "/{project_id}/variants",
    response_model=VariantListResponse,
    summary="Listar variantes generadas",
)
def list_variants(project_id: str) -> VariantListResponse:
    project = load_project_or_404(project_id)
    return VariantListResponse(project_id=project.project_id, variants=project.variants)


@router.get(
    "/{project_id}/variants/{variant_id}",
    summary="Obtener una variante (metadatos o imagen con ?download=true)",
)
def get_variant(project_id: str, variant_id: str, download: bool = False):
    project = load_project_or_404(project_id)
    variant = project.variant_by_id(variant_id)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Variante no encontrada: {variant_id}"
        )
    if not download:
        return variant
    path = storage.abs_path(project.project_id, variant.image)
    if not path.exists():
        raise HTTPException(status_code=404, detail="La imagen de la variante no existe.")
    filename = f"{variant.index:02d}_{variant.layout}_{variant.format}.png"
    return FileResponse(path, media_type="image/png", filename=filename)


# ---------------------------------------------------------------------- export
@router.get("/{project_id}/export", summary="Descargar ZIP con las variantes")
def export_variants(
    project_id: str,
    variant_ids: list[str] | None = Query(None, description="IDs a incluir (todas si se omite)"),
    include_layers: bool = Query(False, description="Incluir los PNG de capas extraídas"),
):
    project = load_project_or_404(project_id)
    try:
        archive = export_service.build_zip(project, variant_ids, include_layers=include_layers)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
    )


# --------------------------------------------------------------- previsualización
@router.get("/{project_id}/preview/detections", summary="Original con bounding boxes")
def preview_detections(project_id: str):
    project = load_project_or_404(project_id)
    try:
        image = renderer.render_detection_preview(project)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return _png_response(image)


@router.get("/{project_id}/preview/mask/{layer_id}", summary="Original con la máscara resaltada")
def preview_mask(project_id: str, layer_id: str):
    project = load_project_or_404(project_id)
    try:
        image = renderer.render_mask_preview(project, layer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Capa no encontrada: {layer_id}") from exc
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    return _png_response(image)


@router.get("/{project_id}/files/{relative_path:path}", summary="Servir un archivo del proyecto")
def get_file(project_id: str, relative_path: str):
    project = load_project_or_404(project_id)
    try:
        path = storage.abs_path(project.project_id, relative_path)
    except Exception as exc:  # noqa: BLE001
        raise as_http_error(exc) from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Archivo no encontrado: {relative_path}")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


def _png_response(image) -> Response:
    buffer = io.BytesIO()
    preview = image.copy()
    preview.thumbnail((1400, 1400))
    preview.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")
