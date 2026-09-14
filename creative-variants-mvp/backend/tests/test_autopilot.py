"""Modo automático: una sola llamada debe dejar variantes listas."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Canvas, Layer, LayerCategory, LayerType, Project, SourceImage
from app.models.schemas import SUPPORTED_FORMATS
from app.services import autopilot

from .conftest import await_task, create_manual_layers


def _project_with_canvas(width: int, height: int) -> Project:
    return Project(
        project_id="00000000-0000-4000-8000-000000000000",
        name="test",
        canvas=Canvas(width=width, height=height),
        source=SourceImage(
            path="original/a.png",
            width=width,
            height=height,
            format="PNG",
            original_filename="a.png",
            bytes=1,
        ),
    )


def test_auto_formats_uses_native_aspect_first():
    # Un banner 1200x400 debe generar en su propia proporción antes que nada.
    formats = autopilot.auto_formats(_project_with_canvas(1200, 400))
    assert formats[0] == "1200x400"

    vertical = autopilot.auto_formats(_project_with_canvas(1080, 1920))
    assert vertical[0] == "1080x1920"
    # Sin repetir el nativo entre los formatos de redes.
    assert len(vertical) == len(set(vertical))


def test_un_arte_plano_no_promete_formatos_que_no_puede_llenar():
    """Un banner sin capas metido en un cuadrado es el arte al 33% y el resto relleno.

    Es lo que producía doce piezas inservibles de un KV de 1920x325: se ofrecían
    los formatos de redes pasara lo que pasara.
    """
    banner = autopilot.auto_formats(_project_with_canvas(1920, 325))
    assert banner == ["970x250"] or all(
        SUPPORTED_FORMATS[fmt][0] / SUPPORTED_FORMATS[fmt][1] > 2.5 for fmt in banner
    )
    assert "1080x1350" not in banner and "1080x1080" not in banner


def test_un_arte_por_capas_si_puede_recomponerse_a_cualquier_proporcion():
    """Con capas el motor recoloca, así que no hay proporción prohibida."""
    project = _project_with_canvas(1920, 325)
    project.layers = [
        Layer(name=f"Capa {i}", type=LayerType.IMAGE, category=LayerCategory.PRODUCT,
              src=f"layers/{i}.png", width=200, height=200)
        for i in range(3)
    ]
    formats = autopilot.auto_formats(project)
    assert "1080x1080" in formats and "1080x1350" in formats


def test_auto_generates_from_scratch(client: TestClient, project: dict):
    """Sin análisis previo ni capas: el endpoint debe hacerlo todo."""
    response = client.post(
        f"/projects/{project['project_id']}/auto", json={"count": 4}
    )
    payload = await_task(client, project["project_id"], response)

    names = [step["name"] for step in payload["steps"]]
    assert names == [
        "Detectar elementos",
        "Recortar elementos",
        "Preparar el fondo",
        "Componer variantes",
    ]
    # `count` son propuestas POR FORMATO. El modo automático elige las medidas
    # (la nativa más las de redes que el arte pueda llenar) y cada una recibe
    # las cuatro: pedir cuatro y recibir cuatro repartidas entre tres tamaños
    # era lo que dejaba formatos con una sola pieza.
    por_formato: dict[str, int] = {}
    for variant in payload["variants"]:
        por_formato[variant["format"]] = por_formato.get(variant["format"], 0) + 1
    assert por_formato and set(por_formato.values()) == {4}
    # El fondo quedó reconstruido y las variantes tienen imagen en disco.
    stored = client.get(f"/projects/{project['project_id']}").json()
    assert stored["background"]["path"]
    for variant in payload["variants"]:
        assert client.get(
            f"/projects/{project['project_id']}/files/{variant['image']}"
        ).status_code == 200


def test_auto_respects_explicit_formats_and_reuses_layers(
    client: TestClient, project: dict
):
    """Con capas ya listas no se vuelve a detectar, y se honran los formatos pedidos."""
    create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"count": 6, "formats": ["1080x1350"], "intensity": "conservative"},
    )
    payload = await_task(client, project["project_id"], response)

    detect = next(step for step in payload["steps"] if step["name"] == "Detectar elementos")
    assert "ya estaban listos" in detect["detail"]
    assert {variant["format"] for variant in payload["variants"]} == {"1080x1350"}
    assert all(variant["width"] == 1080 for variant in payload["variants"])
    assert len(payload["variants"]) == 6


def test_auto_returns_every_selected_format_with_its_own_proposals(
    client: TestClient, project: dict
):
    """Cada medida elegida recibe las propuestas pedidas, no una parte de ellas."""
    create_manual_layers(client, project["project_id"])
    requested = [
        "meta_feed_4_5",
        "google_search_landscape",
        "youtube_video_vertical",
    ]
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"count": 2, "formats": requested, "intensity": "conservative"},
    )
    payload = await_task(client, project["project_id"], response)

    por_formato: dict[str, int] = {}
    for variant in payload["variants"]:
        por_formato[variant["format"]] = por_formato.get(variant["format"], 0) + 1
    assert set(por_formato) == set(requested)
    assert set(por_formato.values()) == {2}
    assert len(payload["variants"]) == 2 * len(requested)


def test_template_mode_keeps_the_design_but_honours_the_chosen_formats(
    client: TestClient, project: dict
):
    """Conservar el diseño no es conservar el lienzo.

    El flujo de catálogo entrega un arte fiel, no propuestas recompuestas: una
    sola composición por medida, sin instrucciones ni fondo nuevo. Pero las
    medidas las elige el usuario, y forzar ahí el tamaño nativo era devolver una
    pieza de 1080x1080 a quien había pedido tres formatos distintos.
    """
    create_manual_layers(client, project["project_id"])
    pedidos = ["1080x1350", "1080x1080", "1920x1080"]
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={
            "count": 6,
            "formats": pedidos,
            "intensity": "creative",
            "instruction": "mover todo y cambiar el fondo",
            "template_mode": True,
            "regenerate_background": True,
        },
    )
    payload = await_task(client, project["project_id"], response)

    # Una por formato, las tres pedidas, ninguna de más.
    assert [variant["format"] for variant in payload["variants"]] == pedidos
    for variant in payload["variants"]:
        # El diseño del arte manda: o se reproduce tal cual, o se recompone con
        # la retícula del propio arte. Nunca una familia genérica.
        assert variant["layout"] in {"faithful", "source_flow"}
        assert variant["intensity"] == "conservative"
    # Y se dice por qué se entregó una sola por medida en vez de las seis.
    assert any("una por formato" in aviso for aviso in payload["warnings"])


def test_template_mode_without_formats_stays_at_the_native_size(
    client: TestClient, project: dict
):
    """Sin medidas elegidas, la sustitución fiel se queda en el tamaño del KV."""
    create_manual_layers(client, project["project_id"])
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"count": 1, "template_mode": True},
    )
    payload = await_task(client, project["project_id"], response)

    assert len(payload["variants"]) == 1
    assert payload["variants"][0]["format"] == "1080x1080"
    assert payload["variants"][0]["layout"] == "faithful"


def test_auto_rejects_unknown_format(client: TestClient, project: dict):
    response = client.post(
        f"/projects/{project['project_id']}/auto", json={"formats": ["5000x5000"]}
    )
    assert response.status_code == 422


def test_auto_rejects_unknown_format_mixed_with_valid_one(
    client: TestClient, project: dict
):
    response = client.post(
        f"/projects/{project['project_id']}/auto",
        json={"formats": ["meta_feed_4_5", "formato_inventado"]},
    )
    assert response.status_code == 422
