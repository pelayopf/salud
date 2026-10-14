"""Tests de Open Food Facts con respuestas inventadas: no se usa la red."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from salud import alimentos, db
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


def test_obtener_producto_usa_cache_y_red(con):
    from datetime import UTC, datetime, timedelta

    cliente, peticiones = cliente_falso(
        lambda _: httpx.Response(
            200,
            json=encontrado(
                {
                    "code": EAN13,
                    "product_name_es": "Galletas inventadas",
                    "brands": "Marca Ficticia",
                    "nutriscore_grade": "c",
                    "quantity": "200 g",
                    "nutriments": {
                        "energy-kcal_100g": 450,
                        "proteins_100g": 7.5,
                        "carbohydrates_100g": 65.2,
                        "fat_100g": 17.0,
                    },
                }
            ),
        )
    )

    # 1. Primera consulta: no está en caché, consulta Open Food Facts y lo guarda
    p1, desde_cache1 = alimentos.obtener_producto(con, EAN13, cliente=cliente)
    assert p1.nombre == "Galletas inventadas"
    assert desde_cache1 is False
    assert len(peticiones) == 1

    # 2. Segunda consulta: está en caché, no toca la red
    cliente_sin_red, peticiones2 = cliente_falso(lambda _: httpx.Response(500))
    p2, desde_cache2 = alimentos.obtener_producto(con, EAN13, cliente=cliente_sin_red)
    assert p2.nombre == "Galletas inventadas"
    assert desde_cache2 is True
    assert len(peticiones2) == 0

    # 3. Caché caducada (> 30 días): vuelve a consultar Open Food Facts
    hace_35_dias = (datetime.now(UTC) - timedelta(days=35)).isoformat()
    con.execute("UPDATE productos SET consultado = ? WHERE codigo = ?", (hace_35_dias, EAN13))

    cliente_refresco, peticiones3 = cliente_falso(
        lambda _: httpx.Response(
            200,
            json=encontrado(
                {
                    "code": EAN13,
                    "product_name_es": "Galletas inventadas (nueva fórmula)",
                    "nutriments": {"energy-kcal_100g": 460},
                }
            ),
        )
    )
    p3, desde_cache3 = alimentos.obtener_producto(con, EAN13, cliente=cliente_refresco)
    assert p3.nombre == "Galletas inventadas (nueva fórmula)"
    assert desde_cache3 is False
    assert len(peticiones3) == 1


def test_obtener_producto_caducado_falla_off_devuelve_cache(con):
    from datetime import UTC, datetime, timedelta

    hace_40_dias = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    db.guardar_producto(
        con,
        codigo=EAN13,
        nombre="Cereal antiguo",
        marca="Bio",
        nutriscore="A",
        unidad="g",
        kcal=350.0,
        proteinas=9.0,
        hidratos=65.0,
        grasas=3.0,
        datos={"raw": "antiguo"},
        consultado=hace_40_dias,
    )

    def caer(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin conexión", request=peticion)

    cliente_caido, _ = cliente_falso(caer)

    # OFF falla pero hay caché caducada: devuelve la caché en vez de fallar
    prod, desde_cache = alimentos.obtener_producto(con, EAN13, cliente=cliente_caido)
    assert prod.nombre == "Cereal antiguo"
    assert prod.kcal == 350.0
    assert desde_cache is True

    # Si no hay caché en absoluto, sí lanza la excepción
    with pytest.raises(OpenFoodFactsNoDisponible):
        alimentos.obtener_producto(con, EAN8, cliente=cliente_caido)


def test_calcular_nutrientes():
    prod = alimentos.Producto(
        codigo=EAN13,
        nombre="Pan de centeno",
        marca="Panadería",
        nutriscore="A",
        unidad="g",
        kcal=240.0,
        proteinas=8.0,
        hidratos=48.0,
        grasas=1.2,
    )
    # Porción de 50 g
    n50 = alimentos.calcular_nutrientes(prod, 50.0)
    assert n50.kcal == 120.0
    assert n50.proteinas == 4.0
    assert n50.hidratos == 24.0
    assert n50.grasas == 0.6
    assert not n50.incompleto

    # Porción con nutrientes None: se respetan los None y no cuentan como 0
    prod_incompleto = alimentos.Producto(
        codigo=EAN13,
        nombre="Aceite sin info de proteínas",
        marca=None,
        nutriscore=None,
        unidad="ml",
        kcal=900.0,
        proteinas=None,
        hidratos=None,
        grasas=100.0,
    )
    n15 = alimentos.calcular_nutrientes(prod_incompleto, 15.0)
    assert n15.kcal == 135.0
    assert n15.proteinas is None
    assert n15.hidratos is None
    assert n15.grasas == 15.0
    assert n15.incompleto


def test_cli_producto(settings, monkeypatch, capsys):
    import argparse

    from salud import cli

    # 1. Producto nuevo (desde OFF)
    cliente, _ = cliente_falso(
        lambda _: httpx.Response(
            200,
            json=encontrado(
                {
                    "code": EAN13,
                    "product_name_es": "Yogur natural",
                    "brands": "Lácteos Test",
                    "nutriscore_grade": "b",
                    "quantity": "125 g",
                    "nutriments": {
                        "energy-kcal_100g": 60,
                        "proteins_100g": 4.0,
                        "carbohydrates_100g": 5.0,
                        "fat_100g": 3.0,
                    },
                }
            ),
        )
    )
    # Interceptamos obtener_producto para pasarle el cliente falso
    orig_obtener = alimentos.obtener_producto
    monkeypatch.setattr(
        alimentos,
        "obtener_producto",
        lambda con, cod: orig_obtener(con, cod, cliente=cliente),
    )

    args = argparse.Namespace(codigo=EAN13)
    ret = cli.cmd_producto(settings, args)
    assert ret == 0
    salida = capsys.readouterr().out
    assert "Yogur natural" in salida
    assert "Open Food Facts" in salida
    assert "Nutri-Score: B" in salida
    assert "60.0 kcal" in salida

    # 2. Segunda consulta: debe indicar que viene de la caché
    monkeypatch.setattr(alimentos, "obtener_producto", orig_obtener)
    ret2 = cli.cmd_producto(settings, args)
    assert ret2 == 0
    salida2 = capsys.readouterr().out
    assert "Yogur natural" in salida2
    assert "caché" in salida2

    # 3. Código no válido
    args_invalido = argparse.Namespace(codigo="1234")
    ret_invalido = cli.cmd_producto(settings, args_invalido)
    assert ret_invalido == 1
    err_invalido = capsys.readouterr().err
    assert "Código no válido" in err_invalido

    # 4. Producto incompleto
    cliente_inc, _ = cliente_falso(
        lambda _: httpx.Response(
            200,
            json=encontrado(
                {
                    "code": EAN8,
                    "product_name": "Té verde",
                    "nutriments": {},
                }
            ),
        )
    )
    monkeypatch.setattr(
        alimentos,
        "obtener_producto",
        lambda con, cod: orig_obtener(con, cod, cliente=cliente_inc),
    )
    args_inc = argparse.Namespace(codigo=EAN8)
    ret_inc = cli.cmd_producto(settings, args_inc)
    assert ret_inc == 0
    salida_inc = capsys.readouterr().out
    assert "Té verde" in salida_inc
    assert "Aviso: datos nutricionales incompletos" in salida_inc
