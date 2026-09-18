"""Lectura segura y mínima de referencias públicas de una campaña."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from ..config import settings
from ..services.public_references import PublicReferenceError, inspect_public_url
from .deps import bind_session

router = APIRouter(
    prefix="/references", tags=["referencias"], dependencies=[Depends(bind_session)]
)


class InspectRequest(BaseModel):
    url: HttpUrl


class ProfileAnalysisRequest(BaseModel):
    profile_url: HttpUrl
    images: list[HttpUrl] = Field(default_factory=list)


@router.post("/inspect")
def inspect_reference(request: InspectRequest) -> dict[str, object]:
    try:
        return inspect_public_url(str(request.url), timeout=8)
    except PublicReferenceError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/profile-analysis")
def analyze_profile(request: ProfileAnalysisRequest) -> dict[str, str | list[str]]:
    """Pide a OpenAI una guía práctica a partir de los posts públicos hallados."""
    if not settings.openai_api_key:
        raise HTTPException(409, "No hay OPENAI_API_KEY configurada para analizar el perfil.")
    images = [str(url) for url in request.images[:8]]
    if not images:
        raise HTTPException(422, "No encontramos imágenes públicas del perfil. Sube capturas de sus posts.")
    content: list[dict] = [{
        "type": "text",
        "text": (
            "Analiza estos posts públicos como director de arte. Devuelve una guía breve en español "
            "para crear plantillas coherentes: paleta, tipografías aparentes, composición, tratamiento "
            "de producto, estilo de copy, CTA y elementos que deben permanecer fijos. No inventes datos."
        ),
    }]
    content.extend({"type": "image_url", "image_url": {"url": image, "detail": "low"}} for image in images)
    try:
        with __import__("httpx").Client(timeout=25) as client:
            response = client.post(
                settings.openai_vision_endpoint,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": settings.openai_vision_model, "messages": [{"role": "user", "content": content}]},
            )
        if response.status_code >= 400:
            raise HTTPException(502, "OpenAI no pudo analizar las referencias.")
        data = response.json()
        guide = str(data["choices"][0]["message"]["content"] or "").strip()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "No se pudo completar el análisis visual.") from exc
    return {"profile_url": str(request.profile_url), "guide": guide[:4000], "images": images}
