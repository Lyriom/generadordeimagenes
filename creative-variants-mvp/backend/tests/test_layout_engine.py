"""Motor de layouts: zonas relativas, no deformación, determinismo y solapes."""
from __future__ import annotations

import random

from app.models import Layer, LayerCategory, LayerType
from app.services.layout_engine import (
    LANDSCAPE_OVERRIDES,
    LAYOUTS,
    VERTICAL_OVERRIDES,
    build_placements,
    choose_layouts,
    overlap_ratio,
    parse_instruction,
    zones_for_format,
)

REQUIRED_SLOTS = {"logo", "product", "headline", "cta", "legal"}


def _layers() -> list[Layer]:
    return [
        Layer(
            id="p1",
            name="Producto",
            type=LayerType.IMAGE,
            category=LayerCategory.PRODUCT,
            src="layers/product.png",
            x=0,
            y=0,
            width=300,
            height=200,
            z_index=3,
            locked=True,
        ),
        Layer(
            id="l1",
            name="Logo",
            type=LayerType.IMAGE,
            category=LayerCategory.LOGO,
            src="layers/logo.png",
            x=0,
            y=0,
            width=120,
            height=40,
            z_index=9,
            locked=True,
        ),
        Layer(
            id="h1",
            name="Titular",
            type=LayerType.TEXT,
            category=LayerCategory.HEADLINE,
            content="Conoce lo nuevo",
            x=0,
            y=0,
            width=400,
            height=90,
            z_index=6,
        ),
        Layer(
            id="c1",
            name="CTA",
            type=LayerType.TEXT,
            category=LayerCategory.CTA,
            content="Comprar ahora",
            x=0,
            y=0,
            width=200,
            height=50,
            z_index=8,
        ),
        Layer(
            id="g1",
            name="Texto legal",
            type=LayerType.TEXT,
            category=LayerCategory.LEGAL,
            content="Aplican terminos y condiciones",
            x=0,
            y=0,
            width=500,
            height=20,
            z_index=4,
        ),
    ]


def test_there_are_at_least_eight_layout_families():
    assert len(LAYOUTS) >= 8


def _assert_zones_valid(name: str, zones: dict):
    for slot, zone in zones.items():
        x, y, w, h = zone
        assert 0 <= x <= 1 and 0 <= y <= 1, f"{name}.{slot} fuera de rango"
        assert 0 < w <= 1 and 0 < h <= 1, f"{name}.{slot} tamaño inválido"
        assert x + w <= 1.001, f"{name}.{slot} se sale a la derecha"
        assert y + h <= 1.001, f"{name}.{slot} se sale abajo"


def test_layout_zones_are_relative_and_inside_canvas():
    for key, layout in LAYOUTS.items():
        assert REQUIRED_SLOTS <= set(layout["zones"]), f"{key} le faltan zonas"
        _assert_zones_valid(key, layout["zones"])
    for key, zones in VERTICAL_OVERRIDES.items():
        assert key in LAYOUTS
        assert REQUIRED_SLOTS <= set(zones), f"{key} vertical le faltan zonas"
        _assert_zones_valid(f"{key}:vertical", zones)
    for key, zones in LANDSCAPE_OVERRIDES.items():
        assert key in LAYOUTS
        assert REQUIRED_SLOTS <= set(zones), f"{key} landscape le faltan zonas"
        _assert_zones_valid(f"{key}:landscape", zones)


def test_zones_adapt_to_the_canvas_format():
    """Un layout de columnas debe recomponerse en 9:16 y en 16:9."""
    square = zones_for_format("product_left", 1080, 1080)
    vertical = zones_for_format("product_left", 1080, 1920)
    assert square["product"] != vertical["product"]
    # En vertical el producto ocupa el ancho completo y el texto va debajo.
    assert vertical["product"][2] > 0.7
    assert vertical["headline"][1] > square["headline"][1]

    landscape = zones_for_format("vertical_stack", 1920, 1080)
    assert landscape["product"] != zones_for_format("vertical_stack", 1080, 1080)["product"]


def test_placements_preserve_aspect_ratio_and_stay_inside():
    layers = _layers()
    for layout_key in LAYOUTS:
        for canvas in ((1080, 1080), (1080, 1920), (1920, 1080)):
            placements, _ = build_placements(
                layers, layout_key, canvas[0], canvas[1], random.Random(7), "creative"
            )
            assert placements
            for placement in placements:
                x, y, w, h = placement.box
                assert x >= 0 and y >= 0
                assert x + w <= canvas[0] and y + h <= canvas[1]
                if placement.layer.type == LayerType.IMAGE:
                    original = placement.layer.width / placement.layer.height
                    rendered = w / h
                    assert abs(original - rendered) / original < 0.03, (
                        layout_key,
                        placement.layer.name,
                    )


def test_legal_layer_always_visible_at_bottom():
    layers = _layers()
    for layout_key in LAYOUTS:
        placements, _ = build_placements(
            layers, layout_key, 1080, 1350, random.Random(3), "creative"
        )
        legal = next(p for p in placements if p.layer.category == LayerCategory.LEGAL)
        assert legal.y + legal.height <= 1350
        assert legal.y > 1350 * 0.7


def test_same_seed_produces_identical_placements():
    layers = _layers()
    first, _ = build_placements(layers, "product_left", 1080, 1080, random.Random(99), "creative")
    second, _ = build_placements(layers, "product_left", 1080, 1080, random.Random(99), "creative")
    assert [p.box for p in first] == [p.box for p in second]


def test_different_seeds_change_the_composition():
    layers = _layers()
    first, _ = build_placements(layers, "product_left", 1080, 1080, random.Random(1), "creative")
    second, _ = build_placements(layers, "product_left", 1080, 1080, random.Random(2), "creative")
    assert [p.box for p in first] != [p.box for p in second]


def test_conservative_intensity_does_not_reorder():
    layers = _layers()
    placements, notes = build_placements(
        layers, "vertical_stack", 1080, 1080, random.Random(11), "conservative"
    )
    assert all("Orden visual" not in note for note in notes)
    z_by_id = {p.layer.id: p.z_index for p in placements}
    assert z_by_id["p1"] == 3 and z_by_id["l1"] == 9


def test_locked_layers_are_not_reordered():
    layers = _layers()
    placements, _ = build_placements(
        layers, "diagonal_flow", 1080, 1080, random.Random(5), "creative"
    )
    logo = next(p for p in placements if p.layer.id == "l1")
    product = next(p for p in placements if p.layer.id == "p1")
    assert logo.z_index == 9
    assert product.z_index == 3


def test_no_severe_overlaps_after_resolution():
    layers = _layers()
    for layout_key in LAYOUTS:
        placements, _ = build_placements(
            layers, layout_key, 1080, 1350, random.Random(13), "moderate"
        )
        for i, first in enumerate(placements):
            for second in placements[i + 1 :]:
                assert overlap_ratio(first.box, second.box) <= 0.5, (
                    layout_key,
                    first.layer.name,
                    second.layer.name,
                )


def test_replaced_products_stay_inside_the_zone_learned_from_psd():
    layers = _layers()
    product = next(layer for layer in layers if layer.category == LayerCategory.PRODUCT)
    product.meta["replacement_box"] = [420, 140, 450, 360]
    second = product.model_copy(deep=True)
    second.id = "p2"
    second.name = "Producto 2"
    second.meta["external"] = True
    layers.append(second)

    placements, notes = build_placements(
        layers,
        "product_left",  # el layout genérico no debe ganar a la zona del PSD.
        1080,
        1080,
        random.Random(2),
        "creative",
        source_canvas=(900, 660),
    )
    products = [p for p in placements if p.layer.category == LayerCategory.PRODUCT]
    learned = (504, 229, 540, 589)  # caja del PSD escalada al nuevo lienzo
    lx, ly, lw, lh = learned
    assert len(products) == 2
    for placement in products:
        assert placement.x >= lx
        assert placement.y >= ly
        assert placement.x + placement.width <= lx + lw
        assert placement.y + placement.height <= ly + lh
        assert placement.pinned is True
    assert any("zona aprendida" in note for note in notes)


def test_overlap_ratio_math():
    assert overlap_ratio((0, 0, 100, 100), (200, 200, 50, 50)) == 0.0
    assert overlap_ratio((0, 0, 100, 100), (0, 0, 100, 100)) == 1.0
    assert 0.24 < overlap_ratio((0, 0, 100, 100), (50, 50, 100, 100)) < 0.26


def test_choose_layouts_covers_every_family_before_repeating():
    from app.services.layout_engine import FAITHFUL_LAYOUT

    sorteables = set(LAYOUTS) - {FAITHFUL_LAYOUT}
    keys = choose_layouts(len(sorteables), random.Random(4))
    assert set(keys) == sorteables
    # La familia «fiel» no entra en el sorteo: la asigna plan_variants por formato.
    assert FAITHFUL_LAYOUT not in choose_layouts(30, random.Random(9))
    # Pero se respeta si el usuario la pide explícitamente.
    assert choose_layouts(3, random.Random(1), allowed=[FAITHFUL_LAYOUT]) == [
        FAITHFUL_LAYOUT
    ] * 3


def test_parse_instruction_biases():
    bias = parse_instruction("Quiero el producto grande y el titular arriba")
    assert bias["product_scale"] > 1.0
    assert "hero_product_overlay" in bias["preferred_layouts"]
    assert "product_center_headline_top" in bias["preferred_layouts"]
    assert parse_instruction(None)["product_scale"] == 1.0


# ------------------------------------------------- fidelidad al diseño original
def test_faithful_layout_reproduces_relative_positions():
    """La familia «fiel» coloca cada capa donde estaba, en proporción."""
    from app.services.layout_engine import FAITHFUL_LAYOUT, build_placements

    source = (1200, 400)
    layers = [
        Layer(
            name="Producto",
            type=LayerType.IMAGE,
            category=LayerCategory.PRODUCT,
            x=600,
            y=80,
            width=300,
            height=240,
            src="layers/p.png",
            z_index=5,
        ),
        Layer(
            name="Logo",
            type=LayerType.IMAGE,
            category=LayerCategory.LOGO,
            x=48,
            y=20,
            width=180,
            height=60,
            src="layers/l.png",
            z_index=9,
        ),
    ]
    # Mismo formato que el original: la reproducción debe ser prácticamente exacta.
    placements, _ = build_placements(
        layers,
        FAITHFUL_LAYOUT,
        1200,
        400,
        random.Random(7),
        intensity="moderate",
        source_canvas=source,
    )
    by_name = {item.layer.name: item for item in placements}
    assert abs(by_name["Producto"].x - 600) <= 6
    assert abs(by_name["Producto"].y - 80) <= 6
    assert abs(by_name["Logo"].x - 48) <= 6
    # Nada se mueve ni se reescala: son posiciones ancladas.
    assert all(item.pinned for item in placements)


def test_faithful_only_used_when_aspect_matches():
    """Un banner 1200x400 no se «conserva» dentro de un 1080x1920."""
    from app.models import Canvas, GenerateRequest, Project, SourceImage
    from app.services.layout_engine import FAITHFUL_LAYOUT, plan_variants

    project = Project(
        project_id="00000000-0000-4000-8000-000000000001",
        name="kv",
        canvas=Canvas(width=1200, height=400),
        source=SourceImage(
            path="original/kv.png",
            width=1200,
            height=400,
            format="PNG",
            original_filename="kv.png",
            bytes=1,
        ),
        layers=_layers(),
    )
    plans, _ = plan_variants(
        project,
        GenerateRequest(count=6, formats=["1200x400", "1080x1920"], seed=3),
    )
    fiel = {plan.format for plan in plans if plan.layout == FAITHFUL_LAYOUT}
    assert fiel == {"1200x400"}
    # Y solo una por formato: el resto son variaciones reales.
    assert sum(1 for plan in plans if plan.layout == FAITHFUL_LAYOUT) == 1
    # La variante fiel usa el fondo real, no uno generado.
    faithful_plan = next(plan for plan in plans if plan.layout == FAITHFUL_LAYOUT)
    assert faithful_plan.background_style == "plate"


def test_stretch_is_only_granted_to_scenery():
    """El permiso de estirar nunca alcanza al producto ni al logo."""
    from app.services.layout_engine import FAITHFUL_LAYOUT, build_placements

    source = (900, 1350)
    layers = [
        # Franja a sangre: escenografía, puede estirarse hasta el borde.
        Layer(
            name="Franja",
            type=LayerType.IMAGE,
            category=LayerCategory.DECORATION,
            x=0,
            y=0,
            width=900,
            height=60,
            src="layers/franja.png",
            z_index=2,
        ),
        # Producto que abarca todo el ancho: jamás se deforma.
        Layer(
            name="Producto ancho",
            type=LayerType.IMAGE,
            category=LayerCategory.PRODUCT,
            x=0,
            y=500,
            width=900,
            height=400,
            src="layers/prod.png",
            z_index=5,
            locked=True,
        ),
    ]
    placements, _ = build_placements(
        layers,
        FAITHFUL_LAYOUT,
        1080,
        1080,
        random.Random(3),
        source_canvas=source,
    )
    by_name = {item.layer.name: item for item in placements}
    assert by_name["Franja"].stretch is True
    assert by_name["Franja"].x == 0 and by_name["Franja"].width == 1080
    assert by_name["Producto ancho"].stretch is False
