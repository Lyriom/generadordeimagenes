"""Reconocer el producto sin que el usuario tenga que nombrar los archivos.

Ninguna prueba sale a internet: el transporte HTTP se simula, como en Magnific.
"""
from __future__ import annotations

import json

import httpx
import pytest
from PIL import Image

from app.config import settings
from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.providers.base import ProviderUnavailableError
from app.providers.openai_vision import OpenAIVisionProvider
from app.services import product_identity, product_scale


def _project(*layers: Layer) -> Project:
    project = Project(
        name="KV",
        canvas=Canvas(width=1080, height=1920),
        source=SourceImage(
            path="original/a.png",
            width=1080,
            height=1920,
            format="PNG",
            original_filename="a.png",
            bytes=10,
        ),
    )
    project.layers = list(layers)
    return project


def _producto(nombre: str, archivo: str | None = None) -> Layer:
    return Layer(
        name=nombre,
        type=LayerType.IMAGE,
        category=LayerCategory.PRODUCT,
        src="layers/p.png",
        width=400,
        height=700,
        meta={"replaced_from": archivo} if archivo else {},
    )


def _titular(texto: str) -> Layer:
    return Layer(
        name="Titular",
        type=LayerType.TEXT,
        category=LayerCategory.HEADLINE,
        content=texto,
        width=600,
        height=90,
    )


def _foto(tmp_path, nombre="recorte.png"):
    ruta = tmp_path / nombre
    Image.new("RGBA", (500, 900), (240, 182, 28, 255)).save(ruta)
    return ruta


def _responde(monkeypatch, contenido: str, enviados: list | None = None, status=200):
    """Sustituye httpx.Client por uno que contesta lo que le digan."""
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if enviados is not None:
            enviados.append(json.loads(request.content.decode()))
        if status >= 400:
            return httpx.Response(status, text="fallo simulado")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": contenido}}]}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------------- el proveedor
def test_el_proveedor_devuelve_la_familia_que_eligio(monkeypatch, tmp_path):
    _responde(monkeypatch, "cilindro_gas")
    provider = OpenAIVisionProvider(api_key="clave")
    assert provider.identify(_foto(tmp_path), list(product_scale.families())) == "cilindro_gas"


def test_lo_que_no_identifica_una_familia_es_no_lo_se(monkeypatch, tmp_path):
    """La respuesta o resuelve a una familia conocida, o no vale. Nunca texto libre."""
    provider = OpenAIVisionProvider(api_key="clave")
    for respuesta in ("desconocido", "no lo sé", "un electrodoméstico blanco", ""):
        _responde(monkeypatch, respuesta)
        assert provider.identify(_foto(tmp_path), list(product_scale.families())) is None


def test_pregunta_con_la_foto_y_con_la_lista_cerrada(monkeypatch, tmp_path):
    enviados: list = []
    _responde(monkeypatch, "cocina", enviados)
    OpenAIVisionProvider(api_key="clave").identify(
        _foto(tmp_path), list(product_scale.families())
    )
    contenido = enviados[0]["messages"][0]["content"]
    texto = next(part["text"] for part in contenido if part["type"] == "text")
    imagen = next(part for part in contenido if part["type"] == "image_url")
    assert "cilindro_gas" in texto and "cocina" in texto
    assert imagen["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # `detail: low` es lo que hace que la consulta sea barata.
    assert imagen["image_url"]["detail"] == "low"


def test_sin_clave_no_se_intenta(tmp_path):
    provider = OpenAIVisionProvider(api_key=None)
    assert provider.available() is False
    with pytest.raises(ProviderUnavailableError):
        provider.identify(_foto(tmp_path), list(product_scale.families()))


# ------------------------------------------------------------- la cascada
def test_el_nombre_del_archivo_evita_la_consulta(monkeypatch, tmp_path):
    """Gratis y exacto: si el archivo ya lo dice, no se gasta una llamada."""
    llamadas: list = []
    monkeypatch.setattr(
        product_identity,
        "_provider",
        lambda: pytest.fail("no debía preguntar por la imagen"),
    )
    layer = _producto("Producto", "cocina-indurama-20p.png")
    record = product_identity.identify(_project(layer), layer, _foto(tmp_path))
    assert record is not None
    assert record["family_key"] == "cocina"
    assert record["source"] == "nombre"
    assert llamadas == []


def test_mira_la_foto_cuando_el_nombre_no_dice_nada(monkeypatch, tmp_path):
    """El caso que motivó todo esto: `producto1.png`."""
    _responde(monkeypatch, "cilindro_gas")
    monkeypatch.setattr(settings, "openai_api_key", "clave")
    monkeypatch.setattr(settings, "enable_product_vision", True)
    layer = _producto("Producto añadido 2", "producto1.png")
    record = product_identity.identify(_project(layer), layer, _foto(tmp_path))
    assert record is not None
    assert record["family_key"] == "cilindro_gas"
    assert record["source"] == "imagen"
    assert record["height_cm"] == 58.0


def test_lo_guarda_en_la_capa_y_la_tabla_lo_lee(monkeypatch, tmp_path):
    """Se paga una vez: la generación después es local y determinista."""
    _responde(monkeypatch, "cocina")
    monkeypatch.setattr(settings, "openai_api_key", "clave")
    layer = _producto("Producto", "producto1.png")
    product_identity.identify(_project(layer), layer, _foto(tmp_path))
    assert layer.meta["product_size"]["family_key"] == "cocina"
    medida = product_scale.measure_layer(layer)
    assert medida is not None and medida.height_cm == 90.0


def test_el_arte_identifica_cuando_hay_un_solo_producto(monkeypatch, tmp_path):
    """El titular dice qué es y de paso cuántas pulgadas."""
    monkeypatch.setattr(settings, "openai_api_key", None)
    layer = _producto("Producto", "producto1.png")
    project = _project(layer, _titular("COCINA A GAS 4Q 20P CROMA"))
    record = product_identity.identify(project, layer, _foto(tmp_path))
    assert record is not None
    assert record["source"] == "arte"
    assert record["family_key"] == "cocina"
    assert '20"' in record["family"]


def test_con_dos_productos_el_arte_no_decide(monkeypatch, tmp_path):
    """Dos productos y dos familias en el copy: repartirlas al azar sería peor."""
    monkeypatch.setattr(settings, "openai_api_key", None)
    uno = _producto("Producto 1", "producto1.png")
    otro = _producto("Producto 2", "producto2.png")
    project = _project(uno, otro, _titular("COCINA A GAS + CILINDRO GRATIS"))
    assert product_identity.identify(project, uno, _foto(tmp_path)) is None
    assert "product_size" not in uno.meta


def test_si_falla_la_consulta_no_rompe_la_subida(monkeypatch, tmp_path):
    _responde(monkeypatch, "", status=500)
    monkeypatch.setattr(settings, "openai_api_key", "clave")
    layer = _producto("Producto", "producto1.png")
    # Sin excepción: subir un producto no puede depender de que OpenAI conteste.
    assert product_identity.identify(_project(layer), layer, _foto(tmp_path)) is None


def test_se_puede_apagar_el_reconocimiento(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "openai_api_key", "clave")
    monkeypatch.setattr(settings, "enable_product_vision", False)
    _responde(monkeypatch, "cocina")
    layer = _producto("Producto", "producto1.png")
    assert product_identity.identify(_project(layer), layer, _foto(tmp_path)) is None


def test_dice_de_donde_lo_saco(monkeypatch, tmp_path):
    layer = _producto("Producto", "televisor-samsung-55.png")
    product_identity.identify(_project(layer), layer, _foto(tmp_path), use_vision=False)
    frases = product_identity.describe([layer])
    assert len(frases) == 1
    assert "televisor" in frases[0]
    assert "nombre del archivo" in frases[0]


def test_rescata_la_clave_de_una_respuesta_hablada(monkeypatch, tmp_path):
    """Un modelo puede contestar con una frase; tirarla sería perder el acierto."""
    _responde(monkeypatch, "Es una cocina a gas (clave: cocina).")
    provider = OpenAIVisionProvider(api_key="clave")
    assert provider.identify(_foto(tmp_path), list(product_scale.families())) == "cocina"


def test_con_dos_claves_en_la_frase_no_adivina(monkeypatch, tmp_path):
    _responde(monkeypatch, "podría ser cocina o microondas")
    provider = OpenAIVisionProvider(api_key="clave")
    assert provider.identify(_foto(tmp_path), list(product_scale.families())) is None


def test_no_manda_tope_de_tokens(monkeypatch, tmp_path):
    """El nombre del campo cambia según el modelo; mandarlo mal es un 400."""
    enviados: list = []
    _responde(monkeypatch, "cocina", enviados)
    OpenAIVisionProvider(api_key="clave").identify(
        _foto(tmp_path), list(product_scale.families())
    )
    assert "max_tokens" not in enviados[0]
    assert "max_completion_tokens" not in enviados[0]


def test_dice_cuando_miro_y_no_supo(monkeypatch, tmp_path):
    """«Lo miré y no lo reconocí» se arregla con una foto mejor."""
    monkeypatch.setattr(settings, "openai_api_key", "clave")
    _responde(monkeypatch, "desconocido")
    layer = _producto("Producto", "producto1.png")
    motivos: list[str] = []
    product_identity.identify(_project(layer), layer, _foto(tmp_path), diagnostics=motivos)
    assert any("se miró la foto" in m for m in motivos)


def test_dice_cuando_no_pudo_preguntar(monkeypatch, tmp_path):
    """«No pude preguntarle a nadie» se arregla en el servidor. No es lo mismo."""
    monkeypatch.setattr(settings, "openai_api_key", "clave")
    _responde(monkeypatch, "", status=404)
    layer = _producto("Producto", "producto1.png")
    motivos: list[str] = []
    product_identity.identify(_project(layer), layer, _foto(tmp_path), diagnostics=motivos)
    assert any("no se pudo consultar" in m for m in motivos)
    # Con el código dentro, que es lo que dice qué hay que arreglar.
    assert any("404" in m for m in motivos)


def test_dice_cuando_esta_apagado(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "openai_api_key", None)
    layer = _producto("Producto", "producto1.png")
    motivos: list[str] = []
    product_identity.identify(_project(layer), layer, _foto(tmp_path), diagnostics=motivos)
    assert any("apagado o sin clave" in m for m in motivos)
