"""La plantilla se compone con las medidas del arte, no con una retícula fija.

Lo que se protege es la queja que originó el cambio: "los fondos no se
reconstruyen, solo salen borrosos", "no ocupa logos", "no me está dando buenas
composiciones". Cada una tiene aquí su prueba.
"""
from __future__ import annotations

from PIL import Image

from app.services import campaign_layout_from_art as desde_arte
from app.services import campaign_plate


def _lectura(texto, caja, *, conf=.99, role="content", color=""):
    return {
        "text": texto, "bbox": list(caja), "confidence": conf,
        "role": role, "color": color,
    }


#: Un KV 4:5 típico, con las cajas donde las pone un diseñador de retail.
LECTURAS = [
    _lectura("MARCIMEX", (62, 66, 359, 114), role="brand", color="#FFFFFF"),
    _lectura("CREDIFEST", (55, 195, 587, 284), color="#FFFFFF"),
    _lectura("Hasta 40% de descuento", (59, 301, 521, 347), color="#1C1D65"),
    _lectura("$499", (81, 840, 300, 932), color="#14184F"),
    _lectura("antes $899", (643, 856, 817, 890), color="#2B247C"),
    _lectura("Cuotas desde $25 al mes", (60, 1084, 521, 1126), color="#FFFFFF"),
    _lectura(
        "Promocion valida hasta agotar stock. Consulte terminos y condiciones.",
        (60, 1282, 718, 1309), color="#7F78B5",
    ),
]
MEDIDA = (1080, 1350)


def test_cada_texto_del_arte_acaba_en_el_hueco_que_le_toca():
    """El precio donde iba el precio, el legal donde iba el legal."""

    cajas = desde_arte.clasificar(LECTURAS, MEDIDA)
    por_hueco = {
        hueco: next(item["text"] for item in LECTURAS if list(item["bbox"]) == list(caja))
        for hueco, caja in cajas.items()
    }

    assert por_hueco["logo"] == "MARCIMEX"
    assert por_hueco["headline"] == "CREDIFEST"
    assert por_hueco["price"] == "$499"
    assert por_hueco["previous_price"] == "antes $899"
    assert por_hueco["installment"] == "Cuotas desde $25 al mes"
    assert por_hueco["discount"] == "Hasta 40% de descuento"
    assert por_hueco["legal"].startswith("Promocion valida")


def test_el_precio_anterior_no_le_roba_el_sitio_al_precio():
    """Los dos llevan moneda; el que dice "antes" es el tachado."""

    cajas = desde_arte.clasificar(LECTURAS, MEDIDA)
    assert cajas["price"] != cajas["previous_price"]
    # El precio actual es el de mayor cuerpo.
    assert cajas["price"][3] - cajas["price"][1] > cajas["previous_price"][3] - cajas["previous_price"][1]


def test_el_producto_ocupa_el_hueco_que_el_diseno_dejo_libre():
    """Sin esto la foto usaba la caja centrada de la retícula y tapaba todo."""

    cajas = desde_arte.clasificar(LECTURAS, MEDIDA)
    hueco = desde_arte.hueco_producto(
        [tuple(item["bbox"]) for item in LECTURAS], MEDIDA
    )

    assert hueco is not None
    _x0, arriba, _x1, abajo = hueco
    # Entre el descuento (acaba en 347) y el precio (empieza en 840).
    assert 347 <= arriba < abajo <= 840
    # Y no pisa ninguna caja de texto.
    for caja in cajas.values():
        assert caja[3] <= arriba or caja[1] >= abajo


def test_un_arte_sin_hueco_franco_deja_el_producto_a_la_reticula():
    """Encajar la foto en un interlineado es peor que la posición genérica."""

    apretadas = [(0, y, 900, y + 120) for y in range(0, 1340, 130)]
    assert desde_arte.hueco_producto(apretadas, MEDIDA) is None


def test_las_posiciones_se_entregan_dentro_del_area_segura():
    """``_layout`` mide dentro del área segura; un valor fuera de 0..1 revienta."""

    segura = {"left": .04, "top": .04, "right": .04, "bottom": .04}
    posiciones = desde_arte.placements(LECTURAS, MEDIDA, segura)

    assert "price" in posiciones and "headline" in posiciones
    for hueco, caja in posiciones.items():
        assert 0 <= caja.x <= 1 and 0 <= caja.y <= 1, hueco
        assert caja.width > 0 and caja.height > 0, hueco
        # Con holgura de una diezmilésima: las coordenadas se guardan
        # redondeadas a 4 decimales y 0.9887 + 0.0113 no da 1.0 en binario.
        assert caja.x + caja.width <= 1.0001, hueco
        assert caja.y + caja.height <= 1.0001, hueco
    # El titular sigue arriba y el legal abajo: el orden del arte se conserva.
    assert posiciones["headline"].y < posiciones["price"].y < posiciones["legal"].y


def test_una_story_no_hereda_las_medidas_de_un_feed():
    assert desde_arte.aspect_key((1080, 1350)) == "portrait"
    assert desde_arte.aspect_key((1080, 1920)) == "story"
    assert desde_arte.aspect_key((1080, 1080)) == "square"
    assert desde_arte.aspect_key((1200, 628)) == "landscape"


# ---------------------------------------------------------------- placa plana
class _OcrFalso:
    """Lee lo que se le diga, con el contrato de RapidOCR."""

    name = "ocr-de-prueba"

    def __init__(self, regiones=LECTURAS):
        self.regiones = regiones

    def available(self):
        return True

    def read(self, _ruta):
        from app.providers.base import OcrResult, TextRegion

        return OcrResult(regions=[
            TextRegion(
                text=item["text"], x=item["bbox"][0], y=item["bbox"][1],
                width=item["bbox"][2] - item["bbox"][0],
                height=item["bbox"][3] - item["bbox"][1],
                confidence=item["confidence"], color=item["color"] or "#FFFFFF",
            )
            for item in self.regiones
        ])


class _SinOcr:
    name = "ninguno"

    def available(self):
        return False


def _arte():
    arte = Image.new("RGB", MEDIDA, (24, 28, 96))
    arte.paste((240, 88, 34), (600, 60, 1060, 520))
    return arte


def test_el_wordmark_de_la_marca_no_se_borra_de_la_placa():
    """Borrarlo dejaba la plantilla sin la firma del cliente."""

    cajas, leidas, motivo = campaign_plate.text_boxes(
        _arte(), _OcrFalso(), brand_name="Marcimex"
    )

    assert motivo == ""
    marca = next(item for item in leidas if item["text"] == "MARCIMEX")
    assert marca["role"] == "brand"
    assert [62, 66, 359, 114] not in [list(caja) for caja in cajas]
    # El resto sí se borra: es el contenido de la pieza anterior.
    assert [55, 195, 587, 284] in [list(caja) for caja in cajas]


def test_sin_ocr_no_hay_placa_en_vez_de_una_placa_con_el_precio_viejo(tmp_path):
    """El mal resultado conocido: hornear el precio anterior en cada pieza."""

    hecho = campaign_plate.build_plate_from_artwork(
        _arte(), tmp_path / "placa.png", ocr_provider=_SinOcr()
    )

    assert hecho is None


def test_la_placa_de_un_arte_plano_borra_el_texto_y_conserva_el_diseno(tmp_path):
    destino = tmp_path / "placa.png"

    hecho = campaign_plate.build_plate_from_artwork(
        _arte(), destino, ocr_provider=_OcrFalso(), brand_name="Marcimex"
    )

    assert hecho is not None
    ruta, _motor, _avisos, leidas = hecho
    assert ruta.exists()
    with Image.open(ruta) as placa:
        assert placa.size == MEDIDA
        # La mancha naranja del diseño sigue ahí: no se borró el arte.
        assert placa.convert("RGB").getpixel((800, 300))[0] > 180
    assert len(leidas) == len(LECTURAS)


def test_el_desenfoque_de_referencia_ya_no_existe():
    """Era la causa directa de los fondos borrosos. No debe volver."""

    from app.services import campaign_creative

    assert not hasattr(campaign_creative, "_reference_texture")


# ------------------------------------------------------- tipografias de prueba
def test_una_tipografia_de_prueba_se_detecta_por_su_tabla_de_nombres(tmp_path, monkeypatch):
    """El «DEMO» que aparecía sobre los artes venía de una cara sin licencia.

    No se mira el nombre del archivo, que se renombra en un segundo, sino la
    tabla de nombres que firma la fundición. Una cara con licencia del catálogo
    real del cliente pasa; la misma anunciándose como prueba, no.
    """
    from pathlib import Path as _Path

    from PIL import ImageFont

    from app.services import campaign_ingestion

    licenciada = _Path("app/assets/client_fonts/marcimex/bfef2aa4026ec6c0723d.otf")
    renombrada = tmp_path / "Titular-DEMO-cualquier-cosa.otf"
    renombrada.write_bytes(licenciada.read_bytes())
    # El nombre del archivo grita DEMO y aun así es una cara con licencia.
    assert campaign_ingestion.font_trial_name(renombrada) == ""

    real = ImageFont.truetype

    def _firmada_como_prueba(ruta, size, *args, **kwargs):
        cara = real(ruta, size, *args, **kwargs)
        cara.getname = lambda: ("Goldplay DEMO", "Regular")  # type: ignore[method-assign]
        return cara

    monkeypatch.setattr(ImageFont, "truetype", _firmada_como_prueba)
    assert campaign_ingestion.font_trial_name(renombrada) == "Goldplay DEMO Regular"


def test_una_cara_de_prueba_no_dibuja_el_arte(tmp_path):
    """Puede quedarse como evidencia; lo que no puede es firmar el entregable."""

    from app.models.campaign import Campaign, CampaignSource, CampaignSourceKind
    from app.services import campaign_creative

    campaign = Campaign(client_id="c", name="Campana")
    campaign.sources = [
        CampaignSource(
            filename="Titular-Bold.otf", kind=CampaignSourceKind.FONT,
            media_type="font/otf", extension=".otf", size_bytes=10, sha256="a" * 64,
            stored_path="sources/x/original.otf", meta={"font_trial": "Goldplay DEMO"},
        )
    ]

    elegida = campaign_creative._font_path(campaign, bold=True)

    assert "sources/x/original.otf" not in elegida
