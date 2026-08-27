"""Sistema de puntuación: solapes, fuera de lienzo, márgenes y deformación."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.services.layout_engine import Placement, VariantPlan
from app.services.quality import evaluate_variant

from .conftest import create_manual_layers


def _project() -> Project:
    return Project(
        name="Test",
        canvas=Canvas(width=1080, height=1080),
        source=SourceImage(
            path="original/a.png",
            width=1080,
            height=1080,
            format="PNG",
            original_filename="a.png",
            bytes=10,
        ),
    )


def _plan(placements: list[Placement]) -> VariantPlan:
    return VariantPlan(
        index=0,
        layout="product_left",
        layout_label="Producto izquierda",
        format="1080x1080",
        width=1080,
        height=1080,
        seed=1,
        intensity="moderate",
        background_style="plate",
        placements=placements,
    )


def _image_layer(name: str, category: LayerCategory, w: int, h: int) -> Layer:
    return Layer(
        name=name,
        type=LayerType.IMAGE,
        category=category,
        src=f"layers/{name}.png",
        width=w,
        height=h,
    )


def _text_layer(name: str, category: LayerCategory, content: str) -> Layer:
    return Layer(
        name=name, type=LayerType.TEXT, category=category, content=content,
        width=400, height=90,
    )


def test_clean_composition_scores_high():
    """Composición completa: producto + logo + titular + CTA."""
    placements = [
        Placement(
            layer=_image_layer("Logo", LayerCategory.LOGO, 200, 80),
            x=60, y=50, width=200, height=80, z_index=9,
        ),
        Placement(
            layer=_image_layer("Producto", LayerCategory.PRODUCT, 400, 400),
            x=90, y=280, width=440, height=440, z_index=3,
        ),
        Placement(
            layer=_text_layer("Titular", LayerCategory.HEADLINE, "Conoce lo nuevo"),
            x=580, y=200, width=420, height=180, z_index=6, font_size=72,
        ),
        Placement(
            layer=_text_layer("CTA", LayerCategory.CTA, "Comprar ahora"),
            x=580, y=760, width=340, height=90, z_index=8, font_size=40,
        ),
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert report.score >= 85, report
    assert not [w for w in report.warnings if "solap" in w.lower()]
    assert report.metrics["text_elements"] == 2.0


def test_composition_without_text_is_penalized():
    placements = [
        Placement(
            layer=_image_layer("Producto", LayerCategory.PRODUCT, 400, 400),
            x=300, y=300, width=480, height=480, z_index=3,
        ),
        Placement(
            layer=_image_layer("Logo", LayerCategory.LOGO, 200, 80),
            x=60, y=50, width=200, height=80, z_index=9,
        ),
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert report.score < 85
    assert any("no tiene texto" in warning for warning in report.warnings)


def test_out_of_canvas_is_detected():
    product = _image_layer("Producto", LayerCategory.PRODUCT, 400, 400)
    placements = [
        Placement(layer=product, x=900, y=900, width=400, height=400, z_index=3),
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert report.metrics["outside_canvas"] == 1.0
    assert any("sale del lienzo" in warning for warning in report.warnings)
    assert report.score < 90


def test_severe_overlap_is_detected():
    product = _image_layer("Producto", LayerCategory.PRODUCT, 400, 400)
    person = _image_layer("Persona", LayerCategory.PERSON, 400, 400)
    placements = [
        Placement(layer=product, x=200, y=200, width=400, height=400, z_index=3),
        Placement(layer=person, x=210, y=210, width=400, height=400, z_index=2),
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert report.metrics["severe_overlaps"] >= 1
    assert any("Solapamiento fuerte" in warning for warning in report.warnings)


def test_distorted_layer_is_penalized():
    product = _image_layer("Producto", LayerCategory.PRODUCT, 400, 200)
    placements = [
        Placement(layer=product, x=100, y=100, width=400, height=400, z_index=3),
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert report.metrics["distorted_layers"] == 1.0
    assert any("relación de aspecto" in warning for warning in report.warnings)


def test_small_logo_and_tiny_text_are_flagged():
    logo = _image_layer("Logo", LayerCategory.LOGO, 40, 20)
    headline = Layer(
        name="Titular",
        type=LayerType.TEXT,
        category=LayerCategory.HEADLINE,
        content="Hola",
        width=300,
        height=40,
    )
    placements = [
        Placement(layer=logo, x=60, y=50, width=40, height=20, z_index=9),
        Placement(
            layer=headline, x=100, y=400, width=300, height=40, z_index=6, font_size=14
        ),
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert any("logo" in warning.lower() for warning in report.warnings)
    assert report.metrics["small_text"] == 1.0


def test_score_is_bounded():
    product = _image_layer("Producto", LayerCategory.PRODUCT, 100, 50)
    placements = [
        Placement(layer=product, x=-500, y=-500, width=2000, height=2000, z_index=1)
        for _ in range(6)
    ]
    report = evaluate_variant(_project(), _plan(placements))
    assert 0 <= report.score <= 100
    assert len(report.warnings) <= 10


def test_faithful_reproduction_is_not_penalised(client: TestClient, project: dict):
    """Un diseño reproducido tal cual no se juzga por márgenes ni por aire.

    Sus elementos van a sangre y se superponen a propósito: aplicar las heurísticas
    de composición castigaría precisamente la fidelidad al arte aprobado.
    """
    create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/generate",
        json={
            "count": 4,
            "formats": ["1080x1080"],
            "layouts": ["faithful"],
            "seed": 11,
        },
    )
    assert response.status_code == 200, response.text
    variant = response.json()["variants"][0]
    metrics = variant["quality"]["metrics"]

    assert metrics["tight_margins"] == 0.0
    assert not any("margen" in w for w in variant["quality"]["warnings"])
    assert not any("espacio vacío" in w for w in variant["quality"]["warnings"])
    assert not any("saturada" in w for w in variant["quality"]["warnings"])
