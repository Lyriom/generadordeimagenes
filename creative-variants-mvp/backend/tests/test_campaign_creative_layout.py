"""Piezas del renderer que no dependen de una campaña: botón y grupo de productos."""
from __future__ import annotations

from PIL import Image

from app.config import settings
from app.services import campaign_creative


def _fuente() -> str:
    return settings.default_font_bold


def test_el_boton_no_corta_el_cta():
    """"VER PRODUCTOS" salía "VER" en el botón de un 300x250."""

    caja = (20, 20, 90, 26)
    capa = campaign_creative._boton((200, 80), caja, "VER PRODUCTOS", _fuente(), (230, 80, 130))
    pixeles = capa.load()
    # Tinta del texto (blanca u oscura) a lo ancho: con solo "VER" centrado la
    # tinta no pasaba del tercio central del botón.
    columnas = [
        x for x in range(200)
        if any(
            pixeles[x, y][3] > 200 and abs(pixeles[x, y][0] - 230) + abs(pixeles[x, y][1] - 80) > 120
            for y in range(20, 46)
        )
    ]
    assert columnas and (max(columnas) - min(columnas)) > 90 * .55


def test_el_texto_no_se_trunca_a_la_primera_palabra():
    capa = campaign_creative._text_layer(
        (300, 60), (10, 10, 120, 14), "30 cuotas semanales", _fuente(), max_lines=1
    )
    caja = capa.getchannel("A").getbbox()
    assert caja is not None and caja[2] - caja[0] > 120 * .6


def _recorte(size, color) -> Image.Image:
    """Un producto ya recortado: opaco dentro, transparente alrededor."""

    imagen = Image.new("RGBA", (size[0] + 20, size[1] + 20), (0, 0, 0, 0))
    imagen.alpha_composite(Image.new("RGBA", size, (*color, 255)), (10, 10))
    return imagen


def test_varios_productos_forman_un_grupo_con_el_primero_delante():
    productos = [
        _recorte((300, 200), (200, 0, 0)),
        _recorte((100, 250), (0, 200, 0)),
        _recorte((120, 180), (0, 0, 200)),
    ]
    capas = campaign_creative._product_layers((800, 500), (50, 50, 700, 400), productos)

    assert len(capas) == 3
    # El primero se pinta el último (delante) y es el más grande.
    nombre, delante = capas[-1]
    assert nombre == "Producto 1"
    areas = {}
    for nombre, capa in capas:
        caja = capa.getchannel("A").getbbox()
        assert caja and caja[0] >= 45 and caja[2] <= 760
        areas[nombre] = (caja[2] - caja[0]) * (caja[3] - caja[1])
    assert areas["Producto 1"] == max(areas.values())
