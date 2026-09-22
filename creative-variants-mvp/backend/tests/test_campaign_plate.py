"""La placa de plantilla: el arte real del PSD sin su contenido variable.

Lo que se protege aqui es sobre todo lo que NO debe pasar. Una plantilla que
conserva un producto de muestra es un defecto conocido y acotado; una a la que
se le ha borrado media escena y rellenado a ojo esta rota, y no hay forma de
notarlo hasta que se han producido cincuenta artes con ella.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from app.services import campaign_plate


def _arte(size=(600, 600)) -> Image.Image:
    """Escena con un 'producto' central bien delimitado."""

    arte = Image.new("RGB", size, (190, 170, 150))
    arte.paste((90, 60, 40), (180, 200, 420, 430))
    return arte


def _manifiesto(canvas=(600, 600)) -> list[dict]:
    return [
        # Fondo a sangre: nunca se borra, dejaria un hueco en vez de plantilla.
        {"name": "Capa 8", "visible": True, "bbox": [0, 0, canvas[0], canvas[1]]},
        # Sellos y legal: esto si es contenido de cada arte.
        {"name": "70%", "visible": True, "bbox": [20, 20, 160, 90]},
        {"name": "Promocion valida", "visible": True, "bbox": [40, 550, 560, 580]},
        # Congelado: es identidad, se queda.
        {"name": "logo marci", "visible": True, "bbox": [400, 20, 570, 60]},
    ]


class _SamFalso:
    """Segmentador con el contrato de SAM y una silueta franca del producto."""

    name = "sam"

    def __init__(self, ratio=.16):
        self.ratio = ratio

    def available(self):
        return True

    def detect(self, _path):
        from app.providers.base import Detection

        # Detection lleva x/y/ancho/alto, no dos esquinas.
        lado = int((600 * 600 * self.ratio) ** .5)
        return [Detection(x=180, y=200, width=lado, height=lado, score=.9, label="producto")]

    def segment(self, _path, box=None, points=None, text_prompt=None):
        mask = np.zeros((600, 600), dtype=np.uint8)
        x, y, ancho, alto = box
        mask[y:y + alto, x:x + ancho] = 255
        return mask


def test_la_placa_borra_sellos_y_legal_pero_conserva_el_fondo_a_sangre(tmp_path):
    destino = tmp_path / "placa.png"

    hecho = campaign_plate.build_plate(
        _arte(), _manifiesto(), {"logo marci"}, destino
    )

    assert hecho is not None
    ruta, _motor, _avisos = hecho
    assert ruta.exists()
    with Image.open(ruta) as placa:
        assert placa.size == (600, 600)


def test_sin_un_modelo_de_verdad_no_se_toca_el_producto_y_se_avisa(tmp_path):
    """El proveedor local propone el 83,9 % del arte como 'sujeto'.

    Medido contra un arte real de Marcimex. Borrar eso no deja plantilla, deja
    un borron, asi que la silueta solo se intenta con SAM.
    """
    silueta, motivo = campaign_plate._product_mask(_arte(), _SamFalso())
    assert silueta is not None, "con SAM si debe recortar"

    class _Local:
        name = "opencv-local"

    silueta, motivo = campaign_plate._product_mask(_arte(), _Local())
    assert silueta is None
    assert "modelo de verdad" in motivo

    hecho = campaign_plate.build_plate(
        _arte(), _manifiesto(), {"logo marci"}, tmp_path / "p.png"
    )
    assert hecho is not None
    _ruta, _motor, avisos = hecho
    assert any("conserva el producto" in aviso for aviso in avisos)


def test_una_silueta_que_se_come_la_escena_se_rechaza():
    """Si lo detectado es la escena y no el producto, no se toca nada."""

    # El 83,9 % medido en el arte real: por encima del tope.
    silueta, motivo = campaign_plate._product_mask(_arte(), _SamFalso(ratio=.84))
    assert silueta is None
    assert "no se distinguió" in motivo or "no cuadra" in motivo

    # Y por debajo del minimo es un adorno, no un producto.
    silueta, motivo = campaign_plate._product_mask(_arte(), _SamFalso(ratio=.01))
    assert silueta is None


def test_con_sam_el_producto_sale_de_la_placa(tmp_path, monkeypatch):
    monkeypatch.setattr(
        campaign_plate, "get_inpainting_provider", lambda *_a, **_k: _sin_motor()
    )
    monkeypatch.setattr(
        "app.providers.get_segmentation_provider", lambda: _SamFalso()
    )
    destino = tmp_path / "placa.png"

    hecho = campaign_plate.build_plate(
        _arte(), _manifiesto(), {"logo marci"}, destino
    )

    assert hecho is not None
    _ruta, _motor, avisos = hecho
    # Con el producto fuera, ya no se avisa de que la plantilla lo conserva.
    assert not any("conserva el producto" in aviso for aviso in avisos)


def _sin_motor():
    from app.providers import ProviderUnavailableError

    class _Nada:
        name = "ninguno"

        def fill(self, *_a, **_k):
            raise ProviderUnavailableError("sin motor en pruebas")

    return _Nada()


def test_si_hay_que_borrar_medio_arte_no_se_genera_placa(tmp_path):
    """Reconstruir medio diseno es inventarlo; mejor el fondo determinista."""

    manifiesto = [
        {"name": "bloque", "visible": True, "bbox": [0, 0, 600, 260]},
        {"name": "otro", "visible": True, "bbox": [0, 270, 600, 520]},
    ]

    assert campaign_plate.build_plate(
        _arte(), manifiesto, set(), tmp_path / "p.png"
    ) is None
