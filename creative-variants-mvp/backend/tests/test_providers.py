"""OCR y SAM: que estén, que lean con tildes y que recorten la forma, no la caja.

La suite corre con `ENABLE_OCR=false` y `SEGMENTATION_PROVIDER=local` (ver
conftest) para que las demás pruebas sean rápidas y deterministas, así que aquí
se instancian los proveedores a mano.

Las que necesitan los paquetes pesados se saltan si no están instalados. Eso
hace que `pytest` siga sirviendo en un portátil sin ellos, pero también que un
fallo al instalarlos pase de largo: por eso el flujo de despliegue comprueba
aparte que la imagen los lleve dentro y que queden activos en el servidor.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.providers.rapid_ocr import RapidOcrProvider, _sha_esperados, modelos_invalidos
from app.providers.sam_segmentation import _sam2_config, SamSegmentationProvider
from app.services.renderer import load_font

CHECKPOINT = "/models/sam2.1_hiera_small.pt"


def _ocr_o_saltar(monkeypatch) -> RapidOcrProvider:
    monkeypatch.setattr(settings, "enable_ocr", True)
    proveedor = RapidOcrProvider(lang="es")
    if not proveedor.available():
        pytest.skip(f"OCR no instalado: {proveedor.error}")
    return proveedor


def _sam_o_saltar() -> SamSegmentationProvider:
    proveedor = SamSegmentationProvider(checkpoint=CHECKPOINT, variant="sam2")
    if not proveedor.available():
        pytest.skip(f"SAM no disponible: {proveedor.load_error}")
    return proveedor


def _cartel(texto: str, ruta, ancho: int = 900, alto: int = 220) -> str:
    """Un texto grande y blanco sobre fondo oscuro, como un titular de arte."""
    imagen = Image.new("RGB", (ancho, alto), (18, 22, 40))
    # load_font y no ImageFont.truetype: en macOS no existe DejaVu y esta
    # prueba también corre ahí (la de abajo se salta, esta no).
    fuente = load_font(settings.default_font_bold, 88)
    dibujo = ImageDraw.Draw(imagen)
    caja = dibujo.textbbox((0, 0), texto, font=fuente)
    dibujo.text(
        ((ancho - (caja[2] - caja[0])) / 2, (alto - (caja[3] - caja[1])) / 2 - caja[1]),
        texto,
        font=fuente,
        fill=(255, 255, 255),
    )
    imagen.save(ruta)
    return str(ruta)


# --------------------------------------------------------------------------- OCR
def test_the_ocr_reads_a_headline(monkeypatch, tmp_path):
    proveedor = _ocr_o_saltar(monkeypatch)
    ruta = _cartel("OFERTA", tmp_path / "titular.png")

    resultado = proveedor.read(ruta)

    assert resultado.available, resultado.warnings
    leido = " ".join(region.text for region in resultado.regions).upper()
    assert "OFERTA" in leido, leido


def test_the_ocr_keeps_the_spanish_accents(monkeypatch, tmp_path):
    """El reconocedor chino/inglés devolvía "VALIDO"; el latino conserva la tilde."""
    proveedor = _ocr_o_saltar(monkeypatch)
    ruta = _cartel("PROMOCIÓN", tmp_path / "tilde.png")

    leido = " ".join(region.text for region in proveedor.read(ruta).regions).upper()

    assert "PROMOCIÓN" in leido, leido


def test_the_ocr_places_the_text_where_it_is(monkeypatch, tmp_path):
    proveedor = _ocr_o_saltar(monkeypatch)
    ruta = _cartel("DESCUENTO", tmp_path / "sitio.png", ancho=900, alto=220)

    regiones = proveedor.read(ruta).regions

    assert regiones
    region = max(regiones, key=lambda r: r.width)
    # El titular está centrado y ocupa buena parte del ancho.
    assert region.width > 300, region.width
    assert 0 < region.y < 220
    assert region.color.startswith("#")


def test_the_ocr_says_why_when_it_is_switched_off(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "enable_ocr", False)
    ruta = _cartel("OFERTA", tmp_path / "apagado.png")

    resultado = RapidOcrProvider(lang="es").read(ruta)

    assert not resultado.available
    assert any("ENABLE_OCR" in aviso for aviso in resultado.warnings)


def test_the_latin_recogniser_is_the_one_used_for_spanish(monkeypatch):
    proveedor = _ocr_o_saltar(monkeypatch)

    assert proveedor._rec_lang().value == "latin"
    assert RapidOcrProvider(lang="pt")._rec_lang().value == "latin"


def test_the_models_that_came_in_the_image_are_complete(monkeypatch):
    """Una descarga cortada deja un .onnx a medias que parece bueno hasta usarlo."""
    _ocr_o_saltar(monkeypatch)

    esperados = _sha_esperados()
    assert esperados, "no se pudo leer el registro de modelos de RapidOCR"
    assert any(nombre.startswith("latin_") for nombre in esperados)
    assert modelos_invalidos() == []


# --------------------------------------------------------------------------- SAM
def test_the_config_comes_from_the_checkpoint_name():
    """SAM_MODEL_TYPE trae "vit_b" por omisión, que es de SAM 1 y rompe SAM 2."""
    assert _sam2_config("vit_b", "/models/sam2.1_hiera_small.pt").endswith("hiera_s.yaml")
    assert _sam2_config(None, "/models/sam2.1_hiera_large.pt").endswith("hiera_l.yaml")
    assert _sam2_config(None, "/models/sam2.1_hiera_base_plus.pt").endswith("hiera_b+.yaml")
    assert _sam2_config(None, "/models/lo_que_sea.pt").endswith("hiera_s.yaml")
    # Un yaml explícito manda sobre el nombre del archivo.
    assert _sam2_config("configs/mio.yaml", "/models/sam2.1_hiera_large.pt") == "configs/mio.yaml"


def test_sam_says_what_is_missing_instead_of_failing(monkeypatch):
    # En la imagen SAM_CHECKPOINT viene puesto, y `checkpoint=None` cae en él;
    # hay que vaciarlo para probar el caso de "no está configurado".
    monkeypatch.setattr(settings, "sam_checkpoint", None)
    proveedor = SamSegmentationProvider(variant="sam2")
    assert not proveedor.available()
    assert "SAM_CHECKPOINT" in (proveedor.load_error or "")

    proveedor = SamSegmentationProvider(checkpoint="/models/no-existe.pt", variant="sam2")
    assert not proveedor.available()
    assert "no-existe" in (proveedor.load_error or "")


def test_sam_cuts_the_shape_and_not_the_box(tmp_path):
    proveedor = _sam_o_saltar()
    ruta = tmp_path / "circulo.png"
    imagen = Image.new("RGB", (400, 400), (12, 14, 30))
    ImageDraw.Draw(imagen).ellipse([120, 120, 280, 280], fill=(240, 200, 40))
    imagen.save(ruta)

    mascara = proveedor.segment(str(ruta), box=(100, 100, 200, 200))

    assert mascara.shape == (400, 400)
    pintado = int((mascara > 127).sum())
    area_circulo = np.pi * 80 * 80
    # Si devolviera el rectángulo entero serían 40.000 píxeles; el círculo son
    # unos 20.100. Se acepta un margen, pero no que se coma la caja.
    assert 0.75 * area_circulo < pintado < 1.25 * area_circulo, pintado


def test_sam_encodes_the_same_artwork_only_once(tmp_path):
    """Codificar cuesta ~2 s y sacar cada máscara son milisegundos."""

    class PredictorFalso:
        def __init__(self) -> None:
            self.codificadas = 0

        def set_image(self, rgb) -> None:
            self.codificadas += 1

        def predict(self, **_):
            mascara = np.zeros((300, 300), dtype=bool)
            mascara[10:60, 10:60] = True
            return np.array([mascara]), np.array([0.9]), None

    ruta = tmp_path / "arte.png"
    Image.new("RGB", (300, 300), (30, 30, 30)).save(ruta)

    proveedor = SamSegmentationProvider(checkpoint=CHECKPOINT, variant="sam2")
    falso = PredictorFalso()
    proveedor._predictor = falso

    proveedor.segment(str(ruta), box=(0, 0, 100, 100))
    proveedor.segment(str(ruta), box=(50, 50, 100, 100))
    assert falso.codificadas == 1

    # Si el arte cambia en disco, hay que volver a codificarlo.
    Image.new("RGB", (300, 300), (200, 200, 200)).save(ruta)
    proveedor.segment(str(ruta), box=(0, 0, 100, 100))
    assert falso.codificadas == 2
