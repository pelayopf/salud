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
        datos={
            "product_name": "Cereal antiguo",
            "brands": "Bio",
            "nutriscore_grade": "a",
            "nutriments": {
                "energy-kcal_100g": 350.0,
                "proteins_100g": 9.0,
                "carbohydrates_100g": 65.0,
                "fat_100g": 3.0,
            },
        },
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


def test_producto_solo_valores_preparados():
    cliente = con_json(
        encontrado(
            {
                "code": EAN13,
                "product_name": "Sopa deshidratada inventada",
                "nutriments": {
                    "energy-kcal_prepared_100g": 35.0,
                    "proteins_prepared_100g": 1.2,
                    "carbohydrates_prepared_100g": 5.4,
                    "fat_prepared_100g": 0.8,
                },
            }
        )
    )
    p = buscar_producto(EAN13, cliente)
    assert (p.kcal, p.proteinas, p.hidratos, p.grasas) == (35.0, 1.2, 5.4, 0.8)
    assert p.preparado is True
    assert not p.incompleto


def test_producto_mezcla_normal_y_preparado():
    cliente = con_json(
        encontrado(
            {
                "code": EAN13,
                "product_name": "Puré inventado",
                "nutriments": {
                    "energy-kcal_100g": 200.0,
                    "proteins_100g": 6.0,
                    "carbohydrates_prepared_100g": 40.0,
                    "fat_100g": 2.0,
                },
            }
        )
    )
    p = buscar_producto(EAN13, cliente)
    assert (p.kcal, p.proteinas, p.hidratos, p.grasas) == (200.0, 6.0, 40.0, 2.0)
    assert p.preparado is True
    assert not p.incompleto


def test_producto_solo_kj_preparados_se_convierte():
    cliente = con_json(
        encontrado(
            {
                "code": EAN13,
                "product_name": "Caldo inventado",
                "nutriments": {
                    "energy_prepared_100g": 418.4,
                    "proteins_prepared_100g": 2.0,
                    "carbohydrates_prepared_100g": 10.0,
                    "fat_prepared_100g": 1.0,
                },
            }
        )
    )
    p = buscar_producto(EAN13, cliente)
    assert p.kcal == 100.0
    assert (p.proteinas, p.hidratos, p.grasas) == (2.0, 10.0, 1.0)
    assert p.preparado is True
    assert not p.incompleto


def test_producto_cache_con_columnas_vacias_reconstruye_desde_datos(con):
    datos_crudos = {
        "code": EAN13,
        "product_name": "Copos inventados",
        "brands": "Marca Test",
        "nutriscore_grade": "a",
        "quantity": "500 g",
        "nutriments": {
            "energy-kcal_prepared_100g": 85.0,
            "proteins_prepared_100g": 2.5,
            "carbohydrates_prepared_100g": 18.0,
            "fat_prepared_100g": 0.5,
        },
    }
    # Guardamos en la base de datos con columnas nutricionales vacías pero datos con preparados
    db.guardar_producto(
        con,
        codigo=EAN13,
        nombre="Copos inventados",
        marca="Marca Test",
        nutriscore="A",
        unidad="g",
        kcal=None,
        proteinas=None,
        hidratos=None,
        grasas=None,
        datos=datos_crudos,
    )

    cliente_sin_red, peticiones = cliente_falso(lambda _: httpx.Response(500))
    res = alimentos.obtener_producto(con, EAN13, cliente=cliente_sin_red)
    assert res.desde_cache is True
    assert len(peticiones) == 0
    p = res.producto
    assert p.nombre == "Copos inventados"
    assert (p.kcal, p.proteinas, p.hidratos, p.grasas) == (85.0, 2.5, 18.0, 0.5)
    assert p.preparado is True
    assert not p.incompleto


def test_producto_desde_fila_datos_vacios_o_invalidos(con):
    db.guardar_producto(
        con,
        codigo=EAN13,
        nombre="Producto clásico",
        marca="Marca",
        nutriscore="B",
        unidad="g",
        kcal=120.0,
        proteinas=3.0,
        hidratos=20.0,
        grasas=1.0,
        datos={},
    )
    # 1. Con datos={} (vacío)
    fila = db.leer_producto(con, EAN13)
    assert fila is not None
    p = alimentos.producto_desde_fila(fila)
    assert p.nombre == "Producto clásico"
    assert (p.kcal, p.proteinas, p.hidratos, p.grasas) == (120.0, 3.0, 20.0, 1.0)
    assert p.preparado is False

    # 2. Con datos como texto no JSON
    con.execute("UPDATE productos SET datos = 'no-es-json' WHERE codigo = ?", (EAN13,))
    fila_invalida = db.leer_producto(con, EAN13)
    assert fila_invalida is not None
    p2 = alimentos.producto_desde_fila(fila_invalida)
    assert p2.nombre == "Producto clásico"
    assert (p2.kcal, p2.proteinas, p2.hidratos, p2.grasas) == (120.0, 3.0, 20.0, 1.0)
    assert p2.preparado is False


def test_producto_desde_json_serving_quantity():
    p_json = {
        "product_name": "Cereales",
        "product_quantity_unit": "g",
        "serving_quantity": 30,
        "nutriments": {"energy-kcal_100g": 380.0},
    }
    prod = alimentos.producto_desde_json("12345678", p_json)
    assert prod.racion_g == 30.0
    assert prod.racion_nombre == "ración"


def test_limpiar_racion_nombre():
    assert alimentos.limpiar_racion_nombre("1 plátano mediano (sin piel)") == "plátano mediano"
    assert (
        alimentos.limpiar_racion_nombre("Taza media (aprox. 150 ml de leche semi") == "taza media"
    )
    assert alimentos.limpiar_racion_nombre("Una taza") == "taza"
    assert alimentos.limpiar_racion_nombre("2,5 rebanadas") == "rebanadas"
    assert alimentos.limpiar_racion_nombre("  Lata. ") == "lata"
    assert alimentos.limpiar_racion_nombre("(algo)") is None
    assert alimentos.limpiar_racion_nombre(None) is None
    largo = "a" * 60
    limpio_largo = alimentos.limpiar_racion_nombre(largo)
    assert limpio_largo is not None
    assert len(limpio_largo) <= 30
    assert limpio_largo == "a" * 30


def test_nombre_con_raciones():
    assert alimentos.nombre_con_raciones("Plátano", 240, 120, "plátano") == "Plátano (2 plátanos)"
    assert alimentos.nombre_con_raciones("Plátano", 120, 120, "plátano") == "Plátano (1 plátano)"
    assert alimentos.nombre_con_raciones("Plátano", 130, 120, "plátano") == "Plátano"
    assert alimentos.nombre_con_raciones("Plátano", 100, None, None) == "Plátano"
    assert alimentos.nombre_con_raciones("Pan", 60, 30, "rebanadas") == "Pan (2 rebanadas)"
    assert (
        alimentos.nombre_con_raciones("Plátano", 120, 120, "1 plátano mediano (sin piel)")
        == "Plátano (1 plátano mediano)"
    )
    assert alimentos.nombre_con_raciones("Café", 300, 150, "Taza media") == "Café (2 × taza media)"
    assert alimentos.nombre_con_raciones("Bacalao", 560, 280, "ración") == "Bacalao (2 raciones)"


def test_buscar_off_exito():
    # 3 productos, uno con código no válido
    cliente, peticiones = cliente_falso(
        lambda _: httpx.Response(
            200,
            json={
                "count": 3,
                "hits": [
                    {
                        "code": EAN13,
                        "product_name": "Pan de centeno",
                        "brands": ["Hacendado"],
                        "nutriments": {"energy-kcal_100g": 250},
                    },
                    {
                        "code": EAN13_BIS,  # no empieza por "pan": fuera
                        "product_name": "Queso fresco rallado",
                    },
                    {
                        "code": "1234",  # no válido
                        "product_name": "Pan con código corto",
                        "brands": "Marca",
                    },
                    {
                        "code": EAN13_BIS,
                        "product_name_es": "Pan de molde",
                        "brands": "Bimbo, Bimbo España",
                        "nutriments": {"energy_100g": 1000},
                    },
                ],
            },
        )
    )
    res = alimentos.buscar_off("pan", cliente, limite=5)
    assert len(res) == 2
    assert res[0].codigo == EAN13
    assert res[0].nombre == "Pan de centeno"
    assert res[0].marca == "Hacendado"
    assert res[0].kcal == 250.0

    assert res[1].codigo == EAN13_BIS
    assert res[1].nombre == "Pan de molde"
    assert res[1].marca == "Bimbo"
    assert res[1].kcal == round(1000 / 4.184, 1)

    [p] = peticiones
    assert p.url.host == "search.openfoodfacts.org"
    assert p.url.path == "/search"
    assert p.url.params["q"] == 'pan countries_tags:"en:spain"'
    assert p.url.params["page_size"] == "15"


def test_buscar_off_quita_sintaxis_de_busqueda():
    cliente, peticiones = cliente_falso(lambda _: httpx.Response(200, json={"hits": []}))
    assert alimentos.buscar_off('  pan: "integral" (bimbo)  ', cliente) == []
    assert peticiones[0].url.params["q"] == 'pan integral bimbo countries_tags:"en:spain"'

    vacio, sin_peticiones = cliente_falso(lambda _: httpx.Response(500))
    assert alimentos.buscar_off(":*", vacio) == []
    assert sin_peticiones == []


def test_buscar_off_500():
    cliente, _ = cliente_falso(lambda _: httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(OpenFoodFactsNoDisponible, match="Open Food Facts no responde ahora"):
        alimentos.buscar_off("pan", cliente)


def test_buscar_off_json_raro():
    # Texto HTML en vez de JSON
    cliente_html, _ = cliente_falso(lambda _: httpx.Response(200, text="<html>Error</html>"))
    with pytest.raises(OpenFoodFactsNoDisponible, match="Open Food Facts no responde ahora"):
        alimentos.buscar_off("pan", cliente_html)

    # JSON sin lista de hits
    cliente_sin_products, _ = cliente_falso(lambda _: httpx.Response(200, json={"status": 0}))
    with pytest.raises(OpenFoodFactsNoDisponible, match="Open Food Facts no responde ahora"):
        alimentos.buscar_off("pan", cliente_sin_products)
