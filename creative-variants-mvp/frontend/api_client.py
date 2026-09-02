"""Cliente HTTP del backend. El frontend NO contiene lógica de procesamiento."""
from __future__ import annotations

import os
from typing import Any

import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))


class ApiError(RuntimeError):
    """Error devuelto por la API con un mensaje legible."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _url(path: str) -> str:
    return f"{BACKEND_URL}{path}"


def _handle(response: requests.Response) -> Any:
    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        if isinstance(detail, dict):
            message = detail.get("message") or str(detail)
            warnings = detail.get("warnings") or []
            if warnings:
                message = f"{message} " + " | ".join(str(w) for w in warnings)
        elif isinstance(detail, list):
            message = "; ".join(
                f"{item.get('loc', ['?'])[-1]}: {item.get('msg', '')}"
                if isinstance(item, dict)
                else str(item)
                for item in detail
            )
        else:
            message = str(detail)
        raise ApiError(message, response.status_code, detail)
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.content


# ------------------------------------------------------------------- sistema
def health() -> dict:
    return _handle(requests.get(_url("/health"), timeout=15))


def capabilities() -> dict:
    return _handle(requests.get(_url("/capabilities"), timeout=15))


def image_models() -> list[dict]:
    """Catálogo de modelos de IA de imagen que ofrece el motor (Magnific)."""
    try:
        return capabilities().get("image_models") or []
    except Exception:  # noqa: BLE001 - el catálogo no debe romper la interfaz
        return []


# ------------------------------------------------------------------ proyectos
def create_project(
    name: str,
    artwork: tuple[str, bytes, str],
    kv: tuple[str, bytes, str] | None = None,
    logo: tuple[str, bytes, str] | None = None,
    font: tuple[str, bytes, str] | None = None,
    import_layers: bool = True,
) -> dict:
    files: dict[str, tuple[str, bytes, str]] = {"artwork": artwork}
    if kv:
        files["kv"] = kv
    if logo:
        files["logo"] = logo
    if font:
        files["font"] = font
    return _handle(
        requests.post(
            _url("/projects"),
            data={"name": name, "import_layers": str(import_layers).lower()},
            files=files,
            timeout=TIMEOUT,
        )
    )


def list_ingest(with_pieces: bool = False) -> dict:
    return _handle(
        requests.get(_url("/ingest"), params={"with_pieces": with_pieces}, timeout=120)
    )


def ingest_pieces(source: str, analyse_pixels: bool = True) -> dict:
    """Piezas que contiene un PSD del pliego (artboards o detección geométrica)."""
    return _handle(
        requests.get(
            _url("/ingest/pieces"),
            params={"source": source, "analyse_pixels": analyse_pixels},
            timeout=TIMEOUT,
        )
    )


def ingest_piece_preview(source: str, index: int, max_side: int = 320) -> bytes:
    return _handle(
        requests.get(
            _url("/ingest/pieces/preview"),
            params={"source": source, "index": index, "max_side": max_side},
            timeout=TIMEOUT,
        )
    )


def create_projects_from_ingest_split(
    source: str,
    name: str | None = None,
    kv: str | None = None,
    import_layers: bool = True,
    pieces: list[int] | None = None,
) -> dict:
    """Un proyecto por cada pieza del PSD. Devuelve {projects, pieces_detected, …}."""
    return _handle(
        requests.post(
            _url("/projects/from-ingest/split"),
            json={
                "source": source,
                "name": name,
                "kv": kv,
                "import_layers": import_layers,
                "pieces": pieces,
            },
            timeout=TIMEOUT,
        )
    )


def create_projects_split(
    name: str,
    artwork: tuple[str, bytes, str],
    pieces: list[int] | None = None,
    logo: tuple[str, bytes, str] | None = None,
    font: tuple[str, bytes, str] | None = None,
    import_layers: bool = True,
) -> dict:
    """Sube un PSD y crea un proyecto por cada pieza que contenga."""
    files: dict[str, tuple[str, bytes, str]] = {"artwork": artwork}
    if logo:
        files["logo"] = logo
    if font:
        files["font"] = font
    return _handle(
        requests.post(
            _url("/projects/split"),
            data={
                "name": name,
                "pieces": ",".join(str(index) for index in pieces or []),
                "import_layers": str(import_layers).lower(),
            },
            files=files,
            timeout=TIMEOUT,
        )
    )


def create_project_from_ingest(
    source: str, name: str | None = None, kv: str | None = None, import_layers: bool = True
) -> dict:
    return _handle(
        requests.post(
            _url("/projects/from-ingest"),
            json={
                "source": source,
                "name": name,
                "kv": kv,
                "import_layers": import_layers,
            },
            timeout=TIMEOUT,
        )
    )


def list_projects() -> list[dict]:
    return _handle(requests.get(_url("/projects"), timeout=30))


def get_project(project_id: str) -> dict:
    return _handle(requests.get(_url(f"/projects/{project_id}"), timeout=30))


def delete_project(project_id: str) -> dict:
    return _handle(requests.delete(_url(f"/projects/{project_id}"), timeout=30))


# -------------------------------------------------------------------- análisis
def analyze(project_id: str, run_ocr: bool = True, max_regions: int = 12) -> dict:
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/analyze"),
            json={"run_ocr": run_ocr, "run_segmentation": True, "max_regions": max_regions},
            timeout=TIMEOUT,
        )
    )


# ----------------------------------------------------------------------- capas
def update_layers(
    project_id: str,
    updates: list[dict] | None = None,
    delete: list[str] | None = None,
    order: list[str] | None = None,
) -> dict:
    payload: dict[str, Any] = {"updates": updates or [], "delete": delete or []}
    if order:
        payload["order"] = order
    return _handle(
        requests.put(_url(f"/projects/{project_id}/layers"), json=payload, timeout=TIMEOUT)
    )


def create_layer(project_id: str, payload: dict) -> dict:
    return _handle(
        requests.post(_url(f"/projects/{project_id}/layers"), json=payload, timeout=TIMEOUT)
    )


def edit_mask(project_id: str, payload: dict) -> dict:
    return _handle(
        requests.post(_url(f"/projects/{project_id}/layers/mask"), json=payload, timeout=TIMEOUT)
    )


def extract(
    project_id: str,
    layer_ids: list[str] | None = None,
    feather: int = 2,
    force: bool = False,
) -> dict:
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/extract"),
            json={"layer_ids": layer_ids, "feather": feather, "force": force},
            timeout=TIMEOUT,
        )
    )


# ----------------------------------------------------------------------- fondo
def reconstruct_background(
    project_id: str,
    prompt: str | None = None,
    dilate: int = 6,
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/reconstruct-background"),
            json={
                "prompt": prompt,
                "dilate": dilate,
                "provider": provider,
                "model": model,
            },
            timeout=TIMEOUT,
        )
    )


# ------------------------------------------------------------------- variantes
def generate(project_id: str, config: dict) -> dict:
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/generate"), json=config, timeout=TIMEOUT
        )
    )


def auto_generate(
    project_id: str,
    count: int = 9,
    formats: list[str] | None = None,
    intensity: str = "moderate",
    instruction: str | None = None,
    seed: int = 42,
    replace_existing: bool = True,
    product_label: str | None = None,
    product_arrangement: str = "auto",
    template_mode: bool = False,
    background_provider: str | None = None,
    background_model: str | None = None,
    background_prompt: str | None = None,
    regenerate_background: bool = False,
) -> dict:
    """Modo automático: detectar, recortar, fondo y componer en una sola llamada."""
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/auto"),
            json={
                "count": count,
                "formats": formats,
                "intensity": intensity,
                "instruction": instruction,
                "seed": seed,
                "replace_existing": replace_existing,
                "product_label": product_label,
                "product_arrangement": product_arrangement,
                "template_mode": template_mode,
                "background_provider": background_provider,
                "background_model": background_model,
                "background_prompt": background_prompt,
                "regenerate_background": regenerate_background,
            },
            timeout=TIMEOUT,
        )
    )


def detect_product(
    project_id: str,
    provider: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    force: bool = False,
) -> dict:
    """Recorta el producto de la foto cuando el PSD no lo trae como capa."""
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/layers/detect-product"),
            json={
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "force": force,
            },
            timeout=TIMEOUT,
        )
    )


def preview_template(project_id: str) -> bytes:
    """El KV sin el producto: la plantilla que quedará."""
    response = requests.get(_url(f"/projects/{project_id}/preview/template"), timeout=120)
    response.raise_for_status()
    return response.content


def replaceable_layers(project_id: str) -> dict:
    return _handle(
        requests.get(_url(f"/projects/{project_id}/layers/replaceable"), timeout=60)
    )


def replace_product(
    project_id: str,
    image: tuple[str, bytes, str],
    layer_id: str | None = None,
    hide_others: bool = False,
    append: bool = False,
    group_id: str | None = None,
    group_name: str | None = None,
    arrangement: str = "auto",
) -> dict:
    """Cambia el producto de una capa del KV por el recorte subido."""
    data: dict[str, str] = {
        "hide_others": str(hide_others).lower(),
        "append": str(append).lower(),
    }
    if layer_id:
        data["layer_id"] = layer_id
    if group_id:
        data["group_id"] = group_id
    if group_name:
        data["group_name"] = group_name
    data["arrangement"] = arrangement
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/layers/replace"),
            data=data,
            files={"image": image},
            timeout=TIMEOUT,
        )
    )


def list_variants(project_id: str) -> dict:
    return _handle(requests.get(_url(f"/projects/{project_id}/variants"), timeout=60))


def variant_image(project_id: str, variant_id: str) -> bytes:
    return _handle(
        requests.get(
            _url(f"/projects/{project_id}/variants/{variant_id}"),
            params={"download": "true"},
            timeout=TIMEOUT,
        )
    )


def export_zip(
    project_id: str, variant_ids: list[str] | None = None, include_layers: bool = False
) -> bytes:
    params: list[tuple[str, str]] = [("include_layers", str(include_layers).lower())]
    for variant_id in variant_ids or []:
        params.append(("variant_ids", variant_id))
    return _handle(
        requests.get(_url(f"/projects/{project_id}/export"), params=params, timeout=TIMEOUT)
    )


# -------------------------------------------------------------------- archivos
def project_file(project_id: str, relative_path: str) -> bytes:
    return _handle(
        requests.get(_url(f"/projects/{project_id}/files/{relative_path}"), timeout=TIMEOUT)
    )


def preview_detections(project_id: str) -> bytes:
    return _handle(
        requests.get(_url(f"/projects/{project_id}/preview/detections"), timeout=TIMEOUT)
    )


def preview_mask(project_id: str, layer_id: str) -> bytes:
    return _handle(
        requests.get(_url(f"/projects/{project_id}/preview/mask/{layer_id}"), timeout=TIMEOUT)
    )

def get_task_status(project_id: str, task_id: str) -> dict:
    return _handle(requests.get(_url(f"/projects/{project_id}/tasks/{task_id}"), timeout=TIMEOUT))

def upload_mask(project_id: str, layer_id: str, mask_bytes: bytes) -> dict:
    return _handle(
        requests.post(
            _url(f"/projects/{project_id}/layers/{layer_id}/mask/upload"),
            files={"mask_file": ("mask.png", mask_bytes, "image/png")},
            timeout=TIMEOUT
        )
    )
