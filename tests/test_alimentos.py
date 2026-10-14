"""Tests de Open Food Facts con respuestas inventadas: no se usa la red."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from salud import alimentos
from salud.alimentos import (
    CodigoInvalido,
    OpenFoodFactsNoDisponible,
    ProductoNoEncontrado,
    buscar_producto,
    codigo_valido,
)

# Prefijo 20-29: uso interno en GS1, no corresponde a productos reales.
EAN13 = "2000000000008"
EAN13_BIS = "2000000000015"
EAN8 = "20000004"


def cliente_falso(
    responder: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    peticiones: list[httpx.Request] = []

    def manejar(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return responder(peticion)

    return httpx.Client(transport=httpx.MockTransport(manejar)), peticiones


def con_json(datos: Any, estado: int = 200) -> httpx.Client:
    cliente, _ = cliente_falso(lambda _: httpx.Response(estado, json=datos))
    return cliente


def encontrado(producto: dict[str, Any]) -> dict[str, Any]:
    return {"status": 1, "status_verbose": "product found", "product": producto}


def test_producto_completo():
    cliente, peticiones = cliente_falso(
        lambda _: httpx.Response(
            200,
            json=encontrado(
                {
                    "code": EAN13,
                    "product_name": "Inventado crunchy",
                    "product_name_es": "Galletas inventadas",
                    "brands": "Marca Ficticia, Otra Ficticia",
                    "nutriscore_grade": "c",
                    "quantity": "200 g",
                    "nutriments": {
                        "energy-kcal_100g": 450,
                        "energy_100g": 1883,
                        "proteins_100g": 7.5,
                        "carbohydrates_100g": "65,2",
                        "fat_100g": 17,
                    },
                }
            ),
        )
    )
    p = buscar_producto(EAN13, cliente)
    assert p.codigo == EAN13
    assert p.nombre == "Galletas inventadas"
    assert p.marca == "Marca Ficticia"
    assert p.nutriscore == "C"
    assert p.unidad == "g"
    assert (p.kcal, p.proteinas, p.hidratos, p.grasas) == (450.0, 7.5, 65.2, 17.0)
    assert not p.incompleto
    [peticion] = peticiones
    assert peticion.url.path == f"/api/v2/product/{EAN13}.json"
    assert peticion.url.host == "world.openfoodfacts.org"
    assert peticion.headers["User-Agent"].startswith("salud/")
    assert peticion.headers["User-Agent"].endswith("(pelayopf@gmail.com)")


def test_solo_kj_se_convierte_a_kcal_y_bebida_en_ml():
    cliente = con_json(
        encontrado(
            {
                "product_name": "Zumo inventado",
                "nutriscore_grade": "unknown",
                "product_quantity_unit": "ml",
                "quantity": "1 L",
                "nutriments": {
                    "energy_100g": 184.1,
                    "proteins_100g": 0.5,
                    "carbohydrates_100g": 10,
                    "fat_100g": 0,
                },
            }
        )
    )
    p = buscar_producto(EAN8, cliente)
    assert p.kcal == 44.0
    assert p.grasas == 0.0  # un 0 real se respeta
    assert p.nombre == "Zumo inventado"  # sin product_name_es, el genérico
    assert p.marca is None
    assert p.nutriscore is None
    assert p.unidad == "ml"
    assert not p.incompleto


def test_unidad_se_deduce_de_la_cantidad():
    cliente = con_json(encontrado({"quantity": "33 cl", "nutriments": {}}))
    assert buscar_producto(EAN13, cliente).unidad == "ml"


def test_sin_nutrientes_no_pone_ceros():
    cliente = con_json(encontrado({"product_name_es": "Producto inventado sin tabla"}))
    p = buscar_producto(EAN13, cliente)
    assert (p.kcal, p.proteinas, p.hidratos, p.grasas) == (None, None, None, None)
    assert p.incompleto


def test_nutrientes_a_medias_es_incompleto():
    cliente = con_json(
        encontrado({"nutriments": {"energy-kcal_100g": 120, "proteins_100g": "", "fat_100g": 3}})
    )
    p = buscar_producto(EAN13, cliente)
    assert p.kcal == 120.0
    assert p.proteinas is None
    assert p.hidratos is None
    assert p.incompleto


def test_no_encontrado_con_404():
    cliente = con_json({"status": 0, "status_verbose": "product not found"}, estado=404)
    with pytest.raises(ProductoNoEncontrado):
        buscar_producto(EAN13_BIS, cliente)


def test_no_encontrado_con_status_0():
    cliente = con_json({"status": 0, "status_verbose": "product not found"})
    with pytest.raises(ProductoNoEncontrado):
        buscar_producto(EAN13_BIS, cliente)


def test_error_de_red():
    def caer(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red (inventado)", request=peticion)

    cliente, _ = cliente_falso(caer)
    with pytest.raises(OpenFoodFactsNoDisponible):
        buscar_producto(EAN13, cliente)


def test_error_del_servidor_y_respuesta_rara():
    with pytest.raises(OpenFoodFactsNoDisponible):
        buscar_producto(EAN13, con_json({}, estado=503))
    cliente, _ = cliente_falso(lambda _: httpx.Response(200, text="<html>no</html>"))
    with pytest.raises(OpenFoodFactsNoDisponible):
        buscar_producto(EAN13, cliente)


@pytest.mark.parametrize(
    "codigo", ["2000000000009", "20000005", "12345", "abcdefghijklm", "", "٢٠٠٠٠٠٠٤"]
)
def test_ean_invalido_no_consulta(codigo):
    cliente, peticiones = cliente_falso(lambda _: httpx.Response(500))
    with pytest.raises(CodigoInvalido):
        buscar_producto(codigo, cliente)
    assert peticiones == []


def test_codigos_validos():
    assert codigo_valido(EAN13)
    assert codigo_valido(EAN13_BIS)
    assert codigo_valido(EAN8)
    assert codigo_valido("012345678905")  # UPC-A de ejemplo
    assert not codigo_valido("012345678906")
    assert alimentos.normalizar_codigo(" 2000 0000-00008 ") == EAN13


def test_errores_tienen_base_comun():
    for error in (CodigoInvalido, ProductoNoEncontrado, OpenFoodFactsNoDisponible):
        assert issubclass(error, alimentos.ErrorAlimentos)
