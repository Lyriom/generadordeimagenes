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


def test_lo_que_estaba_apilado_sigue_apilado_y_a_todo_el_ancho():
    """Compartir banda es repartirse el ancho, y eso arruinaba el copy.

    En el arte el precio iba debajo del titular. Metidos en la misma banda cada
    uno se queda con media anchura y el texto se hunde: fue así como
    «ELECTROMENORES» acabó en 23 px. Apilados conservan el ancho entero.
    """
    places, _ = _colocar(1080, 1350)
    por_cat = _por_categoria(places)
    titular, precio = por_cat["headline"], por_cat["price"]
    assert titular.y < precio.y, "el titular iba encima del precio y sigue encima"
    assert titular.width > 1080 * 0.5, f"el titular se quedó estrecho: {titular.width}px"
    # Y nada se cuela entre los dos: eran el mismo sitio del arte.
    entre = [
        p for p in places
        if titular.y + titular.height <= p.y < precio.y and p.layer.category.value != "legal"
    ]
    assert entre == []


def test_en_columnas_si_se_reparten_el_alto():
    """El eje de salida decide: en un panorámico el bloque sí es una columna."""
    places, _ = _colocar(1200, 628)
    por_cat = _por_categoria(places)
    titular, precio = por_cat["headline"], por_cat["price"]
    solape = min(titular.x + titular.width, precio.x + precio.width) - max(titular.x, precio.x)
    assert solape > 0, "titular y precio comparten columna"
    assert titular.y < precio.y, "y en el orden en que estaban apilados"


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
        assert alto >= source_layout.MIN_FONT_PX, f"'{clave}' quedó ilegible: {alto}px"


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
    """Y solo eso: una familia genérica ahí no da una pieza publicable.

    Medido con el banner del caso, las familias daban entre 50 y 58 puntos con
    «el producto invade 'Titular'»; la retícula, entre 73 y 88. Una pieza con el
    producto encima del titular no es una alternativa de estilo, está rota.
    """
    project = _proyecto()
    plans, _ = plan_variants(project, _Peticion(["1080x1350"]))
    verticales = [p for p in plans if p.format == "1080x1350"]
    assert verticales
    assert all(p.layout == SOURCE_FLOW_LAYOUT for p in verticales)


def test_una_tira_tambien_usa_la_reticula_aunque_la_proporcion_encaje():
    """En 728x90 el titular de una familia genérica cae en la franja del producto.

    Las zonas de una familia son fracciones del lienzo pensadas para formatos de
    una pieza. En una tira salía «el producto invade 'Titular'» cuatro veces.
    """
    project = _proyecto()
    plans, _ = plan_variants(project, _Peticion(["google_display_728x90"]))
    assert any(p.layout == SOURCE_FLOW_LAYOUT for p in plans)


def test_un_formato_normal_no_se_toca():
    """La regla es para lo que no encaja: un arte cuadrado a un 4:5 se compone igual."""
    project = _proyecto()
    project.canvas.width, project.canvas.height = 1080, 1080
    project.source.width, project.source.height = 1080, 1080
    plans, _ = plan_variants(project, _Peticion(["1080x1350"]))
    assert plans
    assert all(p.layout != SOURCE_FLOW_LAYOUT for p in plans)


def test_si_el_usuario_elige_los_layouts_no_se_le_cambian():
    project = _proyecto()
    peticion = _Peticion(["1080x1350"])
    peticion.layouts = ["product_left"]  # type: ignore[attr-defined]
    plans, _ = plan_variants(project, peticion)
    assert {p.layout for p in plans} == {"product_left"}


def test_una_decoracion_entra_en_la_reticula_y_no_se_queda_suelta():
    """Anclada al sitio del original quedaba en medio de otra composición."""
    capas = _capas() + [
        Layer(id="dc", name="Sello", type=LayerType.IMAGE, category=LayerCategory.DECORATION,
              src="l/sello.png", x=1260, y=80, width=300, height=180, z_index=3),
    ]
    places, _ = _colocar(1080, 1350, capas=capas)
    sello = _por_categoria(places)["decoration"]
    assert not sello.pinned, "la decoración no se ancla cuando se deduce la retícula"
    producto = _por_categoria(places)["product"]
    cta = _por_categoria(places)["cta"]
    assert producto.y < sello.y < cta.y, "estaba entre el producto y el CTA en el arte"


def test_fuera_de_la_reticula_la_decoracion_sigue_anclada():
    """La regla vieja se mantiene: una plantilla genérica no sabe dónde ponerla."""
    capas = _capas() + [
        Layer(id="dc", name="Sello", type=LayerType.IMAGE, category=LayerCategory.DECORATION,
              src="l/sello.png", x=1260, y=80, width=300, height=180, z_index=3),
    ]
    places, _ = _colocar(1080, 1350, layout="vertical_stack", capas=capas)
    assert _por_categoria(places)["decoration"].pinned


def test_dos_capas_de_la_misma_categoria_conservan_su_orden():
    """El caso real: en la misma columna, uno encima del titular y otro debajo.

    Juntándolos por categoría la caja que los une pasa por encima del titular y
    el orden se pierde: salían los dos arriba y el titular después.
    """
    capas = _capas() + [
        Layer(id="sb1", name="Subtítulo", type=LayerType.TEXT,
              category=LayerCategory.SUBHEADLINE, content="SOLO ESTE MES",
              x=330, y=40, width=322, height=54, z_index=5),
        Layer(id="sb2", name="Subtítulo 2", type=LayerType.TEXT,
              category=LayerCategory.SUBHEADLINE, content="12 MESES SIN INTERESES",
              x=336, y=240, width=369, height=31, z_index=5),
    ]
    places, _ = _colocar(1080, 1350, capas=capas)
    por_id = {p.layer.id: p for p in places}
    assert por_id["sb1"].y < por_id["hd"].y < por_id["sb2"].y
    # Y cada uno con el ancho entero, no media banda cada uno.
    for clave in ("sb1", "sb2", "hd"):
        assert por_id[clave].width > 1080 * 0.5


def test_un_combo_sigue_viajando_como_un_bloque():
    """Tres productos son un bloque visual: el reparto interno no es del reflujo."""
    capas = [c for c in _capas() if c.category != LayerCategory.PRODUCT] + [
        Layer(id=f"pd{i}", name=f"Producto {i}", type=LayerType.IMAGE,
              category=LayerCategory.PRODUCT, src=f"l/p{i}.png",
              x=1000 + i * 180, y=25, width=170, height=280, z_index=4)
        for i in range(3)
    ]
    places, _ = _colocar(1080, 1350, capas=capas)
    productos = [p for p in places if p.layer.category == LayerCategory.PRODUCT]
    assert len(productos) == 3
    alturas = {p.y for p in productos}
    assert len(alturas) == 1, "los tres van en la misma banda, uno al lado del otro"


def test_el_suelo_de_legibilidad_es_en_pixeles():
    """En proporción al lienzo daba 1 px en una tira de 50 y todo parecía caber.

    Con siete bloques en 320x50 la retícula aceptaba repartir bandas de siete
    píxeles: el texto acababa fuera del lienzo y la pieza en 39 puntos.
    """
    textos = [
        source_layout.Item(
            key=f"t{i}", box=(i * 0.12, 0.1, 0.11, 0.8),
            font_cap=0.06, max_lines=2, chars=18, longest_word=9,
        )
        for i in range(8)
    ]
    assert source_layout.derive_zones(textos, BANNER, 320, 50) == ({}, [])
    # Y con sitio de verdad, la misma retícula sale.
    zones, notas = source_layout.derive_zones(textos, BANNER, 1080, 1350)
    assert len(zones) == 8 and notas


def test_la_reticula_se_ofrece_solo_donde_cabe():
    from app.services.layout_engine import reflow_viable

    assert reflow_viable(_capas(), BANNER, 728, 90)
    # Un lienzo diminuto no aguanta ni una banda legible por bloque.
    assert not reflow_viable(_capas(), BANNER, 120, 30)


def test_la_pieza_igual_al_kv_sigue_estando_en_los_formatos_cercanos():
    project = _proyecto()
    plans, _ = plan_variants(project, _Peticion(["google_display_320x50"]))
    assert any(p.layout == "faithful" for p in plans)


# ------------------------------------------------------- capacidad del formato
def _capas_reales() -> list[Layer]:
    """Las que salieron de separar el banner del caso real, con tres productos."""
    capas = [
        Layer(id="sb1", name="Subtítulo", type=LayerType.TEXT,
              category=LayerCategory.SUBHEADLINE, content="ESPECIAL DE",
              x=33, y=88, width=322, height=54, z_index=5),
        Layer(id="hd", name="Titular", type=LayerType.TEXT, category=LayerCategory.HEADLINE,
              content="ELECTROMENORES", x=31, y=148, width=588, height=68, z_index=6),
        Layer(id="sb2", name="Subtítulo 2", type=LayerType.TEXT,
              category=LayerCategory.SUBHEADLINE, content="12 MESES SIN INTERESES",
              x=37, y=240, width=369, height=31, z_index=5),
        Layer(id="pr", name="Precio", type=LayerType.TEXT, category=LayerCategory.PRICE,
              content="30% OFF", x=1628, y=135, width=205, height=58, z_index=6),
        Layer(id="dc", name="Decoración", type=LayerType.IMAGE,
              category=LayerCategory.DECORATION, src="l/d.png",
              x=1258, y=78, width=306, height=185, z_index=3),
    ]
    capas += [
        Layer(id=f"p{i}", name=f"Producto {i + 1}", type=LayerType.IMAGE,
              category=LayerCategory.PRODUCT, src=f"l/p{i}.png",
              x=798 + i * 130, y=53, width=125, height=235, z_index=4)
        for i in range(3)
    ]
    return capas


def test_una_tira_dice_cuantos_elementos_le_caben():
    """La pregunta que había que contestar antes de generar, no después."""
    from app.services.layout_engine import format_capacity

    capas = _capas_reales()
    tira = format_capacity(capas, BANNER, 300, 60)
    assert tira.crowded
    assert tira.total == 6, "tres productos cuentan como un bloque"
    assert tira.fits < tira.total
    # Se sueltan los menos importantes primero, con el mismo orden que ya usa el
    # motor para resolver solapamientos.
    assert tira.dropped[0] == "Decoración"
    assert "Titular" not in tira.dropped and "3 productos" not in tira.dropped


def test_un_formato_grande_aguanta_el_arte_completo():
    from app.services.layout_engine import format_capacity

    for ancho, alto in ((1080, 1350), (1080, 1080), (1200, 628)):
        cupo = format_capacity(_capas_reales(), BANNER, ancho, alto)
        assert not cupo.crowded, f"{ancho}x{alto} debería aguantar todo"
        assert cupo.fits == cupo.total


def test_cuanto_mas_pequena_la_tira_menos_le_entra():
    from app.services.layout_engine import format_capacity

    capas = _capas_reales()
    cabidas = [format_capacity(capas, BANNER, w, h).fits for w, h in ((300, 60), (728, 90), (970, 90))]
    assert cabidas[0] < cabidas[1] <= cabidas[2]


def test_se_avisa_antes_de_generar_y_una_vez_por_formato():
    """Doce piezas apretadas con la explicación al final es hacer perder el tiempo."""
    project = _proyecto()
    project.layers = _capas_reales()
    peticion = _Peticion(["youtube_companion", "google_display_970x90"])
    peticion.count = 8
    plans, warnings = plan_variants(project, peticion)
    assert plans, "se genera igual: avisar no es negarse"
    apretados = [w for w in warnings if "solo entran" in w]
    # Uno por formato apretado y no uno por pieza: ocho variantes, dos avisos.
    assert len(apretados) == 2, apretados
    assert sum(1 for w in apretados if "300x60" in w) == 1
    assert sum(1 for w in apretados if "970x90" in w) == 1
    tira = next(w for w in apretados if "300x60" in w)
    assert "«Decoración»" in tira and "«Subtítulo 2»" in tira
    assert "Revisar capas" in tira


def test_caber_a_la_fuerza_no_es_caber():
    """Con los mínimos cabe casi todo; con lo que el diseño pide, no."""
    capas = _capas_reales()
    items, opciones = __import__(
        "app.services.layout_engine", fromlist=["_reflow_inputs"]
    )._reflow_inputs(capas, BANNER, 300, 60)
    assert source_layout.viable(items, BANNER, 300, 60, **opciones)
    assert not source_layout.fits_comfortably(items, BANNER, 300, 60, **opciones)
