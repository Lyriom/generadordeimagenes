"""Plantilla a partir del editable vectorial (.ai / PDF con capas).

El caso real: el toolkit de CrediFest enseña cada KV enmarcado dentro de una
lámina. El logo y el sello se repiten en todas; el producto recortado, su
nombre y el precio solo en su KV. La placa debe salir del propio PDF con lo
variable quitado, y los huecos donde estaba lo quitado.
"""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from app.models.formats import FORMAT_PRESETS
from app.services import campaign_vector as vector


def _png(color, size, alpha: bool, dibujo: int = 0) -> bytes:
    modo = "RGBA" if alpha else "RGB"
    fondo = (0, 0, 0, 0) if alpha else color
    imagen = Image.new(modo, size, fondo)
    if alpha:
        # Un recorte con forma: el centro opaco, el borde transparente. El
        # dibujo interior distingue un producto de otro, como en una foto real.
        for x in range(size[0] // 4, size[0] * 3 // 4):
            for y in range(size[1] // 4, size[1] * 3 // 4):
                luz = 255 if ((x * (dibujo + 1)) // 7 + (y * (3 - dibujo)) // 5) % 2 else 90
                imagen.putpixel((x, y), (*(c * luz // 255 for c in color), 255))
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


def _toolkit(path: Path) -> None:
    """Dos láminas con un KV enmarcado cada una, más una "layout base"."""

    fondo = _png((90, 40, 140), (160, 200), alpha=False)
    logo = _png((250, 80, 200), (80, 80), alpha=True)
    doc = fitz.open()
    productos = [(20, 200, 60), (200, 200, 30)]
    for indice in range(3):
        page = doc.new_page(width=1200, height=700)
        marco = fitz.Rect(300, 100, 700, 600)
        page.insert_image(marco, stream=fondo)
        page.insert_image(fitz.Rect(320, 120, 420, 220), stream=logo)
        page.insert_text((330, 560), "CREDITO DIRECTO", fontsize=14, color=(1, 1, 1))
        if indice < 2:
            producto = _png(productos[indice], (120, 200), alpha=True, dibujo=indice + 1)
            page.insert_image(fitz.Rect(520, 250, 690, 560), stream=producto)
            page.insert_text((330, 330), "REFRIGERADORA TOP MOUNT", fontsize=12, color=(1, 1, 1))
            page.insert_text((330, 390), "$359", fontsize=40, color=(1, 1, 0))
            page.insert_text((330, 420), "12 cuotas de $29.92", fontsize=12, color=(1, 1, 1))
    doc.save(path)
    doc.close()


def test_el_toolkit_da_piezas_con_producto_y_campos_variables(tmp_path: Path):
    ruta = tmp_path / "toolkit.pdf"
    _toolkit(ruta)
    doc = fitz.open(ruta)
    piezas = vector.find_pieces(doc)
    doc.close()

    assert len(piezas) == 3
    kv, _otro, base = piezas
    # El logo sale en las tres láminas: es identidad, no producto.
    assert len(kv.products) == 1
    assert not base.products
    variables = {texto.text for texto in kv.variable_texts}
    assert "$359" in variables
    assert "12 cuotas de $29.92" in variables
    # Compartido por los dos KV de producto pero ausente de la base: es el
    # ejemplo de producto, no un rótulo fijo.
    assert "REFRIGERADORA TOP MOUNT" in variables
    # El sello está en las tres, incluida la base sin producto: es fijo.
    assert "CREDITO DIRECTO" not in variables
    assert vector.best_pieces(piezas)[0] is kv


def test_la_placa_sale_del_pdf_sin_producto_ni_precio(tmp_path: Path):
    ruta = tmp_path / "toolkit.pdf"
    _toolkit(ruta)
    doc = fitz.open(ruta)
    kv = vector.best_pieces(vector.find_pieces(doc))[0]
    doc.close()

    ancho, alto, escala = vector.render_plate(ruta, kv, tmp_path / "placa.png")
    with Image.open(tmp_path / "placa.png") as placa:
        placa = placa.convert("RGB")
        caja = vector.product_box(kv, escala)
        centro = ((caja[0] + caja[2]) // 2, (caja[1] + caja[3]) // 2)
        # Donde estaba el producto verde queda el fondo morado del marco.
        r, g, b = placa.getpixel(centro)
        assert g < 100 and b > 100
        # El logo sigue: es lo que hace que la placa sea de la marca.
        lx, ly = int((370 - 300) * escala), int((170 - 100) * escala)
        assert placa.getpixel((lx, ly))[0] > 200

    cajas, _lecturas = vector.field_boxes(kv, escala)
    assert {"price", "installment", "product", "product_name"} <= set(cajas)
    assert (ancho, alto) == (int(round(kv.width * escala)), int(round(kv.height * escala)))


def test_las_cuatro_familias_salen_con_placa_y_posiciones(tmp_path: Path):
    ruta = tmp_path / "toolkit.pdf"
    _toolkit(ruta)
    resumen = vector.build_templates(ruta, tmp_path / "assets")

    familias = {placa["family"]: placa for placa in resumen["plates"]}
    assert set(familias) == {"portrait", "square", "story", "landscape"}
    assert familias["portrait"]["origin"] == "piece"
    assert familias["story"]["origin"] == "adapted"
    for familia, placa in familias.items():
        with Image.open(tmp_path / "assets" / placa["file"]) as imagen:
            assert list(imagen.size) == placa["size"]
        assert "price" in resumen["placements"][familia]
        assert "product" in resumen["placements"][familia]
    assert resumen["text_colors"]["price"] == "#FFFF00"


def test_la_recomposicion_mantiene_la_cifra_dentro_de_su_pastilla():
    capa = Image.new("RGBA", (400, 500), (0, 0, 0, 0))
    pastilla = Image.new("RGBA", (160, 80), (230, 80, 130, 255))
    capa.alpha_composite(pastilla, (40, 250))
    precio = (60, 270, 180, 310)
    safe = {k: float(v) for k, v in FORMAT_PRESETS["meta_stories"]["safe_area"].items()}
    placa, cajas = vector.adapt(None, capa, {"price": precio}, (450, 800), safe)

    x0, y0, x1, y1 = cajas["price"]
    muestra = placa.getpixel(((x0 + x1) // 2, (y0 + y1) // 2))
    assert muestra[:3] == (230, 80, 130)


def test_un_desplegable_con_muchos_precios_es_catalogo(tmp_path: Path):
    ruta = tmp_path / "desplegable.pdf"
    doc = fitz.open()
    page = doc.new_page(width=780, height=1090)
    for fila in range(3):
        for columna in range(3):
            page.insert_text(
                (40 + columna * 250, 200 + fila * 300), f"${9 + fila}.99 semanales", fontsize=18
            )
    doc.save(ruta)
    doc.close()

    doc = fitz.open(ruta)
    piezas = vector.find_pieces(doc)
    doc.close()
    assert piezas and piezas[0].catalog
    assert vector.best_pieces(piezas) == []
    resumen = vector.build_templates(ruta, tmp_path / "assets")
    assert resumen["plates"] == [] and resumen["catalog_pieces"] == 1


def test_el_horizontal_redistribuye_en_vez_de_encoger():
    """De un 4:5 a un 1200x628 anclando, todo quedaba diminuto."""

    capa = Image.new("RGBA", (800, 1000), (0, 0, 0, 0))
    capa.alpha_composite(Image.new("RGBA", (300, 300), (250, 0, 200, 255)), (40, 40))
    capa.alpha_composite(Image.new("RGBA", (300, 200), (230, 80, 130, 255)), (40, 450))
    capa.alpha_composite(Image.new("RGBA", (200, 120), (0, 200, 60, 255)), (40, 800))
    campos = {"price": (60, 480, 320, 600), "product": (450, 200, 760, 950)}
    safe = {k: float(v) for k, v in FORMAT_PRESETS["meta_feed_landscape"]["safe_area"].items()}

    _placa, cajas = vector.adapt(None, capa, campos, (1600, 838), safe)

    minima = min(1600 * .93 / 800, 838 * .93 / 1000)
    x0, y0, x1, y1 = cajas["price"]
    assert (x1 - x0) > 260 * minima * 1.3
    # El producto va a la derecha, sin pisar la columna de identidad.
    assert cajas["product"][0] > x1


def test_sin_precio_la_placa_pierde_la_pastilla_y_nada_mas(tmp_path: Path):
    ruta = tmp_path / "toolkit.pdf"
    _toolkit(ruta)
    resumen = vector.build_templates(ruta, tmp_path / "assets")
    retrato = next(placa for placa in resumen["plates"] if placa["family"] == "portrait")
    assert retrato["bare_file"]
    with Image.open(tmp_path / "assets" / retrato["file"]) as completa, \
            Image.open(tmp_path / "assets" / retrato["bare_file"]) as desnuda:
        assert completa.size == desnuda.size


def test_un_banner_pone_todo_en_una_fila_dentro_del_lienzo():
    """320x50 y 728x90 recortaban la placa horizontal y cortaban el logo."""

    capa = Image.new("RGBA", (800, 1000), (0, 0, 0, 0))
    capa.alpha_composite(Image.new("RGBA", (300, 300), (250, 0, 200, 255)), (40, 40))
    capa.alpha_composite(Image.new("RGBA", (120, 40), (255, 255, 255, 255)), (640, 40))
    capa.alpha_composite(Image.new("RGBA", (300, 200), (230, 80, 130, 255)), (40, 450))
    capa.alpha_composite(Image.new("RGBA", (200, 120), (0, 200, 60, 255)), (40, 800))
    campos = {"price": (60, 480, 320, 600), "product": (450, 200, 760, 950)}
    safe = {"left": .035, "top": .035, "right": .035, "bottom": .035}

    placa, cajas = vector.adapt(None, capa, campos, (728, 90), safe)

    assert placa.size == (728, 90)
    for x0, y0, x1, y1 in cajas.values():
        assert 0 <= x0 < x1 <= 728 and 0 <= y0 < y1 <= 90
    # Lockup, producto y su oferta al lado: de izquierda a derecha.
    assert cajas["product"][2] <= cajas["price"][0]


def _kv():
    """Un KV de juguete: lockup arriba a la izquierda, logo de marca en la
    esquina, pastilla con precio y dos sellos abajo; el producto a la derecha."""

    capa = Image.new("RGBA", (800, 1000), (0, 0, 0, 0))
    capa.alpha_composite(Image.new("RGBA", (300, 300), (250, 0, 200, 255)), (40, 40))
    capa.alpha_composite(Image.new("RGBA", (120, 30), (255, 255, 255, 255)), (640, 40))
    capa.alpha_composite(Image.new("RGBA", (300, 200), (230, 80, 130, 255)), (40, 450))
    capa.alpha_composite(Image.new("RGBA", (90, 90), (0, 200, 60, 255)), (40, 800))
    capa.alpha_composite(Image.new("RGBA", (90, 90), (0, 90, 200, 255)), (170, 800))
    campos = {"price": (60, 480, 320, 600), "product": (450, 200, 760, 950)}
    return capa, campos


def _safe(preset: str) -> dict[str, float]:
    return {k: float(v) for k, v in FORMAT_PRESETS[preset]["safe_area"].items()}


def _dentro(caja, zona) -> bool:
    return zona[0] - 2 <= caja[0] and zona[1] - 2 <= caja[1] and caja[2] <= zona[2] + 2 and caja[3] <= zona[3] + 2


def _pisa(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def test_el_titular_y_el_cta_tienen_su_bloque_y_no_pisan_nada():
    """Antes se apilaban encima del producto; ahora la retícula les da sitio."""

    capa, campos = _kv()
    safe = _safe("meta_feed_square")
    _placa, cajas = vector.adapt(None, capa, campos, (1080, 1080), safe, extras=("headline", "cta"))

    zona = (1080 * .035, 1080 * .035, 1080 * .965, 1080 * .965)
    for clave in ("headline", "cta", "price", "product"):
        assert clave in cajas and _dentro(cajas[clave], zona), clave
    assert not _pisa(cajas["headline"], cajas["product"])
    assert not _pisa(cajas["cta"], cajas["product"])
    assert not _pisa(cajas["headline"], cajas["cta"])
    # Barra de mensaje: el botón a la derecha del titular, como en retail.
    assert cajas["cta"][0] >= cajas["headline"][2]


def test_un_producto_apaisado_va_en_una_franja_a_lo_ancho():
    """Un televisor en la columna alta de una refrigeradora salía diminuto."""

    capa, campos = _kv()
    safe = _safe("meta_feed_4_5")
    _p, alto = vector.adapt(None, capa, campos, (1080, 1350), safe, aspecto=.4)
    _p, ancho = vector.adapt(None, capa, campos, (1080, 1350), safe, aspecto=1.8)

    def w(c):
        return c[2] - c[0]

    assert w(ancho["product"]) > 1080 * .85
    assert w(ancho["product"]) > w(alto["product"]) * 1.5


def test_en_un_reel_el_texto_respeta_la_zona_y_el_producto_baja():
    """El 35 % de abajo lo tapa la interfaz: sin texto, pero no vacío."""

    capa, campos = _kv()
    safe = _safe("meta_reels")
    _placa, cajas = vector.adapt(None, capa, campos, (1080, 1920), safe, extras=("headline", "cta"))

    zona = (1080 * safe["left"], 1920 * safe["top"], 1080 * (1 - safe["right"]), 1920 * (1 - safe["bottom"]))
    for clave in ("headline", "cta", "price"):
        assert _dentro(cajas[clave], zona), clave
    assert cajas["product"][3] > zona[3] + 1920 * .1


def test_sin_precio_el_nombre_pasa_al_bloque_y_la_pastilla_no_deja_hueco():
    capa, campos = _kv()
    campos["product_name"] = (60, 455, 320, 478)
    safe = _safe("meta_feed_square")
    placa, cajas = vector.adapt(
        None, capa, campos, (1080, 1080), safe,
        omit={"price", "installment"}, extras=("product_name", "headline", "cta"),
    )

    assert "price" not in cajas
    assert {"product_name", "headline", "cta"} <= set(cajas)
    # Ni rastro del rosa de la pastilla en la placa.
    rosa = sum(
        1 for pixel in placa.convert("RGB").getdata()
        if abs(pixel[0] - 230) < 8 and abs(pixel[1] - 80) < 8 and abs(pixel[2] - 130) < 8
    )
    assert rosa == 0
    assert cajas["product_name"][3] <= cajas["headline"][1] + 2


def test_la_franja_de_la_pastilla_da_el_ancho_del_texto():
    """El nombre del ejemplo ocupaba dos tercios de la franja: uno más largo no cabía."""

    capa = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    capa.alpha_composite(Image.new("RGBA", (300, 40), (3, 185, 235, 255)), (50, 50))
    cajas = vector.ensanchar(capa, {"product_name": (70, 55, 200, 85), "product": (0, 0, 10, 10)})

    x0, y0, x1, y1 = cajas["product_name"]
    assert x0 <= 70 and 300 < x1 <= 350
    assert (y0, y1) == (55, 85)
    assert cajas["product"] == (0, 0, 10, 10)
