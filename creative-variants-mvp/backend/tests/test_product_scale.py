"""Proporciones entre los productos de un combo.

El caso que originó esto: un KV 1080x1920 de Marcimex con una cocina a gas y un
cilindro. El motor sacaba el cilindro igual de alto o más alto que la cocina y la
pieza puntuaba 98/100. Aquí se fija el comportamiento correcto en las tres capas
donde se decide: la tabla de medidas, el motor de layout y el puntaje.
"""
from __future__ import annotations

import random

from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.services import product_scale
from app.services.imaging import fit_contain
from app.services.layout_engine import Placement, VariantPlan, build_placements
from app.services.quality import evaluate_variant

CANVAS = (1080, 1920)
#: Hueco que ocupaban los productos en el arte original.
BOX = (250, 620, 580, 700)


# --------------------------------------------------------------- la tabla
def test_reconoce_la_cocina_y_el_cilindro_del_kv():
    """Los dos productos del KV real, por el nombre del archivo que sube el usuario."""
    cocina = product_scale.measure("cocina-indurama-4q-20p-croma.png")
    cilindro = product_scale.measure("cilindro-de-gas-15kg.png")
    assert cocina is not None and cilindro is not None
    assert cocina.height_cm == 90.0
    assert cilindro.height_cm == 58.0
    # Un cilindro es más bajo que la cocina: eso es todo lo que hay que saber.
    assert cilindro.height_cm < cocina.height_cm


def test_las_pulgadas_de_una_pantalla_son_su_diagonal():
    """Un televisor de 55" mide unos 74 cm de alto, no 55."""
    tv = product_scale.measure('Smart TV 55"')
    assert tv is not None
    assert 70 <= tv.height_cm <= 78
    assert '55"' in tv.family


def test_las_pulgadas_de_una_cocina_son_su_ancho():
    """«20P» en una cocina es el ancho. Leerlo como diagonal la dejaría en 25 cm."""
    veinte = product_scale.measure("COCINA A GAS 4Q 20P CROMA")
    treinta = product_scale.measure("cocina a gas 30p")
    assert veinte is not None and treinta is not None
    assert veinte.height_cm == 90.0
    # Una cocina de 30 pulgadas es algo más alta, no tres veces más alta.
    assert treinta.height_cm == 95.0


def test_manda_el_producto_dominante_del_nombre():
    """«horno microondas» es un microondas; «refrigeradora con congelador», una nevera."""
    micro = product_scale.measure("horno microondas 20 litros.png")
    nevera = product_scale.measure("refrigeradora-no-frost-con-congelador.jpg")
    assert micro is not None and micro.family == "microondas"
    assert nevera is not None and nevera.family == "refrigeradora"


def test_lo_que_no_reconoce_no_lo_inventa():
    """Sin datos, `None`. Es lo que activa el reparto de altos iguales."""
    assert product_scale.measure("Capa 15") is None
    assert product_scale.measure("producto.png") is None
    assert product_scale.measure("") is None
    # Familias de alto imposible de fijar (30 cm o 110 cm) quedan fuera a propósito.
    assert product_scale.measure("aspiradora.png") is None


def test_mide_por_el_archivo_antes_que_por_el_nombre_de_capa():
    """El PSD nombra «Capa 15»; el archivo que sube el usuario sí dice qué es."""
    layer = Layer(
        name="Capa 15",
        type=LayerType.IMAGE,
        category=LayerCategory.PRODUCT,
        width=400,
        height=700,
        meta={"replaced_from": "cilindro-gas.png"},
    )
    found = product_scale.measure_layer(layer)
    assert found is not None and found.family == "cilindro de gas"


def test_altos_relativos_por_medida_real():
    cocina = product_scale.measure("cocina 20p")
    cilindro = product_scale.measure("cilindro")
    ratios, from_real = product_scale.relative_heights([cocina, cilindro])
    assert from_real is True
    assert ratios[0] == 1.0
    assert abs(ratios[1] - 58 / 90) < 0.01


def test_sin_dos_medidas_los_altos_se_igualan():
    """Igualar no afirma nada. Lo de antes sí: el más estrecho salía el más alto."""
    ratios, from_real = product_scale.relative_heights([None, None])
    assert ratios == [1.0, 1.0]
    assert from_real is False
    # Con una sola medida tampoco hay proporción que respetar.
    una = product_scale.measure("cocina")
    ratios, from_real = product_scale.relative_heights([una, None])
    assert ratios == [1.0, 1.0] and from_real is False


def test_nada_baja_del_minimo_legible():
    """Un celular junto a una refrigeradora sería fiel al 9% y también invisible."""
    ratios, _ = product_scale.relative_heights(
        [product_scale.measure("refrigeradora"), product_scale.measure("celular")]
    )
    assert ratios[1] == product_scale.MIN_RELATIVE_HEIGHT


def test_avisa_del_cilindro_mas_grande_que_la_cocina():
    cocina = product_scale.measure("cocina 20p")
    cilindro = product_scale.measure("cilindro")
    avisos = product_scale.proportion_conflicts(
        [("Cocina", cocina, 500), ("Cilindro", cilindro, 520)]
    )
    assert len(avisos) == 1
    assert "Cilindro" in avisos[0] and "Cocina" in avisos[0]


def test_calla_cuando_la_proporcion_es_correcta_o_no_se_sabe():
    cocina = product_scale.measure("cocina 20p")
    cilindro = product_scale.measure("cilindro")
    assert product_scale.proportion_conflicts(
        [("Cocina", cocina, 500), ("Cilindro", cilindro, 322)]
    ) == []
    # Sin medidas no hay nada que comparar: inventar un aviso sería peor.
    assert product_scale.proportion_conflicts(
        [("Uno", None, 500), ("Otro", None, 200)]
    ) == []


# --------------------------------------------------- el motor de layout
def _combo(nombres: dict[str, tuple[int, int]]) -> list[Layer]:
    """Un combo tal como lo deja `replacement.py`: cada recorte ajustado al hueco."""
    layers: list[Layer] = []
    bx, by, bw, bh = BOX
    for index, (nombre, (cut_w, cut_h)) in enumerate(nombres.items()):
        w, h = fit_contain(cut_w, cut_h, bw, bh)
        layers.append(
            Layer(
                id=f"prod{index}",
                name=nombre,
                type=LayerType.IMAGE,
                category=LayerCategory.PRODUCT,
                src=f"layers/p{index}.png",
                x=bx + (bw - w) // 2,
                y=by + (bh - h) // 2,
                width=w,
                height=h,
                z_index=3 + index,
                locked=True,
                preserve_aspect_ratio=True,
                meta={
                    "replacement_box": [bx, by, bw, bh],
                    "replaced_from": nombre,
                    "product_group_id": "g1",
                },
            )
        )
    layers.append(
        Layer(
            id="logo",
            name="Logo",
            type=LayerType.IMAGE,
            category=LayerCategory.LOGO,
            src="layers/logo.png",
            x=380,
            y=90,
            width=320,
            height=70,
            z_index=9,
            locked=True,
        )
    )
    return layers


KV_COMBO = {
    "cocina-indurama-4q-20p-croma.png": (900, 1580),
    "cilindro-de-gas-15kg.png": (700, 1270),
}


def _productos(arrangement: str, seed: int = 7) -> dict[str, Placement]:
    placements, _ = build_placements(
        _combo(KV_COMBO),
        "product_center_headline_top",
        *CANVAS,
        random.Random(seed),
        intensity="moderate",
        source_canvas=CANVAS,
        product_arrangement=arrangement,
    )
    return {
        p.layer.name: p for p in placements if p.layer.category == LayerCategory.PRODUCT
    }


def test_el_cilindro_sale_mas_bajo_que_la_cocina_en_toda_disposicion():
    """La regresión del caso real: antes salía a 1.00-1.03 en las tres."""
    for arrangement in ("auto", "vertical", "horizontal"):
        productos = _productos(arrangement)
        cocina = productos["cocina-indurama-4q-20p-croma.png"].height
        cilindro = productos["cilindro-de-gas-15kg.png"].height
        obtenido = cilindro / cocina
        assert abs(obtenido - 58 / 90) < 0.08, f"{arrangement}: {obtenido:.2f}"


def test_los_productos_lado_a_lado_se_apoyan_en_el_mismo_suelo():
    """Al reducir el pequeño, centrarlo lo dejaría flotando a media altura."""
    productos = _productos("horizontal")
    suelos = {p.y + p.height for p in productos.values()}
    assert max(suelos) - min(suelos) <= 2


def test_no_se_deforma_ninguno_al_escalar():
    for arrangement in ("auto", "vertical", "horizontal"):
        for placement in _productos(arrangement).values():
            original = placement.layer.width / placement.layer.height
            rendered = placement.width / placement.height
            assert abs(original - rendered) / original < 0.05


def test_un_solo_producto_no_cambia():
    """La escala de grupo no debe tocar el KV de un producto, que es el caso normal."""
    solo = {"cocina-indurama-4q-20p-croma.png": (900, 1580)}
    placements, _ = build_placements(
        _combo(solo),
        "product_center_headline_top",
        *CANVAS,
        random.Random(7),
        intensity="moderate",
        source_canvas=CANVAS,
    )
    productos = [p for p in placements if p.layer.category == LayerCategory.PRODUCT]
    assert len(productos) == 1
    # Sigue llenando el hueco aprendido del arte, sin reducción por comparación.
    assert productos[0].height > int(CANVAS[1] * 0.20)


def test_dice_en_los_avisos_por_que_escaló_asi():
    _, notes = build_placements(
        _combo(KV_COMBO),
        "product_center_headline_top",
        *CANVAS,
        random.Random(7),
        intensity="moderate",
        source_canvas=CANVAS,
    )
    assert any("tamaño real" in note for note in notes)


def test_sin_nombres_utiles_iguala_y_lo_explica():
    sin_nombre = {"Capa 15": (900, 1580), "Capa 16": (700, 1270)}
    placements, notes = build_placements(
        _combo(sin_nombre),
        "product_center_headline_top",
        *CANVAS,
        random.Random(7),
        intensity="moderate",
        source_canvas=CANVAS,
        product_arrangement="horizontal",
    )
    alturas = {
        p.height for p in placements if p.layer.category == LayerCategory.PRODUCT
    }
    assert max(alturas) - min(alturas) <= 2
    assert any("igualan los altos" in note for note in notes)


# ---------------------------------------------------------------- puntaje
def _project() -> Project:
    return Project(
        name="Combo",
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


def _plan(placements: list[Placement]) -> VariantPlan:
    return VariantPlan(
        index=0,
        layout="product_center_headline_top",
        layout_label="Producto centrado",
        format="1080x1920",
        width=CANVAS[0],
        height=CANVAS[1],
        seed=1,
        intensity="moderate",
        background_style="plate",
        placements=placements,
    )


def _producto(nombre: str, x: int, y: int, w: int, h: int) -> Placement:
    layer = Layer(
        name=nombre,
        type=LayerType.IMAGE,
        category=LayerCategory.PRODUCT,
        src="layers/p.png",
        width=w,
        height=h,
        meta={"replaced_from": nombre},
    )
    return Placement(layer=layer, x=x, y=y, width=w, height=h, z_index=3)


def test_el_puntaje_castiga_la_proporcion_irreal():
    """Lo que fallaba de verdad: la pieza mala sacaba 98."""
    mala = evaluate_variant(
        _project(),
        _plan(
            [
                _producto("cocina 20p.png", 200, 800, 280, 500),
                _producto("cilindro de gas.png", 560, 780, 300, 520),
            ]
        ),
    )
    buena = evaluate_variant(
        _project(),
        _plan(
            [
                _producto("cocina 20p.png", 200, 800, 280, 500),
                _producto("cilindro de gas.png", 560, 978, 185, 322),
            ]
        ),
    )
    assert mala.metrics["product_scale_conflicts"] == 1.0
    assert buena.metrics["product_scale_conflicts"] == 0.0
    assert buena.score - mala.score >= 20
    assert any("Proporción irreal" in aviso for aviso in mala.warnings)
    assert not any("Proporción irreal" in aviso for aviso in buena.warnings)


def test_el_aviso_de_proporcion_va_primero():
    """La lista se recorta a diez: este es el aviso por el que se descarta la pieza."""
    report = evaluate_variant(
        _project(),
        _plan(
            [
                _producto("cocina 20p.png", -40, 800, 280, 500),
                _producto("cilindro de gas.png", 560, 780, 300, 520),
            ]
        ),
    )
    assert report.warnings
    assert "Proporción irreal" in report.warnings[0]


# ------------------------------------------- validar la pieza y volver a probar
class _PlanFalso:
    """Plan mínimo: al motor de reintentos solo le importa índice, formato y semilla."""

    def __init__(self, index=0, seed=1000):
        self.index = index
        self.seed = seed
        self.format = "1080x1920"
        self.layout = "product_center_headline_top"
        self.notes: list[str] = []
        self.placements: list = []


def _informe(score: int, conflictos: float = 0.0):
    from app.models import QualityReport

    return QualityReport(
        score=score,
        warnings=["algo"] if conflictos else [],
        metrics={"product_scale_conflicts": conflictos},
    )


def _simular(monkeypatch, guion: list[tuple[int, float]]):
    """Cada entrada del guion es (puntaje, conflictos) del siguiente render."""
    from PIL import Image as PilImage

    from app.services import layout_engine as le
    from app.services import quality as q
    from app.services import renderer as r
    from app.services import variants as v

    turnos = iter(guion)
    monkeypatch.setattr(r, "render_variant", lambda *a, **k: (PilImage.new("RGB", (4, 4)), []))
    monkeypatch.setattr(q, "evaluate_variant", lambda *a, **k: _informe(*next(turnos)))
    monkeypatch.setattr(
        le, "replan", lambda project, request, plan, attempt: _PlanFalso(plan.index, plan.seed + attempt)
    )
    return v


def test_una_pieza_limpia_se_compone_una_sola_vez(monkeypatch):
    v = _simular(monkeypatch, [(96, 0.0)])
    _, _, report, attempts = v._compose_until_clean(None, None, _PlanFalso())
    assert attempts == 1
    assert report.score == 96


def test_una_pieza_invalida_se_vuelve_a_plantear(monkeypatch):
    """Antes se entregaba con un aviso; ahora se prueba otra composición."""
    v = _simular(monkeypatch, [(70, 1.0), (94, 0.0)])
    plan, _, report, attempts = v._compose_until_clean(None, None, _PlanFalso())
    assert attempts == 2
    assert report.score == 94
    assert any("intento 2" in note for note in plan.notes)


def test_si_ninguna_sale_limpia_se_entrega_la_mejor_y_se_dice(monkeypatch):
    v = _simular(monkeypatch, [(70, 1.0), (81, 1.0), (75, 1.0)])
    _, _, report, attempts = v._compose_until_clean(None, None, _PlanFalso())
    assert attempts == v.MAX_ATTEMPTS
    assert report.score == 81  # la mejor de las tres, no la última
    assert "Se probaron 3 composiciones" in report.warnings[0]


def test_no_insiste_para_siempre(monkeypatch):
    v = _simular(monkeypatch, [(50, 1.0)] * 10)
    _, _, _, attempts = v._compose_until_clean(None, None, _PlanFalso())
    assert attempts == v.MAX_ATTEMPTS
