"""La zona del producto la elige quien mira la pieza, y el motor la respeta.

El motor deduce dónde va el producto del hueco que ocupaba en el PSD, y casi
siempre acierta. Casi: cuando el KV traía tres prendas y entra una sola, o
cuando el arte se lleva a otra proporción, el hueco aprendido deja el producto
donde ya no debe estar. Estas pruebas fijan el contrato de la elección manual:
manda sobre lo aprendido, manda sobre el anclaje del modo fiel, y vale igual en
todos los formatos de la tanda porque se guarda en fracciones del lienzo.
"""
from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.models import (
    Canvas,
    Layer,
    LayerCategory,
    LayerType,
    ProductZone,
    Project,
    SourceImage,
)
from app.services import layout_engine
from app.services.layout_engine import FAITHFUL_LAYOUT, build_placements, plan_variants

from .conftest import create_manual_layers

SOURCE = (1000, 1000)
#: Abajo a la derecha: un cuarto del lienzo, lejos de donde el arte tenía el
#: producto (centrado), para que confundir una cosa con otra sea imposible.
ZONA = (0.60, 0.62, 0.34, 0.30)


def _layers() -> list[Layer]:
    return [
        Layer(
            id="p1",
            name="Producto",
            type=LayerType.IMAGE,
            category=LayerCategory.PRODUCT,
            src="layers/product.png",
            x=300,
            y=250,
            width=400,
            height=400,
            z_index=3,
            locked=True,
            # El hueco aprendido del PSD: centrado y grande. Es lo que la zona
            # elegida a mano tiene que desbancar.
            meta={"replacement_box": [300, 250, 400, 400]},
        ),
        Layer(
            id="h1",
            name="Titular",
            type=LayerType.TEXT,
            category=LayerCategory.HEADLINE,
            content="Conoce lo nuevo",
            x=80,
            y=80,
            width=600,
            height=120,
            z_index=6,
        ),
        Layer(
            id="g1",
            name="Legal",
            type=LayerType.TEXT,
            category=LayerCategory.LEGAL,
            content="Aplican condiciones.",
            x=60,
            y=940,
            width=880,
            height=40,
            z_index=8,
        ),
    ]


def _project(formats: list[str] | None = None) -> Project:
    project = Project(
        project_id="00000000-0000-4000-8000-0000000000aa",
        name="zona",
        canvas=Canvas(width=SOURCE[0], height=SOURCE[1]),
        source=SourceImage(
            path="original/a.png",
            width=SOURCE[0],
            height=SOURCE[1],
            format="PNG",
            original_filename="a.png",
            bytes=1,
        ),
        layers=_layers(),
    )
    return project


class _Request:
    """Lo mínimo que `plan_variants` le pregunta a una petición."""

    def __init__(self, formats: list[str], **extra):
        self.formats = formats
        self.count = len(formats)
        self.seed = 7
        self.intensity = "conservative"
        self.layouts = None
        self.anchored_layouts = True
        self.product_arrangement = "auto"
        self.instruction = None
        self.product_position_instruction = None
        self.hidden_layers: list[str] = []
        self.locked_layers: list[str] = []
        for key, value in extra.items():
            setattr(self, key, value)


def _product(placements):
    return next(
        item for item in placements if item.layer.category == LayerCategory.PRODUCT
    )


def test_la_zona_elegida_gana_al_hueco_aprendido_del_psd():
    placements, notes = build_placements(
        _layers(),
        FAITHFUL_LAYOUT,
        1000,
        1000,
        random.Random(1),
        intensity="conservative",
        source_canvas=SOURCE,
        product_zone=ZONA,
    )
    caja = _product(placements).box
    # Dentro del recuadro pedido, con el margen de un píxel de redondeo.
    assert caja[0] >= ZONA[0] * 1000 - 1
    assert caja[1] >= ZONA[1] * 1000 - 1
    assert caja[0] + caja[2] <= (ZONA[0] + ZONA[2]) * 1000 + 1
    assert caja[1] + caja[3] <= (ZONA[1] + ZONA[3]) * 1000 + 1
    assert any("zona elegida a mano" in nota for nota in notes)


def test_sin_zona_elegida_el_producto_sigue_donde_lo_puso_el_arte():
    """La elección manual es opcional: sin ella no cambia nada de lo de antes."""
    placements, _ = build_placements(
        _layers(),
        FAITHFUL_LAYOUT,
        1000,
        1000,
        random.Random(1),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    x, y, _, _ = _product(placements).box
    assert abs(x - 300) <= 2 and abs(y - 250) <= 2


def test_la_indicacion_escrita_no_pisa_el_recuadro_dibujado():
    """Dibujar un recuadro es más explícito que escribir «producto a la izquierda»."""
    bias = layout_engine.parse_instruction("producto izquierda")
    placements, _ = build_placements(
        _layers(),
        FAITHFUL_LAYOUT,
        1000,
        1000,
        random.Random(1),
        intensity="conservative",
        bias=bias,
        source_canvas=SOURCE,
        product_zone=ZONA,
    )
    assert _product(placements).box[0] >= ZONA[0] * 1000 - 1


def test_la_misma_zona_vale_para_todos_los_formatos_de_la_tanda():
    """Se guarda en fracciones justo para esto: cada medida la aplica a su lienzo."""
    project = _project()
    project.product_zone = ProductZone(
        x=ZONA[0], y=ZONA[1], width=ZONA[2], height=ZONA[3]
    )
    # La tira entra a propósito: es el formato que obliga al motor a recomponer
    # con la retícula del arte, donde el producto llevaba su propia banda
    # deducida y el recuadro dibujado se quedaba sin efecto.
    formats = ["1080x1080", "1080x1350", "1920x1080", "1200x400"]
    plans, _ = plan_variants(project, _Request(formats))

    assert [plan.format for plan in plans] == formats
    assert any(plan.layout == "source_flow" for plan in plans)
    for plan in plans:
        caja = _product(plan.placements).box
        assert caja[0] >= ZONA[0] * plan.width - 1
        assert caja[1] >= ZONA[1] * plan.height - 1
        assert caja[0] + caja[2] <= (ZONA[0] + ZONA[2]) * plan.width + 1
        assert caja[1] + caja[3] <= (ZONA[1] + ZONA[3]) * plan.height + 1


def test_la_api_guarda_la_zona_la_devuelve_y_la_borra(
    client: TestClient, project: dict
):
    create_manual_layers(client, project["project_id"])
    ruta = f"/projects/{project['project_id']}/product-zone"

    guardada = client.put(ruta, json={"zone": {"x": 0.6, "y": 0.62, "width": 0.34, "height": 0.3}})
    assert guardada.status_code == 200, guardada.text
    assert guardada.json()["zone"]["x"] == 0.6

    stored = client.get(f"/projects/{project['project_id']}").json()
    assert stored["product_zone"]["width"] == 0.34

    borrada = client.put(ruta, json={"zone": None})
    assert borrada.status_code == 200
    assert borrada.json()["zone"] is None
    assert client.get(f"/projects/{project['project_id']}").json()["product_zone"] is None


def test_la_api_rechaza_un_recuadro_que_es_un_resbalon_del_raton(
    client: TestClient, project: dict
):
    respuesta = client.put(
        f"/projects/{project['project_id']}/product-zone",
        json={"zone": {"x": 0.5, "y": 0.5, "width": 0.01, "height": 0.01}},
    )
    assert respuesta.status_code == 422


def test_la_api_avisa_de_los_elementos_que_el_producto_va_a_apartar(
    client: TestClient, project: dict
):
    """Elegir una zona encima del titular se puede; no saberlo, no."""
    capas = create_manual_layers(client, project["project_id"])
    titular = capas["headline"]
    canvas = client.get(f"/projects/{project['project_id']}").json()["canvas"]
    respuesta = client.put(
        f"/projects/{project['project_id']}/product-zone",
        json={
            "zone": {
                "x": titular["x"] / canvas["width"],
                "y": titular["y"] / canvas["height"],
                "width": titular["width"] / canvas["width"],
                "height": titular["height"] / canvas["height"],
            }
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert any("Titular" in aviso for aviso in respuesta.json()["warnings"])
