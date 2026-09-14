"""Una capa girada: ni se sale de su hueco ni es una capa deformada.

El modelo admite `rotation` en cualquier capa y la API la deja escribir. El
renderer giraba **después** de medir, con `expand=True`, así que la caja crecía
por su cuenta —un rectángulo 2:1 a 15° ocupa una caja 1.48:1— y pasaban dos
cosas a la vez: el elemento se salía por los cuatro lados del hueco que se le
había asignado, y el control de calidad, que compara la caja dibujada contra la
proporción de la capa, lo marcaba como «cambió su relación de aspecto». Eso es
un defecto que invalida la pieza, no depende de la semilla, y por tanto los tres
replanteos del motor salían marcados igual.
"""
from __future__ import annotations

import io

from PIL import Image

from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.services import renderer, storage
from app.services.imaging import rotated_bounds
from app.services.layout_engine import Placement, VariantPlan
from app.services.quality import evaluate_variant

CANVAS = (1080, 1080)
GIRO = 15.0


def _project(project_id: str) -> Project:
    storage.ensure_project_dirs(project_id)
    return Project(
        project_id=project_id,
        name="Giro",
        canvas=Canvas(width=CANVAS[0], height=CANVAS[1]),
        source=SourceImage(
            path="original/a.png",
            width=CANVAS[0],
            height=CANVAS[1],
            format="PNG",
            original_filename="a.png",
            bytes=10,
        ),
    )


def _franja(project: Project) -> Layer:
    """Una franja decorativa 2:1 inclinada, con su PNG en disco."""
    buffer = io.BytesIO()
    Image.new("RGBA", (400, 200), (198, 40, 40, 255)).save(buffer, format="PNG")
    relative = "layers/franja.png"
    storage.write_bytes(project.project_id, relative, buffer.getvalue())
    return Layer(
        name="Franja inclinada",
        type=LayerType.IMAGE,
        category=LayerCategory.DECORATION,
        src=relative,
        width=400,
        height=200,
        rotation=GIRO,
    )


def _plan(placements: list[Placement]) -> VariantPlan:
    return VariantPlan(
        index=0,
        layout="product_left",
        layout_label="Producto izquierda",
        format="1080x1080",
        width=CANVAS[0],
        height=CANVAS[1],
        seed=1,
        intensity="moderate",
        background_style="plate",
        placements=placements,
    )


def test_la_caja_girada_coincide_con_la_que_devuelve_pillow():
    """La fórmula es la que usa el control: tiene que ser la misma caja."""
    ancho, alto = rotated_bounds(400, 200, GIRO)
    girada = Image.new("RGBA", (400, 200)).rotate(-GIRO, expand=True)
    assert abs(ancho / alto - girada.width / girada.height) < 0.01


def test_una_capa_girada_no_se_sale_de_su_hueco():
    """Girar después de medir la sacaba por los cuatro lados del hueco asignado."""
    project = _project("11111111-0000-4000-8000-000000000001")
    placement = Placement(
        layer=_franja(project), x=300, y=300, width=400, height=200, z_index=2
    )
    canvas = Image.new("RGB", CANVAS, (250, 250, 250))
    renderer.draw_image_layer(canvas, placement, project)

    assert placement.width <= 400 and placement.height <= 200
    assert placement.x >= 300 and placement.y >= 300
    assert placement.x + placement.width <= 700
    assert placement.y + placement.height <= 500
    # Y sigue siendo la franja girada, no una franja recta encogida.
    esperado = rotated_bounds(400, 200, GIRO)
    assert abs(
        placement.width / placement.height - esperado[0] / esperado[1]
    ) < 0.05


def test_una_capa_girada_no_cuenta_como_deformada():
    """El defecto bloqueante que el propio motor se inventaba."""
    project = _project("11111111-0000-4000-8000-000000000002")
    placement = Placement(
        layer=_franja(project), x=300, y=300, width=400, height=200, z_index=2
    )
    canvas = Image.new("RGB", CANVAS, (250, 250, 250))
    renderer.draw_image_layer(canvas, placement, project)

    report = evaluate_variant(project, _plan([placement]))
    assert report.metrics["distorted_layers"] == 0.0
    assert not [w for w in report.warnings if "relación de aspecto" in w]


def test_una_capa_de_verdad_deformada_sigue_saltando():
    """El control no se ha vuelto ciego: sin giro, estirar sigue siendo defecto."""
    project = _project("11111111-0000-4000-8000-000000000003")
    layer = _franja(project)
    layer.rotation = 0.0
    placement = Placement(layer=layer, x=100, y=100, width=400, height=400, z_index=2)
    report = evaluate_variant(project, _plan([placement]))
    assert report.metrics["distorted_layers"] == 1.0
