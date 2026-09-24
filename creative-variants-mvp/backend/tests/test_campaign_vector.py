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
