"""Recomponer con la retícula del arte: orden de lectura, bandas y reparto.

El caso que motivó todo esto es un banner de 1920x325 pedido en 1080x1350. Con
una familia de layout genérica cada bloque aterrizaba donde dijera la plantilla:
la pieza salía correcta y floja. Lo que se comprueba aquí es que el orden y la
agrupación del original sobreviven al cambio de proporción, y que el reparto de
espacio no vuelve a los tres defectos que aparecieron al medirlo.
"""
from __future__ import annotations

import random

from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.services import source_layout
from app.services.layout_engine import (
    LEGAL_FOOT,
    MAX_UPSCALE,
    SOURCE_FLOW_LAYOUT,
    build_placements,
    plan_variants,
)

BANNER = (1920, 325)


def _capas() -> list[Layer]:
    """El banner separado en capas: se lee marca, copy, producto y CTA."""
    return [
        Layer(id="lg", name="Logo", type=LayerType.IMAGE, category=LayerCategory.LOGO,
              src="l/logo.png", x=60, y=110, width=210, height=90, z_index=9),
        Layer(id="hd", name="Titular", type=LayerType.TEXT, category=LayerCategory.HEADLINE,
              content="HASTA 40% DE DESCUENTO", x=330, y=80, width=520, height=110, z_index=6),
        Layer(id="pr", name="Precio", type=LayerType.TEXT, category=LayerCategory.PRICE,
              content="$235.00", x=330, y=200, width=280, height=80, z_index=6),
        Layer(id="pd", name="Producto", type=LayerType.IMAGE, category=LayerCategory.PRODUCT,
              src="l/prod.png", x=1000, y=25, width=520, height=280, z_index=4),
        Layer(id="ct", name="CTA", type=LayerType.TEXT, category=LayerCategory.CTA,
              content="COMPRA AHORA", x=1620, y=130, width=240, height=64, z_index=7),
        Layer(id="lgl", name="Legal", type=LayerType.TEXT, category=LayerCategory.LEGAL,
              content="Aplican restricciones.", x=60, y=290, width=600, height=24, z_index=2),
    ]


def _colocar(w: int, h: int, layout: str = SOURCE_FLOW_LAYOUT, capas=None):
    return build_placements(
        capas if capas is not None else _capas(),
        layout, w, h, random.Random(7), source_canvas=BANNER,
    )


def _por_categoria(places) -> dict[str, object]:
    return {p.layer.category.value: p for p in places}


def _relleno(places, w: int, h: int, paso: int = 8) -> float:
    """Fracción del lienzo que queda debajo de alguna capa."""
    dentro = 0
    total = 0
    for y in range(0, h, paso):
        for x in range(0, w, paso):
            total += 1
            if any(p.x <= x < p.x + p.width and p.y <= y < p.y + p.height for p in places):
                dentro += 1
    return dentro / total


# --------------------------------------------------------------- orden y grupos
def test_el_banner_se_apila_en_el_orden_en_que_se_leia():
    """De izquierda a derecha pasa a ser de arriba a abajo, no a la plantilla."""
    places, notes = _colocar(1080, 1350)
    por_cat = _por_categoria(places)
    assert por_cat["logo"].y < por_cat["headline"].y < por_cat["product"].y < por_cat["cta"].y
    assert any("orden de lectura" in nota for nota in notes)


def test_lo_que_estaba_junto_en_el_arte_comparte_banda():
    """El precio vivía pegado al titular: separarlos en dos bandas rompe la lectura."""
    places, _ = _colocar(1080, 1350)
    por_cat = _por_categoria(places)
    precio, titular = por_cat["price"], por_cat["headline"]
    solape = min(precio.y + precio.height, titular.y + titular.height) - max(precio.y, titular.y)
    assert solape > 0, "precio y titular deberían quedar a la misma altura"
    assert precio.x < titular.x, "y en el orden que tenían dentro del bloque"


def test_un_lienzo_panoramico_conserva_las_columnas():
    """El eje no está escrito: se resuelven los dos y gana el que llena más."""
    places, notes = _colocar(1200, 628)
    por_cat = _por_categoria(places)
    assert por_cat["logo"].x < por_cat["headline"].x < por_cat["product"].x < por_cat["cta"].x
    assert any("se conservó de izquierda a derecha" in nota for nota in notes)


# ---------------------------------------------------------------- los tres fallos
def test_llena_mas_lienzo_que_una_familia_generica():
    """El defecto de fondo: la plantilla dejaba la pieza medio vacía."""
    reticula, _ = _colocar(1080, 1350)
    for generica in ("vertical_stack", "product_center_headline_top"):
        plantilla, _ = _colocar(1080, 1350, layout=generica)
        assert _relleno(reticula, 1080, 1350) > _relleno(plantilla, 1080, 1350) + 0.05


def test_la_ultima_banda_no_pisa_el_pie_del_legal():
    """Si la pisa, el motor la manda a otro lado y se pierde el orden."""
    places, _ = _colocar(1080, 1350)
    por_cat = _por_categoria(places)
    assert por_cat["cta"].y + por_cat["cta"].height <= int(1350 * LEGAL_FOOT) + 4
    assert por_cat["cta"].y > por_cat["product"].y, "el CTA sigue siendo el último"


def test_una_imagen_no_pide_banda_que_no_puede_llenar():
    """Un logo de 210x90 no necesita media pieza: no se amplía más de MAX_UPSCALE."""
    places, _ = _colocar(1080, 1350)
    logo = _por_categoria(places)["logo"]
    assert logo.width <= 210 * MAX_UPSCALE + 1
    assert logo.height <= 90 * MAX_UPSCALE + 1


def test_el_texto_no_se_queda_en_una_rendija():
    """Cuando no cabe todo se recorta de las imágenes antes que del copy."""
    places, _ = _colocar(1080, 1350)
    por_cat = _por_categoria(places)
    for clave in ("headline", "price", "cta"):
        alto = por_cat[clave].height
        assert alto >= source_layout.MIN_FONT * 1350, f"'{clave}' quedó ilegible: {alto}px"


def test_el_titular_no_se_come_la_pieza():
    """Pedir el cuerpo máximo para cada texto dejaba al producto en un cuarto."""
    places, _ = _colocar(1080, 1350)
    por_cat = _por_categoria(places)
    assert por_cat["product"].height > por_cat["headline"].height


# ------------------------------------------------------------------- geometría
def test_con_un_solo_bloque_no_hay_reticula_que_conservar():
    solo = [c for c in _capas() if c.id == "pd"]
    places, notes = _colocar(1080, 1350, capas=solo)
    assert len(places) == 1
    assert any("no tiene bloques suficientes" in nota for nota in notes)


def test_el_eje_de_lectura_sale_de_la_forma_del_arte():
    items = [
        source_layout.Item(key="a", box=(0.0, 0.0, 0.2, 0.9), aspect=1.0),
        source_layout.Item(key="b", box=(0.6, 0.0, 0.2, 0.9), aspect=1.0),
    ]
    assert source_layout.reading_axis(items, (1920, 325)) == "x"
    assert source_layout.reading_axis(items, (1080, 1920)) == "y"
    # En un cuadrado lo dice el reparto de los bloques, no la forma.
    assert source_layout.reading_axis(items, (1080, 1080)) == "x"


def test_las_bandas_no_se_pisan_ni_se_salen():
    zones, _ = source_layout.derive_zones(
        [
            source_layout.Item(key="logo", box=(0.03, 0.3, 0.11, 0.28), aspect=2.3,
                               natural=(210, 90), ref_y=0.07, ref_x=0.20),
            source_layout.Item(key="headline", box=(0.17, 0.25, 0.27, 0.34),
                               font_cap=0.115, max_lines=3, chars=22, ref_y=0.15, ref_x=0.39),
            source_layout.Item(key="product", box=(0.52, 0.08, 0.27, 0.86), aspect=1.86,
                               natural=(520, 280), ref_y=0.34, ref_x=0.48),
        ],
        BANNER, 1080, 1350, reserve=1.0 - LEGAL_FOOT,
    )
    assert set(zones) == {"logo", "headline", "product"}
    for x, y, w, h in zones.values():
        assert 0.0 <= x and 0.0 <= y and x + w <= 1.001 and y + h <= LEGAL_FOOT + 0.001
    ordenadas = sorted(zones.values(), key=lambda z: z[1])
    for previa, siguiente in zip(ordenadas, ordenadas[1:]):
        assert previa[1] + previa[3] <= siguiente[1] + 0.001


# ------------------------------------------------------------------ integración
class _Peticion:
    def __init__(self, formats):
        self.formats = formats
        self.count = 6
        self.seed = 7
        self.intensity = "moderate"


def _proyecto() -> Project:
    return Project(
        name="Banner",
        canvas=Canvas(width=BANNER[0], height=BANNER[1]),
        source=SourceImage(path="original/a.png", width=BANNER[0], height=BANNER[1],
                           format="PNG", original_filename="a.png", bytes=10),
        layers=_capas(),
    )


def test_el_formato_que_obliga_a_recomponer_usa_la_reticula_del_arte():
    """Y el que se parece al original no: ahí conservar el diseño es mejor."""
    project = _proyecto()
    plans, _ = plan_variants(project, _Peticion(["1080x1350", "google_display_728x90"]))
    verticales = [p for p in plans if p.format == "1080x1350"]
    banners = [p for p in plans if p.format == "google_display_728x90"]
    assert any(p.layout == SOURCE_FLOW_LAYOUT for p in verticales)
    # La otra mitad sigue probando familias distintas: la variedad vale.
    assert any(p.layout != SOURCE_FLOW_LAYOUT for p in verticales)
    assert all(p.layout != SOURCE_FLOW_LAYOUT for p in banners)


def test_si_el_usuario_elige_los_layouts_no_se_le_cambian():
    project = _proyecto()
    peticion = _Peticion(["1080x1350"])
    peticion.layouts = ["product_left"]  # type: ignore[attr-defined]
    plans, _ = plan_variants(project, peticion)
    assert {p.layout for p in plans} == {"product_left"}
