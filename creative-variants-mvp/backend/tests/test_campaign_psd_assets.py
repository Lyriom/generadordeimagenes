"""Capas fijas de PSD: evidencia segura que sí llega a la plantilla."""
from __future__ import annotations

import uuid
from pathlib import Path

from PIL import Image

from app.models.campaign import (
    Campaign,
    CampaignSource,
    CampaignSourceKind,
    TemplateBlueprint,
    TemplateCandidate,
    TemplateSlotProposal,
)
from app.models.formats import DEFAULT_SAFE_AREA
from app.services.campaign_creative import _render
from app.services.campaign_ingestion import _extract_psd
from app.services import campaign_store
from tests.psd_fixture import sample_kv


class _FakeLayer:
    def __init__(
        self,
        name: str,
        bbox: tuple[int, int, int, int],
        image: Image.Image,
        *,
        kind: str = "pixel",
        visible: bool = True,
        group: bool = False,
        text: str = "",
    ) -> None:
        self.name = name
        self.bbox = bbox
        self._image = image
        self.kind = kind
        self.visible = visible
        self._group = group
        self.text = text

    def is_group(self) -> bool:
        return self._group

    def composite(self) -> Image.Image:
        return self._image.copy()


class _FakePSD:
    size = (1000, 1000)

    def __init__(self, layers: list[_FakeLayer]) -> None:
        self._layers = layers

    def composite(self) -> Image.Image:
        return Image.new("RGBA", self.size, (12, 34, 56, 255))

    def descendants(self) -> list[_FakeLayer]:
        return self._layers


def _source(client_id: str, campaign_id: str, source_id: str) -> CampaignSource:
    return CampaignSource(
        source_id=source_id,
        filename="master.psd",
        kind=CampaignSourceKind.LAYERED_DESIGN,
        extension=".psd",
        size_bytes=8,
        sha256="a" * 64,
        stored_path=f"sources/{source_id}/original.psd",
    )


def test_psd_fixed_layers_are_extracted_and_composited_without_variable_content(
    monkeypatch, tmp_path: Path
):
    client_id, campaign_id, source_id = (str(uuid.uuid4()) for _ in range(3))
    source = _source(client_id, campaign_id, source_id)
    document = _FakePSD(
        [
            _FakeLayer(
                "Fondo campaña", (0, 0, 1000, 1000),
                Image.new("RGBA", (1000, 1000), (12, 34, 56, 255)),
            ),
            _FakeLayer(
                "Decoración esquina", (800, 800, 1000, 1000),
                Image.new("RGBA", (200, 200), (0, 220, 110, 255)),
            ),
            _FakeLayer(
                "Producto TV", (200, 200, 600, 600),
                Image.new("RGBA", (400, 400), (230, 30, 30, 255)),
            ),
            _FakeLayer(
                "Decoración IG", (40, 40, 120, 120),
                Image.new("RGBA", (80, 80), (150, 20, 250, 255)),
            ),
            _FakeLayer(
                "Titular de oferta", (100, 90, 900, 180),
                Image.new("RGBA", (800, 90), (255, 255, 255, 255)),
                kind="type",
                text="Oferta real que no debe congelarse",
            ),
            _FakeLayer(
                "Logo de marca", (40, 40, 180, 100),
                Image.new("RGBA", (140, 60), (250, 210, 20, 255)),
            ),
        ]
    )
    monkeypatch.setattr("psd_tools.PSDImage.open", staticmethod(lambda _path: document))
    path = tmp_path / "master.psd"
    path.write_bytes(b"8BPS\x00\x01")

    _extract_psd(client_id, campaign_id, path, source)

    assets = source.meta["layer_assets"]
    assert {asset["role"] for asset in assets} == {
        "logo", "fixed_background", "fixed_decoration"
    }
    assert {asset["name"] for asset in assets}.isdisjoint(
        {"Producto TV", "Decoración IG", "Titular de oferta"}
    )
    fixed = [asset for asset in assets if asset["role"].startswith("fixed_")]
    assert all(asset["bbox"] and asset["source_size"] == [1000, 1000] for asset in fixed)
    assert all(
        campaign_store.campaign_path(client_id, campaign_id, asset["path"]).exists()
        for asset in fixed
    )

    candidate = TemplateCandidate(
        name="Institucional basado en master",
        category="institutional",
        source_ids=[source_id],
        slots=[
            TemplateSlotProposal(key="titular", label="Titular", category="headline"),
        ],
        blueprint=TemplateBlueprint(background_style="campaign", accent_style="minimal"),
    )
    campaign = Campaign(client_id=client_id, campaign_id=campaign_id, name="Campaña", sources=[source])
    final, layers = _render(
        campaign,
        "Marca",
        candidate,
        width=500,
        height=500,
        safe=dict(DEFAULT_SAFE_AREA),
        proposal=1,
    )
    try:
        # El fondo y la decoración siguen su bbox normalizado al formato nuevo.
        assert final.getpixel((250, 250))[:3] == (12, 34, 56)
        assert final.getpixel((450, 450))[:3] == (0, 220, 110)
        assert any("PSD fijo" in name for name, _layer in layers)
        # Una candidata institucional sin slot de producto no dibuja el
        # placeholder artificial de PRODUCTO.
        assert not any(name.startswith("Producto") for name, _layer in layers)
    finally:
        final.close()
        for _name, layer in layers:
            layer.close()


def test_real_psd_color_fill_is_preserved_as_a_fixed_background(tmp_path: Path):
    """Los nombres estándar de Photoshop también deben conservar la identidad.

    ``Relleno de color`` es el nombre habitual que dejan los archivos reales,
    no una convención inventada por nuestra prueba. Se valida con un PSD válido
    de capas reales, no con el doble de ``PSDImage`` de la prueba anterior.
    """

    client_id, campaign_id, source_id = (str(uuid.uuid4()) for _ in range(3))
    source = _source(client_id, campaign_id, source_id)
    path = sample_kv(tmp_path / "master-real.psd", width=480, height=360)

    _extract_psd(client_id, campaign_id, path, source)

    assets = source.meta["layer_assets"]
    by_role = {asset["role"]: asset for asset in assets}
    assert set(by_role) == {"fixed_background", "logo"}
    background = by_role["fixed_background"]
    assert background["name"] == "Relleno de color 1"
    assert background["bbox"] == [0, 0, 480, 360]
    with Image.open(
        campaign_store.campaign_path(client_id, campaign_id, background["path"])
    ) as rendered:
        assert rendered.mode == "RGBA"
        assert rendered.size == (480, 360)
        assert rendered.convert("RGB").getpixel((10, 10)) == (14, 30, 60)

    candidate = TemplateCandidate(
        name="Mensaje de marca real",
        category="institutional",
        source_ids=[source_id],
        slots=[
            TemplateSlotProposal(key="titular", label="Titular", category="headline"),
        ],
        blueprint=TemplateBlueprint(background_style="campaign", accent_style="minimal"),
    )
    campaign = Campaign(
        client_id=client_id,
        campaign_id=campaign_id,
        name="Campaña",
        sources=[source],
    )
    final, layers = _render(
        campaign,
        "Marca",
        candidate,
        width=480,
        height=360,
        safe=dict(DEFAULT_SAFE_AREA),
        proposal=1,
    )
    try:
        assert final.getpixel((240, 180))[:3] == (14, 30, 60)
        assert any("PSD fijo" in name for name, _layer in layers)
    finally:
        final.close()
        for _name, layer in layers:
            layer.close()


def test_psd_logo_group_is_preserved_instead_of_becoming_a_wordmark(
    monkeypatch, tmp_path: Path
):
    """Un logo suele ser un grupo, no una sola capa raster de Photoshop."""

    client_id, campaign_id, source_id = (str(uuid.uuid4()) for _ in range(3))
    source = _source(client_id, campaign_id, source_id)
    document = _FakePSD(
        [
            _FakeLayer(
                "Fondo", (0, 0, 1000, 1000),
                Image.new("RGBA", (1000, 1000), (18, 28, 54, 255)),
            ),
            _FakeLayer(
                "Marca gráfica", (40, 40, 280, 140),
                Image.new("RGBA", (240, 100), (241, 201, 33, 255)), group=True,
            ),
        ]
    )
    monkeypatch.setattr("psd_tools.PSDImage.open", staticmethod(lambda _path: document))
    path = tmp_path / "master.psd"
    path.write_bytes(b"8BPS\x00\x01")

    _extract_psd(client_id, campaign_id, path, source)

    logos = [item for item in source.meta["layer_assets"] if item["role"] == "logo"]
    assert len(logos) == 1
    assert logos[0]["name"] == "Marca gráfica"


def test_un_sufijo_copy_de_photoshop_no_descarta_un_logotipo_real():
    """"copy" es como Photoshop bautiza un duplicado, no el copy publicitario.

    Medido en un PSD de Cyber real: la capa ``logo copy`` es un logotipo, y se
    descartaba porque "copy" estaba en la lista de contenido variable. Su gemela
    ``logo cece copia`` si se conservaba, solo porque el sufijo estaba en
    español. Esa asimetria delataba el fallo.
    """
    from app.services.campaign_ingestion import _psd_fixed_asset_role

    assert _psd_fixed_asset_role("logo copy", "smartobject", False) == "logo"
    assert _psd_fixed_asset_role("logo cece copia", "group", True) == "logo"
    assert _psd_fixed_asset_role("Fondo copy", "shape", False) == "fixed_background"
    # Y el copy publicitario de verdad sigue siendo variable: es texto.
    assert _psd_fixed_asset_role("Copy principal", "type", False) is None
    assert _psd_fixed_asset_role("titular copy", "shape", False) is None


def test_un_grupo_no_se_congela_aunque_se_llame_fondo():
    """Medido en un KV real: el grupo "BG" traia foto, logo y legal dentro.

    Componerlo horneaba el producto en todas las piezas y duplicaba el logo. El
    nombre de un grupo describe su intencion, no su contenido.
    """
    from app.services.campaign_ingestion import _psd_fixed_asset_role

    assert _psd_fixed_asset_role("BG", "group", True) is None
    assert _psd_fixed_asset_role("fondo", "group", True) is None
    # Una capa suelta con ese nombre si es fondo.
    assert _psd_fixed_asset_role("fondo", "shape", False) == "fixed_background"


def test_las_capas_se_miden_contra_su_artboard_y_no_contra_el_documento():
    """Un PSD de agencia trae varias piezas en el mismo lienzo.

    En el KV de muebles conviven cuatro artboards (post 1080x1080 y story
    1080x1920) dentro de 2235x3100. Midiendo contra el documento, el logo de la
    portada aterrizaba en un cuadrante diminuto de la plantilla.
    """
    from app.services.campaign_ingestion import _psd_artboard_frame, _psd_clip_to_frame

    class _Falsa:
        def __init__(self, kind, bbox, parent=None):
            self.kind, self.bbox, self.parent = kind, bbox, parent

    artboard = _Falsa("artboard", (0, 1180, 1080, 3100))
    grupo = _Falsa("group", (0, 1180, 1080, 3100), artboard)
    capa = _Falsa("smartobject", (320, 1363, 761, 1410), grupo)

    marco = _psd_artboard_frame(capa)
    assert marco == ((0, 1180, 1080, 3100), (1080, 1920))
    # Recortada al artboard y trasladada a su origen.
    recorte = _psd_clip_to_frame(capa.bbox, marco[0])
    assert recorte == (320, 1363, 761, 1410)
    relativa = (
        recorte[0] - marco[0][0], recorte[1] - marco[0][1],
        recorte[2] - marco[0][0], recorte[3] - marco[0][1],
    )
    assert relativa == (320, 183, 761, 230)

    # Sin artboard, el marco es el documento entero.
    assert _psd_artboard_frame(_Falsa("smartobject", (0, 0, 10, 10))) is None
