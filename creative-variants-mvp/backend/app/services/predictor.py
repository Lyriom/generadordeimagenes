"""Interfaz del futuro Predictor Creativo.

En este MVP no hay modelo predictivo: `HeuristicPredictor` devuelve el puntaje de
reglas de `quality.py`. Cuando exista un modelo real (CTR, atención, recall de
marca), basta implementar `CreativePredictor` y registrarlo en `get_predictor()`.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from PIL import Image

from ..models import Project, QualityReport
from .layout_engine import VariantPlan


@runtime_checkable
class CreativePredictor(Protocol):
    name: str

    def available(self) -> bool: ...

    def predict(
        self, project: Project, plan: VariantPlan, image: Image.Image, quality: QualityReport
    ) -> dict[str, Any]:
        """Devuelve métricas predichas (p. ej. {'ctr_index': 0.83})."""
        ...


class HeuristicPredictor:
    """Placeholder determinista basado en las reglas de composición."""

    name = "heuristic"

    def available(self) -> bool:
        return True

    def predict(
        self, project: Project, plan: VariantPlan, image: Image.Image, quality: QualityReport
    ) -> dict[str, Any]:
        return {
            "model": self.name,
            "composition_score": quality.score,
            "predicted_performance": None,  # se completará con el modelo real
            "notes": "Puntaje por reglas; sin predicción de desempeño en el MVP.",
        }


_predictor: CreativePredictor = HeuristicPredictor()


def get_predictor() -> CreativePredictor:
    return _predictor


def set_predictor(predictor: CreativePredictor) -> None:
    """Punto de extensión para inyectar el Predictor Creativo real."""
    global _predictor
    _predictor = predictor
