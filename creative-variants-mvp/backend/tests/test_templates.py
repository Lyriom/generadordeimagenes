"""Etapa 1: la plantilla existe, es correcta y no se la lleva la limpieza.

Lo que se comprueba aquí no es que la API responda 200. Es lo que hacía
imposible tener una biblioteca: que el trabajo de decidir qué cambia en cada
arte se perdía con el proyecto, y que las cajas estaban en píxeles de un lienzo
concreto y no servían para otra medida.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.template import Box
from app.services import storage, template_store
from tests.conftest import create_manual_layers


@pytest.fixture()
def brand(client: TestClient) -> dict:
    response = client.post("/brands", json={"name": "Marcimex"})
    assert response.status_code == 201, response.text
    return response.json()["brand"]


@pytest.fixture()
def kv(client: TestClient, project: dict) -> dict:
    """Un KV con producto, logo, titular, CTA, legal y precio."""
    layers = create_manual_layers(client, project["project_id"])
    precio = client.post(
        f"/projects/{project['project_id']}/layers",
        json={
            "name": "Precio",
            "category": "price",
            "type": "text",
            "x": 40,
            "y": 470,
            "width": 220,
            "height": 60,
            "content": "$399",
            "auto_segment": False,
        },
    )
    assert precio.status_code == 201, precio.text
    layers["price"] = precio.json()
    return {"project": project, "layers": layers}


def _crear_plantilla(client: TestClient, brand: dict, kv: dict, **extra) -> dict:
    response = client.post(
        f"/brands/{brand['brand_id']}/templates/from-project",
        json={"project_id": kv["project"]["project_id"], **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()["template"]


def test_propone_campos_para_lo_que_cambia_y_congela_el_resto(client, brand, kv):
    """Solo el producto es imprescindible; el copy se adapta a lo que venga.

    Una plantilla de campaña no puede obligar a inventar un precio, titular o
    CTA cuando la fila (o el propio brief) no los usa. Esos campos se reservan
    para poder llenarlos, pero si llegan vacíos el render debe omitirlos y
    redistribuir la composición.
    """
    plantilla = _crear_plantilla(client, brand, kv)

    campos = {slot["id"]: slot for slot in plantilla["slots"]}
    assert "producto" in campos
    assert "precio" in campos
    assert campos["producto"]["required"] is True
    for campo in ("precio", "titular", "cta", "legal"):
        assert campos[campo]["required"] is False

    fijas = {layer["category"] for layer in plantilla["fixed_layers"]}
    assert "logo" in fijas
    assert "logo" not in campos


def test_el_producto_es_campo_imagen_y_el_precio_campo_texto(client, brand, kv):
    plantilla = _crear_plantilla(client, brand, kv)
    campos = {slot["id"]: slot for slot in plantilla["slots"]}

    assert campos["producto"]["kind"] == "image"
    # Un campo imagen dice qué resolución mínima necesita cada fila, para poder
    # rechazar un PNG diminuto antes de producir y no después.
    assert campos["producto"]["min_source_px"] > 0

    assert campos["precio"]["kind"] == "text"
    assert campos["precio"]["default"] == "$399"


def test_los_ids_son_slugs_legibles_y_unicos(client, brand, kv):
    """La cabecera de la columna la rellena una persona: nada de uuid."""
    segundo = client.post(
        f"/projects/{kv['project']['project_id']}/layers",
        json={
            "name": "Producto acompañante",
            "category": "product",
            "type": "image",
            "x": 420,
            "y": 300,
            "width": 120,
            "height": 130,
            "auto_segment": True,
        },
    )
    assert segundo.status_code == 201, segundo.text

    plantilla = _crear_plantilla(client, brand, kv)
    ids = [slot["id"] for slot in plantilla["slots"]]

    assert "producto" in ids
    assert "producto_2" in ids
    assert len(ids) == len(set(ids))
    assert all("-" not in slot_id or slot_id.count("-") < 4 for slot_id in ids)


def test_las_cajas_van_en_fracciones_y_valen_para_otra_medida(client, brand, kv):
    """El motivo de no guardar píxeles: la misma decisión en las cinco medidas."""
    plantilla = _crear_plantilla(client, brand, kv)
    producto = next(slot for slot in plantilla["slots"] if slot["id"] == "producto")

    caja = Box.model_validate(producto["box"])
    assert 0.0 <= caja.x <= 1.0 and 0.0 <= caja.y <= 1.0
    assert 0.0 < caja.width <= 1.0 and 0.0 < caja.height <= 1.0

    # La misma caja, resuelta en dos lienzos distintos, cae en la misma
    # proporción del arte.
    x_cuadrado, _, ancho_cuadrado, _ = caja.to_pixels(1080, 1080)
    x_story, _, ancho_story, _ = caja.to_pixels(1080, 1920)
    assert x_cuadrado == x_story
    assert ancho_cuadrado == ancho_story
    assert caja.to_pixels(600, 600)[2] == pytest.approx(
        producto["box"]["width"] * 600, abs=1
    )


def test_la_plantilla_sobrevive_al_barrido_de_proyectos(client, brand, kv):
    """El defecto que esta etapa existe para arreglar.

    Se envejece el proyecto y se corre el barrido de verdad, el mismo que pasa
    a las ocho horas en el servidor. La plantilla tiene que seguir entera: su
    manifiesto, su plancha y los recortes congelados.
    """
    import os
    import time

    plantilla = _crear_plantilla(client, brand, kv)

    viejo = time.time() - 9 * 3600
    manifiesto = storage.project_json_path(kv["project"]["project_id"])
    os.utime(manifiesto, (viejo, viejo))
    borrados = storage.purge_expired_projects(retention_hours=8)
    assert kv["project"]["project_id"] in borrados

    respuesta = client.get(
        f"/brands/{brand['brand_id']}/templates/{plantilla['template_id']}"
    )
    assert respuesta.status_code == 200, respuesta.text
    recuperada = respuesta.json()["template"]
    assert len(recuperada["slots"]) == len(plantilla["slots"])

    base = template_store.template_dir(brand["brand_id"], plantilla["template_id"])
    assert (base / "template.json").exists()
    assert (base / "plate.png").exists()
    congelados = [slot["sample_src"] for slot in recuperada["slots"] if slot["sample_src"]]
    assert congelados, "ningún campo congeló sus píxeles"
    for rel in congelados:
        assert (base / rel).exists(), f"falta el recorte congelado {rel}"


def test_la_plancha_congelada_mide_lo_que_el_lienzo(client, brand, kv):
    from PIL import Image

    plantilla = _crear_plantilla(client, brand, kv)
    base = template_store.template_dir(brand["brand_id"], plantilla["template_id"])
    with Image.open(base / "plate.png") as plate:
        assert plate.size == (
            plantilla["source_canvas"]["width"],
            plantilla["source_canvas"]["height"],
        )

    servido = client.get(
        f"/brands/{brand['brand_id']}/templates/{plantilla['template_id']}/files/plate.png"
    )
    assert servido.status_code == 200
    assert servido.headers["content-type"] == "image/png"


def test_convertir_capa_fija_en_campo_y_volver(client, brand, kv):
    plantilla = _crear_plantilla(client, brand, kv)
    logo = next(item for item in plantilla["fixed_layers"] if item["category"] == "logo")

    respuesta = client.put(
        f"/brands/{brand['brand_id']}/templates/{plantilla['template_id']}",
        json={"promote": [logo["layer_id"]]},
    )
    assert respuesta.status_code == 200, respuesta.text
    ascendida = respuesta.json()["template"]
    assert "logo" in {slot["id"] for slot in ascendida["slots"]}
    assert logo["layer_id"] not in {i["layer_id"] for i in ascendida["fixed_layers"]}

    vuelta = client.put(
        f"/brands/{brand['brand_id']}/templates/{plantilla['template_id']}",
        json={"demote": ["logo"]},
    )
    assert vuelta.status_code == 200, vuelta.text
    final = vuelta.json()["template"]
    assert "logo" not in {slot["id"] for slot in final["slots"]}
    assert any(item["category"] == "logo" for item in final["fixed_layers"])


def test_renombrar_un_campo_cambia_su_cabecera(client, brand, kv):
    plantilla = _crear_plantilla(client, brand, kv)
    respuesta = client.put(
        f"/brands/{brand['brand_id']}/templates/{plantilla['template_id']}",
        json={"slots": [{"id": "precio", "rename_to": "Precio Oferta", "required": False}]},
    )
    assert respuesta.status_code == 200, respuesta.text
    campos = {slot["id"]: slot for slot in respuesta.json()["template"]["slots"]}
    assert "precio_oferta" in campos
    assert campos["precio_oferta"]["required"] is False


def test_un_identificador_que_no_es_uuid_no_escapa_de_la_carpeta(client):
    for sospechoso in ("../../etc", "..%2f..%2fetc", "no-es-uuid"):
        respuesta = client.get(f"/brands/{sospechoso}")
        assert respuesta.status_code in {400, 404, 422}, respuesta.text


def test_borrar_la_marca_se_lleva_su_biblioteca(client, brand, kv):
    plantilla = _crear_plantilla(client, brand, kv)
    base = template_store.brand_dir(brand["brand_id"])
    assert base.exists()

    respuesta = client.delete(f"/brands/{brand['brand_id']}")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["templates_deleted"] == 1
    assert not base.exists()

    perdida = client.get(
        f"/brands/{brand['brand_id']}/templates/{plantilla['template_id']}"
    )
    assert perdida.status_code == 404


def test_avisa_cuando_el_psd_no_dijo_que_es_cada_capa(client, brand, project):
    """El caso real: un PSD de agencia con quince capas llamadas «Decoración 7».

    Se reconoce el producto por la geometría y nada más, así que la plantilla
    sale con un campo y trece capas fijas. Decirlo al crearla es la diferencia
    entre revisar trece miniaturas una vez y descubrirlo cuando los doscientos
    artes ya salieron con el precio del KV original.
    """
    pid = project["project_id"]
    for nombre, categoria, caja in (
        ("Producto", "product", (180, 180, 240, 250)),
        ("Decoración 7", "decoration", (40, 470, 220, 60)),
        ("Decoración 8", "decoration", (300, 60, 240, 60)),
    ):
        x, y, w, h = caja
        respuesta = client.post(
            f"/projects/{pid}/layers",
            json={
                "name": nombre,
                "category": categoria,
                "type": "image",
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "auto_segment": False,
            },
        )
        assert respuesta.status_code == 201, respuesta.text

    creada = client.post(
        f"/brands/{brand['brand_id']}/templates/from-project",
        json={"project_id": pid, "classify_with_vision": False},
    )
    assert creada.status_code == 201, creada.text
    cuerpo = creada.json()
    assert len(cuerpo["template"]["slots"]) == 1
    assert any("capas fijas" in aviso for aviso in cuerpo["warnings"]), cuerpo["warnings"]


def test_un_kv_sin_capas_no_se_convierte_en_plantilla(client, brand, project):
    respuesta = client.post(
        f"/brands/{brand['brand_id']}/templates/from-project",
        json={"project_id": project["project_id"]},
    )
    assert respuesta.status_code == 409
    assert "capas" in respuesta.json()["detail"].lower()
