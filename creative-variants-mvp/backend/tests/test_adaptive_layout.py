"""Layout "adaptivo": conserva el diseño, reparte la holgura entre sus bandas.

El motivo de este archivo: pedir varios formatos con "conservar el diseño"
devolvía piezas con el 41% del lienzo en bandas muertas, puntuadas 100/100 —
"faithful" con una proporción distinta a la de origen es, por definición, un
letterbox, y el control de calidad no lo veía. Estas pruebas fijan el
contrato del layout que lo repara: una sola escala (nada se deforma), la
holgura se reparte entre las bandas del propio arte en vez de en los bordes,
el producto puede crecer para absorber parte de esa holgura, el legal se
ancla al pie, y una capa de fondo a sangre no rompe el reparto de las demás.
"""
from __future__ import annotations

import random

from app.models import Layer, LayerCategory, LayerType
from app.services import quality
from app.services.layout_engine import (
    ADAPTIVE_LAYOUT,
    FAITHFUL_LAYOUT,
    VariantPlan,
    _adaptive_boxes,
    build_placements,
)
from app.services.source_layout import _shares, Item

#: Un arte compacto (900x660, aspecto 1.36) con bloques dispersos verticalmente:
#: logo arriba, titular en medio, producto a la derecha centro, marca al pie.
#: Es una versión reducida del KV real que reveló el defecto.
SOURCE = (900, 660)


def _layers() -> list[Layer]:
    return [
        # Fondo a sangre completa: la plancha de color. Debe IGNORARSE al
        # calcular las bandas (no es contenido), pero seguir viéndose en el
        # render con su propia zona anclada.
        Layer(
            id="fondo",
            name="Fondo",
            type=LayerType.IMAGE,
            category=LayerCategory.DECORATION,
            src="layers/fondo.png",
            x=0,
            y=0,
            width=900,
            height=660,
            z_index=0,
        ),
        Layer(
            id="logo",
            name="Logo",
            type=LayerType.IMAGE,
            category=LayerCategory.LOGO,
            src="layers/logo.png",
            x=43,
            y=39,
            width=371,
            height=155,
            z_index=9,
        ),
        Layer(
            id="titular",
            name="Titular",
            type=LayerType.TEXT,
            category=LayerCategory.HEADLINE,
            content="HASTA 60% DE DESCUENTO",
            x=41,
            y=242,
            width=314,
            height=192,
            z_index=6,
        ),
        Layer(
            id="producto",
            name="Producto",
            type=LayerType.IMAGE,
            category=LayerCategory.PRODUCT,
            src="layers/producto.png",
            x=428,
            y=149,
            width=358,
            height=348,
            z_index=4,
            locked=True,
        ),
        Layer(
            id="marca",
            name="Marca",
            type=LayerType.IMAGE,
            category=LayerCategory.DECORATION,
            src="layers/marca.png",
            x=498,
            y=538,
            width=378,
            height=86,
            z_index=8,
        ),
        Layer(
            id="legal",
            name="Legal",
            type=LayerType.TEXT,
            category=LayerCategory.LEGAL,
            content="Aplican condiciones.",
            x=50,
            y=612,
            width=297,
            height=13,
            z_index=10,
        ),
    ]


def _content_bbox(placements) -> tuple[int, int, int, int]:
    """Caja envolvente de las capas que no son escenografía a sangre."""
    contenido = [
        p
        for p in placements
        if not (p.width >= SOURCE[0] * 0.9 and p.height >= SOURCE[1] * 0.9)
        and p.layer.category != LayerCategory.LEGAL
    ]
    x0 = min(p.x for p in contenido)
    y0 = min(p.y for p in contenido)
    x1 = max(p.x + p.width for p in contenido)
    y1 = max(p.y + p.height for p in contenido)
    return x0, y0, x1, y1


def test_una_capa_a_sangre_no_colapsa_el_reparto_en_una_banda():
    """El bug real: una capa a sangre sin zona propia tumbaba TODO su grupo.

    `_source_items` excluye la escenografía a sangre de las bandas —no define
    una banda de lectura—, pero esa capa sigue en el mismo grupo de categoría
    dentro de `build_placements`. Ese grupo solo usa las zonas calculadas si
    TODAS sus capas tienen una: sin dársela también a la capa a sangre, el
    grupo entero caía al reparto genérico de "faithful" y diez decoraciones
    se amontonaban en su esquina reservada.
    """
    zonas, notas = _adaptive_boxes(_layers(), SOURCE, 1080, 1350)
    assert zonas
    assert "fondo" in zonas
    # La capa a sangre conserva su tamaño (con la escala uniforme): no se
    # reparte como una banda más, solo se ancla.
    _, _, fw, fh = zonas["fondo"]
    assert abs(fw * 1080 - 900 * (1080 / 900)) < 2
    assert abs(fh * 1350 - 660 * (1080 / 900)) < 2


def test_la_holgura_se_reparte_y_no_queda_en_bandas_muertas():
    """900x660 a 1080x1350: antes, el 41% del lienzo quedaba vacío arriba y abajo."""
    plan = VariantPlan(
        index=0,
        layout=ADAPTIVE_LAYOUT,
        layout_label="",
        format="meta_feed_4_5",
        width=1080,
        height=1350,
        seed=1,
        intensity="conservative",
        background_style="plate",
    )
    placements, _ = build_placements(
        _layers(),
        ADAPTIVE_LAYOUT,
        1080,
        1350,
        random.Random(1),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    plan.placements = placements
    x0, y0, x1, y1 = _content_bbox(placements)
    banda_v = (y0 + (1350 - y1)) / 1350
    # El original (sin arreglar) dejaba el 41%: cualquier valor bastante por
    # debajo de eso confirma que la holgura se repartió, no que se dejó en
    # los bordes. El margen de seguridad del layout ya ocupa un ~7%.
    assert banda_v < 0.18, f"banda muerta vertical de {banda_v:.0%}, se esperaba < 18%"


def test_nada_cambia_de_proporcion():
    """Una sola escala uniforme: ninguna IMAGEN se deforma al repartir la holgura.

    El texto queda fuera de esta comprobación a propósito: se reenvuelve al
    espacio disponible (`_fit_font_size`), igual que en cualquier otro layout
    del motor. Cambiar de forma es correcto para él; no lo es para una foto o
    un logo.
    """
    placements, _ = build_placements(
        _layers(),
        ADAPTIVE_LAYOUT,
        1080,
        1350,
        random.Random(3),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    by_id = {p.layer.id: p for p in placements}
    for layer in _layers():
        if layer.is_text:
            continue
        placement = by_id[layer.id]
        original = layer.width / layer.height
        rendered = placement.width / max(1, placement.height)
        assert abs(original - rendered) / original < 0.05, layer.name


def test_el_orden_vertical_de_lectura_se_conserva():
    """Logo arriba, titular y producto en medio, marca y legal al pie."""
    placements, _ = build_placements(
        _layers(),
        ADAPTIVE_LAYOUT,
        1080,
        1350,
        random.Random(5),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    by_id = {p.layer.id: p for p in placements}
    assert by_id["logo"].y < by_id["titular"].y
    assert by_id["titular"].y < by_id["marca"].y
    assert by_id["marca"].y <= by_id["legal"].y


def test_el_legal_se_ancla_al_pie():
    placements, _ = build_placements(
        _layers(),
        ADAPTIVE_LAYOUT,
        1080,
        1920,
        random.Random(2),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    legal = next(p for p in placements if p.layer.id == "legal")
    assert legal.y + legal.height >= 1920 * 0.90


def test_el_producto_puede_crecer_mas_que_el_resto():
    """En un formato muy alto, el producto absorbe holgura en vez de flotar solo."""
    placements_originales, _ = build_placements(
        _layers(), FAITHFUL_LAYOUT, 900, 660, random.Random(1),
        intensity="conservative", source_canvas=SOURCE,
    )
    placements_adaptados, _ = build_placements(
        _layers(), ADAPTIVE_LAYOUT, 1080, 1920, random.Random(1),
        intensity="conservative", source_canvas=SOURCE,
    )
    original = next(p for p in placements_originales if p.layer.id == "producto")
    adaptado = next(p for p in placements_adaptados if p.layer.id == "producto")
    escala_lienzo = 1080 / 900
    escala_producto = adaptado.height / original.height
    assert escala_producto > escala_lienzo * 1.005


def test_una_proporcion_parecida_no_activa_el_reparto():
    """900x660 a un lienzo casi igual: nada que repartir, cae al ancla fiel."""
    zonas, _ = _adaptive_boxes(_layers(), SOURCE, 918, 673)
    assert zonas == {}


def test_sin_zonas_el_layout_se_comporta_como_faithful():
    """`keep_all_relative` es la red de seguridad cuando no hubo holgura real."""
    placements, _ = build_placements(
        _layers(), ADAPTIVE_LAYOUT, 918, 673, random.Random(1),
        intensity="conservative", source_canvas=SOURCE,
    )
    by_id = {p.layer.id: p for p in placements}
    scale = 918 / 900
    assert abs(by_id["logo"].x - 43 * scale) < 3
    assert abs(by_id["logo"].y - 39 * scale) < 3


def test_el_control_de_calidad_marca_el_letterbox_fuera_de_la_proporcion_nativa():
    """El bug de fondo: un letterbox con todo anclado sacaba 100/100."""
    from app.models import Canvas, Project, SourceImage

    project = Project(
        project_id="00000000-0000-4000-8000-00000000adad",
        name="letterbox",
        canvas=Canvas(width=SOURCE[0], height=SOURCE[1]),
        source=SourceImage(
            path="original/a.png", width=SOURCE[0], height=SOURCE[1],
            format="PNG", original_filename="a.png", bytes=1,
        ),
        layers=_layers(),
    )
    placements, _ = build_placements(
        _layers(), FAITHFUL_LAYOUT, 1080, 1350, random.Random(1),
        intensity="conservative", source_canvas=SOURCE,
    )
    plan = VariantPlan(
        index=0, layout=FAITHFUL_LAYOUT, layout_label="", format="meta_feed_4_5",
        width=1080, height=1350, seed=1, intensity="conservative",
        background_style="plate", placements=placements,
    )
    report = quality.evaluate_variant(project, plan)
    assert report.metrics.get("letterbox") == 1.0
    assert "letterbox" in quality.BLOCKING_METRICS
    assert quality.blocking_defects(report)


def test_con_zona_manual_el_producto_no_participa_del_reparto():
    """Manda el recuadro, no el reflujo: sin esto quedaba un hueco "fantasma".

    Si el producto siguiera contando para el reparto de bandas aunque tuviera
    zona manual, el resto de bloques recibirían MENOS holgura de la que
    deberían —parte se "reservaría" para un producto que en realidad ya no
    vive ahí—.
    """
    zonas_libres, _ = _adaptive_boxes(_layers(), SOURCE, 1080, 1350)
    assert "producto" in zonas_libres

    recuadro = (0.6, 0.6, 0.3, 0.3)
    zonas_con_recuadro, _ = _adaptive_boxes(_layers(), SOURCE, 1080, 1350, recuadro)
    assert "producto" not in zonas_con_recuadro


def test_el_reparto_de_banda_pesa_la_prioridad_no_solo_el_tamano():
    """Un producto y una rayita decorativa de ancho parecido no deben repartirse igual."""
    producto = Item(key="producto", box=(0.1, 0.1, 0.4, 0.4), aspect=1.0, priority=92)
    rayita = Item(key="rayita", box=(0.5, 0.1, 0.4, 0.02), aspect=20.0, priority=12)
    shares = _shares([producto, rayita], axis="y")
    assert shares[0] > shares[1] * 1.5, (
        "el producto (prioridad 92) debe llevarse bastante más ancho que la "
        "rayita decorativa (prioridad 12), aunque midan casi lo mismo"
    )


# ------------------------------------------------- importado de PSD, no a mano
def _como_de_psd(capas: list[Layer]) -> list[Layer]:
    """Las mismas capas tal y como las deja `psd_import`.

    El importador marca `mandatory_art` en logo, titular, subtítulo, precio, CTA
    y legal: significa que sus PÍXELES salen del recorte del PSD y nunca se
    redibujan. Todo lo demás de la capa es idéntico.
    """
    from app.services.psd_import import MANDATORY_ART_CATEGORIES

    for capa in capas:
        if capa.category in MANDATORY_ART_CATEGORIES:
            capa.meta["mandatory_art"] = True
    return capas


def _posiciones(capas: list[Layer], layout: str, salida=(1080, 1920)) -> dict[str, tuple[int, int]]:
    places, _ = build_placements(
        capas,
        layout,
        *salida,
        random.Random(1),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    return {p.layer.id: (p.x, p.y) for p in places}


def test_venir_de_un_psd_no_cambia_donde_cae_nada():
    """`mandatory_art` dice de dónde salen los píxeles, no dónde va la capa.

    Estaban confundidos, y el precio lo pagaba justo el caso normal: un KV
    importado de PSD. El titular, el precio, el logo y el legal se anclaban a su
    sitio del original —la copia fiel, con sus bandas muertas— mientras el
    producto sí se movía a la banda que le tocaba. Salía el letterbox de
    "faithful" con el producto suelto en medio, que es peor que cualquiera de
    los dos, y los avisos seguían diciendo que la pieza se había recompuesto.

    Las capas manuales nunca lo sufrieron, y por eso el resto de este archivo no
    lo veía: ninguna de sus capas lleva la marca.
    """
    for layout in (ADAPTIVE_LAYOUT, "source_flow"):
        a_mano = _posiciones(_layers(), layout)
        de_psd = _posiciones(_como_de_psd(_layers()), layout)
        assert de_psd == a_mano, (
            f"{layout}: importar de PSD movió las capas.\n"
            f"  a mano: {a_mano}\n"
            f"  de psd: {de_psd}"
        )


def test_el_kv_de_psd_tampoco_queda_en_bandas_muertas():
    """La consecuencia visible, medida como la mide el control de calidad."""
    capas = _como_de_psd(_layers())
    places, _ = build_placements(
        capas,
        ADAPTIVE_LAYOUT,
        1080,
        1920,
        random.Random(1),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    contenido = [p for p in places if not quality._is_full_bleed(p, 1080, 1920)]
    arriba, abajo, _izq, _der = quality._dead_bands(contenido, 1080, 1920)
    assert arriba + abajo < quality.DEAD_BAND_TOTAL, (
        f"bandas muertas de {(arriba + abajo) * 100:.0f}%: el arte sigue flotando"
    )

    # Y con "faithful" sí las tiene: la comparación es lo que da sentido a la de
    # arriba, y fija que el layout fiel no ha cambiado de comportamiento.
    fieles, _ = build_placements(
        _como_de_psd(_layers()),
        FAITHFUL_LAYOUT,
        1080,
        1920,
        random.Random(1),
        intensity="conservative",
        source_canvas=SOURCE,
    )
    f_contenido = [p for p in fieles if not quality._is_full_bleed(p, 1080, 1920)]
    f_arriba, f_abajo, _, _ = quality._dead_bands(f_contenido, 1080, 1920)
    assert f_arriba + f_abajo >= quality.DEAD_BAND_TOTAL
