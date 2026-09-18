"""Contrato de la matriz: una fila decide contenido, formatos y propuestas."""
from __future__ import annotations

import pytest

from app.services.production_matrix import (
    MatrixParseError,
    parse_csv,
    requested_piece_count,
    score_template,
    select_template,
)


def test_csv_respeta_comas_entre_comillas_y_columnas_en_espanol():
    rows = parse_csv(
        'producto,imagen,titular,precio,cta,formatos,cantidad_propuestas\n'
        'TV 55,tv.png,"Grande, brillante y conectado",499,"Compra, disfruta",'
        '"feed;story",2\n'
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.titular == "Grande, brillante y conectado"
    assert row.cta == "Compra, disfruta"
    assert row.precio_actual == "499"
    assert row.formatos == ["feed", "story"]
    assert row.cantidad_propuestas == 2


def test_detecta_csv_con_punto_y_coma_y_formatos_separados_por_barra():
    rows = parse_csv(
        "producto;imagen;subtitulo;formatos;propuestas\n"
        "Laptop;laptop.png;Potencia para crear;1080x1080|1080x1920|1080x1080;3\n"
    )

    assert rows[0].subtitulo == "Potencia para crear"
    assert rows[0].formatos == ["1080x1080", "1080x1920"]
    assert rows[0].cantidad_propuestas == 3


def test_celdas_vacias_no_inventan_copy_y_no_poner_bloquea_la_ia():
    [row] = parse_csv(
        "producto,imagen,titular,subtitulo,precio,cta,legal\n"
        "Refrigeradora,refri.png,,no poner,799,omitir,\n"
    )

    assert row.titular is None
    assert row.subtitulo is None
    assert row.cta is None
    assert row.legal is None
    assert row.suppressed_fields == ["subtitulo", "cta"]


@pytest.mark.parametrize("quantity", ["0", "7", "dos", "1.5"])
def test_rechaza_cantidades_fuera_del_contrato(quantity: str):
    with pytest.raises(MatrixParseError, match="cantidad_propuestas"):
        parse_csv(
            "producto,imagen,cantidad_propuestas\n"
            f"TV,tv.png,{quantity}\n"
        )


def test_la_cantidad_total_es_por_fila_por_formato():
    rows = parse_csv(
        "producto,imagen,formatos,cantidad_propuestas\n"
        "TV,tv.png,feed|story,3\n"
        "Laptop,laptop.png,feed,2\n"
        "Audio,audio.png,,4\n"
    )

    assert requested_piece_count(rows) == 2 * 3 + 1 * 2 + 1 * 4


def test_aliases_de_excel_llegan_al_mismo_contrato():
    [row] = parse_csv(
        b"SKU,file,headline,old_price,current_price,installments,discount,terms,validity,template,count\n"
        b"ABC-1,abc.png,Oferta,999,799,12x,20%,Aplican terminos,Septiembre,Precio grande,2\n"
    )

    assert row.producto == "ABC-1"
    assert row.precio_anterior == "999"
    assert row.precio_actual == "799"
    assert row.cuota == "12x"
    assert row.descuento == "20%"
    assert row.legal == "Aplican terminos"
    assert row.vigencia == "Septiembre"
    assert row.plantilla == "Precio grande"


def test_selector_prefiere_slots_compatibles_y_soporta_combos():
    [row] = parse_csv(
        "producto,imagen,precio,cta\n"
        "TV | Soundbar,combo.png,699,Comprar\n"
    )
    simple = {
        "candidate_id": "simple",
        "name": "Producto individual",
        "slots": [
            {"id": "producto", "category": "product", "required": True},
            {"id": "precio", "required": False},
            {"id": "cta", "required": False},
        ],
    }
    combo = {
        "candidate_id": "combo",
        "name": "Combo",
        "slots": [
            {"id": "producto", "category": "product", "required": True},
            {"id": "producto_2", "category": "product", "required": True},
            {"id": "precio", "required": False},
            {"id": "cta", "required": False},
        ],
    }

    assert score_template(row, simple) == float("-inf")
    assert select_template(row, [simple, combo]) is combo


def test_combo_tambien_se_detecta_por_varias_imagenes_en_una_fila():
    [row] = parse_csv(
        "producto,imagen,precio\n"
        'Combo sala,"tv.png|soundbar.png",699\n'
    )
    simple = {
        "candidate_id": "simple",
        "name": "Producto individual",
        "slots": [{"id": "producto", "category": "product", "required": True}],
        "supported_product_count": {"minimum": 1, "maximum": 1},
    }
    combo = {
        "candidate_id": "combo",
        "name": "Combo",
        "slots": [
            {
                "id": "productos",
                "category": "product",
                "required": True,
                "repeatable": True,
            },
            {"id": "precio", "required": False},
        ],
        "supported_product_count": {"minimum": 2, "maximum": 4},
    }

    assert score_template(row, simple) == float("-inf")
    assert select_template(row, [simple, combo]) is combo


def test_columna_plantilla_es_override_opcional_no_un_requisito():
    [automatic] = parse_csv("producto,imagen\nTV,tv.png\n")
    [forced] = parse_csv("producto,imagen,plantilla\nTV,tv.png,Editorial\n")
    price = {
        "candidate_id": "price",
        "name": "Precio",
        "slots": [{"id": "producto", "category": "product", "required": True}],
    }
    editorial = {
        "candidate_id": "editorial",
        "name": "Editorial",
        "slots": [{"id": "producto", "category": "product", "required": True}],
    }

    assert select_template(automatic, [price, editorial]) is price
    assert select_template(forced, [price, editorial]) is editorial


def test_una_plantilla_combo_no_se_elige_para_un_solo_producto():
    [row] = parse_csv("producto,imagen\nTV,tv.png\n")
    combo = {
        "candidate_id": "combo",
        "name": "Combo",
        "slots": [
            {
                "id": "productos",
                "category": "product",
                "required": True,
                "repeatable": True,
            }
        ],
        "supported_product_count": {"minimum": 2, "maximum": 4},
    }

    assert score_template(row, combo) == float("-inf")
    assert select_template(row, [combo]) is None
