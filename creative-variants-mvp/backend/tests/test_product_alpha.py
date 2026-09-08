"""Recortar el producto que llega con su fondo.

Un catálogo entrega el producto sobre blanco: `A06656.png`,
`AWHMM20C01-01.webp`. Antes eso solo se avisaba y el KV terminaba con un
rectángulo blanco pegado encima. Aquí se comprueba que se recorta, que se
recorta bien, y que no se gasta una consulta de red en lo que no hace falta.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services import product_alpha


def _catalogo(fondo=(255, 255, 255), ruido: int = 0, puerta=(250, 250, 252)) -> Image.Image:
    """Un microondas sobre fondo de estudio, con una puerta casi del color del fondo."""
    img = Image.new("RGB", (800, 600), fondo)
    dibujo = ImageDraw.Draw(img)
    dibujo.rounded_rectangle([140, 150, 660, 450], radius=18, fill=(38, 40, 44))
    dibujo.rectangle([175, 185, 470, 415], fill=puerta)
    dibujo.ellipse([540, 330, 610, 400], fill=(200, 202, 208))
    if ruido:
        crudo = np.asarray(img).astype(np.int16)
        rng = np.random.default_rng(3)
        crudo = np.clip(crudo + rng.integers(-ruido, ruido + 1, crudo.shape), 0, 255)
        img = Image.fromarray(crudo.astype(np.uint8))
    return img


def _ambiente() -> Image.Image:
    """El producto fotografiado en una cocina: no hay un color de fondo que quitar."""
    img = Image.new("RGB", (800, 600), (210, 200, 185))
    dibujo = ImageDraw.Draw(img)
    dibujo.rectangle([0, 0, 800, 120], fill=(190, 195, 200))
    dibujo.rectangle([0, 380, 800, 600], fill=(150, 130, 105))
    dibujo.rounded_rectangle([240, 200, 560, 400], radius=14, fill=(40, 42, 46))
    return img


def _opaco(image: Image.Image) -> float:
    return float((np.asarray(image.getchannel("A")) > product_alpha.OPAQUE).mean())


class _RecortadorFalso:
    """El recortador de red, sin red."""

    def __init__(self, *, disponible: bool = True, salida: Image.Image | None = None) -> None:
        self.disponible = disponible
        self.salida = salida
        self.llamadas = 0

    def available(self) -> bool:
        return self.disponible

    def cutout(self, image_path: str, output_path: str | None = None) -> str:
        self.llamadas += 1
        assert output_path, "el recortador escribe en el archivo que se le pide"
        salida = self.salida or Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        if self.salida is None:
            ImageDraw.Draw(salida).ellipse([80, 60, 320, 240], fill=(30, 30, 30, 255))
        salida.save(output_path, format="PNG")
        return output_path


@pytest.fixture
def sin_red(monkeypatch):
    """Ningún test toca la red: el recortador falla si alguien lo construye."""

    def prohibido(*_args, **_kwargs):
        raise AssertionError("no se debía consultar al recortador")

    monkeypatch.setattr(product_alpha, "MagnificCutoutProvider", prohibido)


# ------------------------------------------------------------------ fondo plano
def test_un_producto_sobre_blanco_se_recorta_solo(tmp_path: Path, sin_red):
    ruta = tmp_path / "AWHMM20C01-01.png"
    _catalogo().save(ruta)
    recortada, avisos, metodo = product_alpha.cutout(
        Image.open(ruta).convert("RGBA"), ruta
    )
    assert metodo == "fondo-plano"
    assert avisos == []
    # El microondas ocupa 520x300 de 800x600: un tercio del archivo.
    assert 0.28 < _opaco(recortada) < 0.38
    # Y las esquinas quedan transparentes, que es lo que tapaba el arte.
    alpha = np.asarray(recortada.getchannel("A"))
    assert alpha[5, 5] == 0 and alpha[-5, -5] == 0


def test_la_parte_clara_del_producto_no_se_agujerea(tmp_path: Path, sin_red):
    """La puerta es casi del color del fondo, pero es producto."""
    ruta = tmp_path / "microondas.png"
    _catalogo().save(ruta)
    recortada, _, _ = product_alpha.cutout(Image.open(ruta).convert("RGBA"), ruta)
    alpha = np.asarray(recortada.getchannel("A"))
    assert alpha[300, 320] > 200, "el centro de la puerta se quedó sin recortar"


def test_un_fondo_con_ruido_de_compresion_sigue_siendo_plano(tmp_path: Path, sin_red):
    """Un blanco de catálogo en JPG no es un blanco, es un blanco con granos."""
    ruta = tmp_path / "con_ruido.png"
    _catalogo(ruido=6).save(ruta)
    _, _, metodo = product_alpha.cutout(Image.open(ruta).convert("RGBA"), ruta)
    assert metodo == "fondo-plano"


def test_un_gris_de_estudio_tambien_es_fondo(tmp_path: Path, sin_red):
    ruta = tmp_path / "gris.png"
    _catalogo(fondo=(238, 238, 240)).save(ruta)
    _, _, metodo = product_alpha.cutout(Image.open(ruta).convert("RGBA"), ruta)
    assert metodo == "fondo-plano"


# ------------------------------------------------------------------ lo que no
def test_lo_que_ya_viene_recortado_no_se_toca(tmp_path: Path, sin_red):
    ruta = tmp_path / "recortado.png"
    limpio = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(limpio).ellipse([60, 60, 340, 340], fill=(20, 60, 200, 255))
    limpio.save(ruta)
    recortada, avisos, metodo = product_alpha.cutout(
        Image.open(ruta).convert("RGBA"), ruta
    )
    assert metodo == "ya-transparente"
    assert avisos == []
    assert list(recortada.getdata()) == list(limpio.getdata())


def test_una_imagen_de_un_solo_color_no_se_consulta_al_recortador(
    tmp_path: Path, sin_red
):
    """No hay producto dentro: preguntarlo era gastar una consulta por nada."""
    ruta = tmp_path / "plano.png"
    Image.new("RGB", (400, 400), (10, 120, 200)).save(ruta)
    _, avisos, metodo = product_alpha.cutout(Image.open(ruta).convert("RGBA"), ruta)
    assert metodo == "sin-recorte"
    assert any("no hay ningún producto que recortar" in aviso for aviso in avisos)


def test_una_foto_de_ambiente_no_se_recorta_por_color(tmp_path: Path):
    """Con pared arriba y encimera abajo, el color no separa nada: es del modelo."""
    ruta = tmp_path / "ambiente.png"
    _ambiente().save(ruta)
    alpha, motivo = product_alpha.flat_backdrop_alpha(Image.open(ruta).convert("RGBA"))
    assert alpha is None
    assert motivo and "color" in motivo


# --------------------------------------------------------------- el recortador
def test_el_recortador_se_usa_cuando_el_color_no_basta(tmp_path: Path, monkeypatch):
    ruta = tmp_path / "ambiente.png"
    _ambiente().save(ruta)
    falso = _RecortadorFalso()
    monkeypatch.setattr(product_alpha, "MagnificCutoutProvider", lambda: falso)
    recortada, avisos, metodo = product_alpha.cutout(
        Image.open(ruta).convert("RGBA"), ruta
    )
    assert metodo == "magnific"
    assert falso.llamadas == 1
    assert avisos == []
    assert 0.02 < _opaco(recortada) < 0.9
    assert not (ruta.with_name(f"{ruta.stem}_cutout.png")).exists()


def test_si_el_recortador_no_esta_se_dice_y_se_sigue(tmp_path: Path, monkeypatch):
    ruta = tmp_path / "ambiente.png"
    _ambiente().save(ruta)
    monkeypatch.setattr(
        product_alpha, "MagnificCutoutProvider", lambda: _RecortadorFalso(disponible=False)
    )
    imagen, avisos, metodo = product_alpha.cutout(
        Image.open(ruta).convert("RGBA"), ruta
    )
    assert metodo == "sin-recorte"
    assert any("se verá su fondo rectangular" in aviso for aviso in avisos)
    assert imagen.getchannel("A").getextrema() == (255, 255)


def test_un_recorte_que_devuelve_la_imagen_entera_se_rechaza(
    tmp_path: Path, monkeypatch
):
    """Si el modelo no recortó nada, pegarlo igual es pegar el fondo."""
    ruta = tmp_path / "ambiente.png"
    _ambiente().save(ruta)
    entera = Image.new("RGBA", (400, 300), (10, 10, 10, 255))
    monkeypatch.setattr(
        product_alpha, "MagnificCutoutProvider", lambda: _RecortadorFalso(salida=entera)
    )
    _, avisos, metodo = product_alpha.cutout(Image.open(ruta).convert("RGBA"), ruta)
    assert metodo == "sin-recorte"
    assert any("100%" in aviso for aviso in avisos)
