"""Extender el fondo al lienzo del formato, sin tocar los píxeles del arte."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.models import BackgroundInfo, Canvas, Project, SourceImage, utcnow
from app.services import background_expand, storage

BANNER = (1920, 325)


def _banner() -> Image.Image:
    img = Image.new("RGB", BANNER, (236, 240, 248))
    dibujo = ImageDraw.Draw(img)
    dibujo.rectangle([0, 0, 700, 325], fill=(28, 46, 122))
    dibujo.rounded_rectangle([800, 55, 1180, 285], radius=16, fill=(120, 190, 150))
    return img


@pytest.fixture
def proyecto(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path, raising=False)
    project = Project(
        name="Banner",
        canvas=Canvas(width=BANNER[0], height=BANNER[1]),
        source=SourceImage(path="original/a.png", width=BANNER[0], height=BANNER[1],
                           format="PNG", original_filename="a.png", bytes=10),
    )
    destino = storage.abs_path(project.project_id, project.source.path)
    destino.parent.mkdir(parents=True, exist_ok=True)
    _banner().save(destino)
    return project


class _RellenoFalso:
    """El proveedor de máscara, sin red. Deja constancia de lo que le pidieron."""

    name = "magnific"
    model_id = "ideogram-image-edit"

    def __init__(self) -> None:
        self.llamadas: list[tuple[str, str, str | None]] = []
        #: Copia de lo que recibió: los temporales se borran al terminar.
        self.recibido: list[Image.Image] = []

    def fill(self, image_path, mask_path, prompt=None, output_path=None):
        self.llamadas.append((image_path, mask_path, prompt))
        with Image.open(image_path) as base:
            pintado = base.convert("RGB").copy()
        self.recibido.append(pintado.copy())
        mask = np.asarray(Image.open(mask_path).convert("L"))
        # Un relleno reconocible solo donde la máscara lo permite.
        pixeles = np.asarray(pintado).copy()
        pixeles[mask > 127] = (255, 0, 0)
        Image.fromarray(pixeles).save(output_path, format="PNG")
        return output_path


def test_extiende_el_fondo_y_no_toca_el_arte(proyecto, monkeypatch):
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)

    rel, avisos = background_expand.expand(proyecto, 1080, 1350)
    assert rel is not None and avisos == []
    assert len(falso.llamadas) == 1
    assert "No dibujes productos" in (falso.llamadas[0][2] or "")

    with Image.open(storage.abs_path(proyecto.project_id, rel)) as salida:
        assert salida.size == (1080, 1350)
        pixeles = np.asarray(salida.convert("RGB"))
    # El centro es el arte: el banner cabe a lo ancho y queda una franja central.
    assert tuple(pixeles[675, 540]) != (255, 0, 0), "se repintó el arte"
    # Y arriba y abajo es lo generado.
    assert tuple(pixeles[10, 540]) == (255, 0, 0)
    assert tuple(pixeles[1340, 540]) == (255, 0, 0)


def test_se_guarda_y_no_se_paga_dos_veces(proyecto, monkeypatch):
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    primero, _ = background_expand.expand(proyecto, 1080, 1350)
    segundo, _ = background_expand.expand(proyecto, 1080, 1350)
    assert primero == segundo
    assert len(falso.llamadas) == 1, "la segunda vez sale del disco"
    assert background_expand.cached(proyecto, 1080, 1350) == primero


@pytest.mark.parametrize("lienzo", [(728, 90), (970, 90), (320, 50)])
def test_donde_la_plancha_llena_el_lienzo_no_se_gasta_nada(proyecto, monkeypatch, lienzo):
    """Reducir el arte tiene píxeles de sobra: inventar fondo ahí es pagar por nada."""
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    rel, avisos = background_expand.expand(proyecto, *lienzo)
    assert rel is None and avisos == []
    assert falso.llamadas == []


def test_sin_modelo_de_imagen_no_se_inventa_nada(proyecto, monkeypatch):
    class _OpenCV:
        name = "opencv"

        def fill(self, *a, **k):  # pragma: no cover - no debería llamarse
            raise AssertionError("OpenCV no sirve para extender una franja entera")

    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: _OpenCV())
    rel, avisos = background_expand.expand(proyecto, 1080, 1350)
    assert rel is None and avisos == []


def test_si_el_modelo_falla_se_dice_y_se_sigue(proyecto, monkeypatch):
    class _Roto:
        name = "magnific"
        model_id = "ideogram-image-edit"

        def fill(self, *a, **k):
            raise RuntimeError("503")

    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: _Roto())
    rel, avisos = background_expand.expand(proyecto, 1080, 1350)
    assert rel is None
    assert any("no se pudo extender el fondo" in a.lower() for a in avisos)
    assert background_expand.cached(proyecto, 1080, 1350) is None


def test_el_fondo_reconstruido_manda_sobre_el_arte(proyecto, monkeypatch):
    """Si ya hay plancha limpia, se extiende esa y no el arte con productos."""
    rel_fondo = "backgrounds/background.png"
    destino = storage.abs_path(proyecto.project_id, rel_fondo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", BANNER, (10, 20, 30)).save(destino)
    proyecto.background = BackgroundInfo(path=rel_fondo, provider="opencv", generated_at=utcnow())

    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    background_expand.expand(proyecto, 1080, 1350)
    assert tuple(np.asarray(falso.recibido[0])[675, 540]) == (10, 20, 30)


def test_la_frontera_es_la_ampliacion_no_la_proporcion():
    """Lo que decide no es la forma del formato, es si quedan píxeles.

    Un 1200x628 no es una tira, pero sacarlo de un banner de 325 px de alto
    obliga a ampliar casi dos veces: ahí sí hay que extender. Un 970x90, mucho
    más raro de proporción, se resuelve reduciendo.
    """
    assert background_expand.cover_upscale(BANNER, 970, 90) < background_expand.SHARP_UPSCALE
    assert background_expand.cover_upscale(BANNER, 1200, 628) > background_expand.SHARP_UPSCALE
    assert background_expand.cover_upscale(BANNER, 1080, 1350) > 4.0


def test_el_modelo_sin_mascara_solo_con_plancha_limpia(proyecto, monkeypatch):
    """Gemini 2.5 regenera la imagen entera: sobre el arte redibujaría el copy.

    Se le permite solo cuando lo que se extiende es el fondo ya reconstruido,
    donde no hay copy, precios ni logos que pueda estropear.
    """
    from app.services import background_expand as be

    monkeypatch.setattr(be.settings, "magnific_expand_model", "gemini-2-5-flash-image-preview")
    assert be._scene_provider(None, plancha_limpia=False) is None

    llamadas: list[str] = []

    class _Escena:
        model_id = "gemini-2-5-flash-image-preview"
        name = "magnific-scene"

        def available(self) -> bool:
            return True

        def empty(self, image_path, output_path=None, prompt=None):
            llamadas.append(image_path)
            with Image.open(image_path) as base:
                base.convert("RGB").save(output_path, format="PNG")
            return output_path

    monkeypatch.setattr(be, "MagnificSceneProvider", lambda model=None: _Escena())
    monkeypatch.setattr(
        be, "get_inpainting_provider",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debía usar la máscara")),
    )
    rel_fondo = "backgrounds/background.png"
    destino = storage.abs_path(proyecto.project_id, rel_fondo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", BANNER, (10, 20, 30)).save(destino)
    proyecto.background = BackgroundInfo(path=rel_fondo, provider="opencv", generated_at=utcnow())

    rel, avisos = be.expand(proyecto, 1080, 1350)
    assert rel is not None and avisos == []
    assert len(llamadas) == 1


def test_un_modelo_que_no_existe_no_cambia_nada(proyecto, monkeypatch):
    from app.services import background_expand as be

    monkeypatch.setattr(be.settings, "magnific_expand_model", "modelo-inventado")
    assert be._scene_provider(None, plancha_limpia=True) is None
