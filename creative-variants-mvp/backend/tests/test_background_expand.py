"""Extender el fondo al lienzo del formato, sin tocar los píxeles del arte."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.models import BackgroundInfo, Canvas, Project, SourceImage, utcnow
from app.services import background_expand, storage

#: Un banner: para llevarlo a un lienzo alto habría que inventar el 86%.
BANNER = (1920, 325)
#: Un KV cuadrado a story: el trabajo más común, y donde extender sí sirve.
CUADRADO = (1080, 1080)
STORY = (1080, 1920)


def _arte(size: tuple[int, int]) -> Image.Image:
    """Fondo de un tono con algo de diseño, para poder medir la costura."""
    ancho, alto = size
    img = Image.new("RGB", size, (236, 240, 248))
    dibujo = ImageDraw.Draw(img)
    dibujo.rectangle([0, 0, ancho // 3, alto], fill=(28, 46, 122))
    dibujo.rounded_rectangle(
        [ancho // 2, alto // 5, int(ancho * 0.9), int(alto * 0.8)],
        radius=16, fill=(120, 190, 150),
    )
    return img


def _proyecto_de(tmp_path: Path, monkeypatch, size: tuple[int, int]) -> Project:
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path, raising=False)
    project = Project(
        name="KV",
        canvas=Canvas(width=size[0], height=size[1]),
        source=SourceImage(path="original/a.png", width=size[0], height=size[1],
                           format="PNG", original_filename="a.png", bytes=10),
    )
    destino = storage.abs_path(project.project_id, project.source.path)
    destino.parent.mkdir(parents=True, exist_ok=True)
    _arte(size).save(destino)
    return project


@pytest.fixture
def proyecto(tmp_path: Path, monkeypatch) -> Project:
    return _proyecto_de(tmp_path, monkeypatch, CUADRADO)


@pytest.fixture
def proyecto_banner(tmp_path: Path, monkeypatch) -> Project:
    return _proyecto_de(tmp_path, monkeypatch, BANNER)


class _RellenoFalso:
    """El proveedor de máscara, sin red. Deja constancia de lo que le pidieron."""

    name = "magnific"
    model_id = "ideogram-image-edit"

    def __init__(self) -> None:
        self.llamadas: list[tuple[str, str, str | None]] = []
        #: Copia de lo que recibió: los temporales se borran al terminar.
        self.recibido: list[Image.Image] = []
        self.relleno: tuple[int, int, int] | None = None

    def fill(self, image_path, mask_path, prompt=None, output_path=None):
        self.llamadas.append((image_path, mask_path, prompt))
        with Image.open(image_path) as base:
            pintado = base.convert("RGB").copy()
        self.recibido.append(pintado.copy())
        mask = np.asarray(Image.open(mask_path).convert("L"))
        pixeles = np.asarray(pintado).copy()
        # Un fondo liso del color del arte: lo que se le pide y lo que la
        # comprobación acepta. El relleno queda marcado en el canal azul para
        # poder distinguir después qué se pintó y qué no.
        dentro = pixeles[mask <= 24]
        tono = dentro.reshape(-1, 3).mean(axis=0).astype(np.int16) if dentro.size else np.array([128, 128, 128])
        tono[2] = min(255, int(tono[2]) + 9)
        pixeles[mask > 127] = tono.astype(np.uint8)
        self.relleno = tuple(int(v) for v in tono)
        Image.fromarray(pixeles).save(output_path, format="PNG")
        return output_path


def test_extiende_el_fondo_y_no_toca_el_arte(proyecto, monkeypatch):
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)

    rel, avisos = background_expand.expand(proyecto, *STORY)
    assert rel is not None and avisos == []
    assert len(falso.llamadas) == 1
    assert "sin productos" in (falso.llamadas[0][2] or "")

    with Image.open(storage.abs_path(proyecto.project_id, rel)) as salida:
        assert salida.size == STORY
        pixeles = np.asarray(salida.convert("RGB"))
    # El centro es el arte y no se tocó; arriba y abajo es lo generado.
    assert falso.relleno is not None
    assert tuple(pixeles[960, 540]) != falso.relleno, "se repintó el arte"
    assert tuple(pixeles[10, 540]) == falso.relleno
    assert tuple(pixeles[1910, 540]) == falso.relleno


def test_se_guarda_y_no_se_paga_dos_veces(proyecto, monkeypatch):
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    primero, _ = background_expand.expand(proyecto, *STORY)
    segundo, _ = background_expand.expand(proyecto, *STORY)
    assert primero == segundo
    assert len(falso.llamadas) == 1, "la segunda vez sale del disco"
    assert background_expand.cached(proyecto, *STORY) == primero


@pytest.mark.parametrize("lienzo", [(728, 90), (970, 90), (320, 50)])
def test_donde_la_plancha_llena_el_lienzo_no_se_gasta_nada(proyecto_banner, monkeypatch, lienzo):
    """Reducir el arte tiene píxeles de sobra: inventar fondo ahí es pagar por nada."""
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    rel, avisos = background_expand.expand(proyecto_banner, *lienzo)
    assert rel is None and avisos == []
    assert falso.llamadas == []


def test_lo_que_habria_que_inventar_casi_entero_no_se_pide(proyecto_banner, monkeypatch):
    """Medido: pedir el 86% de un 1080x1350 devolvía una ilustración con casas.

    A partir de cierto punto el modelo no rellena, compone, y lo que compone no
    es un fondo: se pelea con el copy. La plancha difuminada es peor de mirar
    pero sigue siendo el arte.
    """
    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    rel, avisos = background_expand.expand(proyecto_banner, 1080, 1350)
    assert rel is None and falso.llamadas == []


def test_sin_modelo_de_imagen_no_se_inventa_nada(proyecto, monkeypatch):
    class _OpenCV:
        name = "opencv"

        def fill(self, *a, **k):  # pragma: no cover - no debería llamarse
            raise AssertionError("OpenCV no sirve para extender una franja entera")

    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: _OpenCV())
    rel, avisos = background_expand.expand(proyecto, *STORY)
    assert rel is None and avisos == []


def test_si_el_modelo_falla_se_dice_y_se_sigue(proyecto, monkeypatch):
    class _Roto:
        name = "magnific"
        model_id = "ideogram-image-edit"

        def fill(self, *a, **k):
            raise RuntimeError("503")

    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: _Roto())
    rel, avisos = background_expand.expand(proyecto, *STORY)
    assert rel is None
    assert any("no se pudo extender el fondo" in a.lower() for a in avisos)
    assert background_expand.cached(proyecto, *STORY) is None


def test_el_fondo_reconstruido_manda_sobre_el_arte(proyecto, monkeypatch):
    """Si ya hay plancha limpia, se extiende esa y no el arte con productos."""
    rel_fondo = "backgrounds/background.png"
    destino = storage.abs_path(proyecto.project_id, rel_fondo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", CUADRADO, (10, 20, 30)).save(destino)
    proyecto.background = BackgroundInfo(path=rel_fondo, provider="opencv", generated_at=utcnow())

    falso = _RellenoFalso()
    monkeypatch.setattr(background_expand, "get_inpainting_provider", lambda *a, **k: falso)
    background_expand.expand(proyecto, *STORY)
    assert tuple(np.asarray(falso.recibido[0])[960, 540]) == (10, 20, 30)


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
    Image.new("RGB", CUADRADO, (10, 20, 30)).save(destino)
    proyecto.background = BackgroundInfo(path=rel_fondo, provider="opencv", generated_at=utcnow())

    rel, avisos = be.expand(proyecto, *STORY)
    assert rel is not None and avisos == []
    assert len(llamadas) == 1


def test_un_modelo_que_no_existe_no_cambia_nada(proyecto, monkeypatch):
    from app.services import background_expand as be

    monkeypatch.setattr(be.settings, "magnific_expand_model", "modelo-inventado")
    assert be._scene_provider(None, plancha_limpia=True) is None
