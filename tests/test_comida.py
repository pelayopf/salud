"""Tests de la sección /comida: diario, productos de Open Food Facts y alta manual."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import alimentos, ciqual, db, ia
from salud.config import Settings
from salud.metricas import Metrica
from salud.web import crear_app, datos_producto, momento_por_hora, momento_valido

EAN13 = "2000000000008"
EAN13_BIS = "2000000000015"


def cliente_off_mock(
    productos: dict[str, Any] | None = None,
    busqueda: dict[str, Any] | int | None = None,
) -> tuple[httpx.Client, list[httpx.Request]]:
    peticiones: list[httpx.Request] = []
    datos = productos or {}

    def manejar(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        if peticion.url.host == "search.openfoodfacts.org":
            if busqueda is not None:
                if isinstance(busqueda, int):
                    return httpx.Response(busqueda)
                return httpx.Response(200, json=busqueda)
            return httpx.Response(200, json={"count": 0, "hits": []})
        for codigo, respuesta in datos.items():
            if f"/api/v2/product/{codigo}.json" in peticion.url.path:
                if isinstance(respuesta, int):
                    return httpx.Response(respuesta)
                return httpx.Response(200, json=respuesta)
        return httpx.Response(404, json={"status": 0, "product": None})

    return httpx.Client(transport=httpx.MockTransport(manejar)), peticiones


def cliente_web(
    settings: Settings,
    cliente_off: httpx.Client | None = None,
    hoy: date = date(2026, 1, 10),
    lector_etiqueta: Any = None,
    estimar: Any = None,
) -> TestClient:
    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=GarminFalso,
        hoy=lambda: hoy,
        cliente_off=cliente_off,
        lector_etiqueta=lector_etiqueta,
        estimar=estimar,
    )
    return TestClient(app, follow_redirects=False)


def producto_payload(
    codigo: str,
    nombre: str = "Avena crunchy",
    marca: str = "Marca Test",
    nutriscore: str = "b",
    unidad: str = "g",
    kcal: float | None = 380.0,
    proteinas: float | None = 12.0,
    hidratos: float | None = 60.0,
    grasas: float | None = 7.0,
) -> dict[str, Any]:
    return {
        "status": 1,
        "product": {
            "code": codigo,
            "product_name": nombre,
            "brands": marca,
            "nutriscore_grade": nutriscore,
            "product_quantity_unit": unidad,
            "nutriments": {
                "energy-kcal_100g": kcal,
                "proteins_100g": proteinas,
                "carbohydrates_100g": hidratos,
                "fat_100g": grasas,
            },
        },
    }


def test_momento_por_hora_limites():
    assert momento_por_hora(9) == "desayuno"
    assert momento_por_hora(10) == "media mañana"
    assert momento_por_hora(12) == "media mañana"
    assert momento_por_hora(13) == "comida"
    assert momento_por_hora(15) == "comida"
    assert momento_por_hora(16) == "merienda"
    assert momento_por_hora(20) == "cena"


def test_momento_valido():
    assert momento_valido("desayuno") == "desayuno"
    assert momento_valido("media mañana") == "media mañana"
    assert momento_valido("comida") == "comida"
    assert momento_valido("merienda") == "merienda"
    assert momento_valido("cena") == "cena"
    assert momento_valido("otro") == "otro"
    assert momento_valido("brunch") is None
    assert momento_valido("") is None
    assert momento_valido(None) is None


def test_dia_vacio(settings):
    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "Desayuno" in r.text
    assert "Media mañana" in r.text
    assert "Comida" in r.text
    assert "Merienda" in r.text
    assert "Cena" in r.text
    assert "Nada apuntado" in r.text
    assert "Otro" not in r.text
    assert "Sin comidas registradas este día" in r.text
    assert "10 de enero" in r.text
    assert "/comida?fecha=2026-01-10&momento=media%20ma%C3%B1ana#buscar" in r.text


def test_dia_con_entrada_en_otro(settings, con):
    db.anadir_comida(
        con,
        "2026-01-10",
        "otro",
        "Snack medianoche",
        50.0,
        "g",
        kcal=100.0,
    )
    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "Otro" in r.text
    assert "Snack medianoche" in r.text
    assert "100 kcal" in r.text


def test_momento_elegido_query_param(settings):
    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10&momento=media%20ma%C3%B1ana")
    assert r.status_code == 200
    assert "Añadiendo a: Media mañana" in r.text
    assert '<input type="hidden" name="momento" value="media mañana">' in r.text

    # Con momento inventado no debe aparecer ni el aviso ni el hidden
    r_invalido = c.get("/comida?fecha=2026-01-10&momento=brunch_inventado")
    assert r_invalido.status_code == 200
    assert "Añadiendo a:" not in r_invalido.text
    assert '<input type="hidden" name="momento"' not in r_invalido.text


def test_producto_con_momento_elegido(settings):
    off, _ = cliente_off_mock({EAN13: producto_payload(EAN13, nombre="Avena crunchy")})
    c = cliente_web(settings, cliente_off=off)
    r = c.get(f"/comida/producto?codigo={EAN13}&fecha=2026-01-10&momento=cena")
    assert r.status_code == 200
    assert '<option value="cena" selected>Cena</option>' in r.text


def test_recientes_y_propios_con_momento_elegido(settings, con):
    db.guardar_producto(
        con,
        codigo=EAN13,
        nombre="Avena crunchy",
        marca="Marca Test",
        nutriscore=None,
        unidad="g",
        kcal=380.0,
        proteinas=None,
        hidratos=None,
        grasas=None,
        datos={},
    )
    id_propio = db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Bizcocho casero",
        marca=None,
        unidad="g",
        kcal=400.0,
        proteinas=6.0,
        hidratos=50.0,
        grasas=20.0,
        origen="manual",
    )
    db.anadir_comida(con, "2026-01-10", "desayuno", "Avena crunchy", 50.0, "g", codigo=EAN13)
    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10&momento=media%20ma%C3%B1ana")
    assert r.status_code == 200
    assert f"/comida/producto?codigo={EAN13}&fecha=2026-01-10&momento=media%20ma%C3%B1ana" in r.text
    assert f"/comida/propio/{id_propio}?fecha=2026-01-10&momento=media%20ma%C3%B1ana" in r.text


def test_dia_con_entradas_en_dos_momentos_y_su_total(settings, con):
    db.anadir_comida(
        con,
        "2026-01-10",
        "desayuno",
        "Plátano",
        100.0,
        "g",
        kcal=90.0,
        proteinas=1.0,
        hidratos=22.0,
        grasas=0.3,
    )
    db.anadir_comida(
        con,
        "2026-01-10",
        "comida",
        "Pechuga de pollo",
        200.0,
        "g",
        kcal=330.0,
        proteinas=62.0,
        hidratos=0.0,
        grasas=7.2,
    )

    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200

    # Subtotales
    assert "Plátano" in r.text
    assert "90 kcal" in r.text
    assert "Pechuga de pollo" in r.text
    assert "330 kcal" in r.text

    # Total del día: 90 + 330 = 420 kcal
    assert "Total del día" in r.text
    assert "420 kcal" in r.text
    # Proteínas totales: 1.0 + 62.0 = 63.0 g
    assert "63 g" in r.text


def test_total_con_un_dato_que_falta(settings, con):
    # Comida completa
    db.anadir_comida(
        con,
        "2026-01-10",
        "comida",
        "Arroz",
        100.0,
        "g",
        kcal=130.0,
        proteinas=2.5,
        hidratos=28.0,
        grasas=0.3,
    )
    # Entrada sin proteínas ni grasas
    db.anadir_comida(
        con,
        "2026-01-10",
        "comida",
        "Alimento misterioso",
        50.0,
        "g",
        kcal=80.0,
        proteinas=None,
        hidratos=15.0,
        grasas=None,
    )

    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "faltan datos de" in r.text


def test_flujo_completo_codigo_producto_anadir(settings):
    off, peticiones = cliente_off_mock({EAN13: producto_payload(EAN13, nombre="Avena crunchy")})
    c = cliente_web(settings, cliente_off=off)

    # 1. Buscar producto por código
    r = c.get(f"/comida/producto?codigo={EAN13}&fecha=2026-01-10")
    assert r.status_code == 200
    assert "Avena crunchy" in r.text
    assert "Nutri-Score B" in r.text
    assert "380 kcal" in r.text

    # 2. Añadir al día con cantidad 50 g
    r_post = c.post(
        "/comida/anadir",
        data={
            "codigo": EAN13,
            "fecha": "2026-01-10",
            "cantidad": "50",
            "momento": "desayuno",
        },
    )
    assert r_post.status_code == 303
    assert "/comida?fecha=2026-01-10" in r_post.headers["location"]

    # 3. Comprobar que aparece en el día con valores proporcionales (factor 0.5)
    r_dia = c.get("/comida?fecha=2026-01-10")
    assert r_dia.status_code == 200
    assert "Avena crunchy" in r_dia.text
    assert "190 kcal" in r_dia.text  # 380 * 0.5
    assert "6 g" in r_dia.text  # 12 * 0.5

    # 4. Volver a consultar el producto: debe salir de la caché
    r_cache = c.get(f"/comida/producto?codigo={EAN13}&fecha=2026-01-10")
    assert r_cache.status_code == 200
    assert "de la caché" in r_cache.text


def test_codigo_invalido(settings):
    c = cliente_web(settings)
    r = c.get("/comida/producto?codigo=1234&fecha=2026-01-10")
    assert r.status_code == 200
    assert "Código no válido" in r.text
    assert 'value="1234"' in r.text


def test_producto_no_encontrado(settings):
    off, _ = cliente_off_mock()  # devuelve 404
    c = cliente_web(settings, cliente_off=off)
    r = c.get(f"/comida/producto?codigo={EAN13_BIS}&fecha=2026-01-10")
    assert r.status_code == 200
    assert "Nuevo producto" in r.text
    assert "Open Food Facts no tiene este producto" in r.text
    assert f'value="{EAN13_BIS}"' in r.text


def test_alta_a_mano(settings, con):
    c = cliente_web(settings)
    r = c.post(
        "/comida/anadir",
        data={
            "nombre": "Tortilla francesa",
            "momento": "cena",
            "cantidad": "150",
            "unidad": "g",
            "kcal": "220",
            "proteinas": "15",
            "hidratos": "2",
            "grasas": "16",
            "fecha": "2026-01-10",
        },
    )
    assert r.status_code == 303
    assert "/comida?fecha=2026-01-10" in r.headers["location"]

    r_dia = c.get("/comida?fecha=2026-01-10")
    assert r_dia.status_code == 200
    assert "Tortilla francesa" in r_dia.text
    assert "220 kcal" in r_dia.text

    # Alta a mano con nutrientes vacíos (deben guardarse como None)
    r2 = c.post(
        "/comida/anadir",
        data={
            "nombre": "Manzana",
            "momento": "merienda",
            "cantidad": "100",
            "unidad": "g",
            "kcal": "",
            "proteinas": "",
            "hidratos": "",
            "grasas": "",
            "fecha": "2026-01-10",
        },
    )
    assert r2.status_code == 303
    comidas = db.comidas(con, "2026-01-10")
    manzana = [x for x in comidas if x.nombre == "Manzana"][0]
    assert manzana.kcal is None
    assert manzana.proteinas is None


def test_alta_a_mano_con_momento_invalido(settings):
    c = cliente_web(settings)
    r = c.post(
        "/comida/anadir",
        data={
            "nombre": "Tapa",
            "momento": "picoteo_invalido",
            "cantidad": "100",
            "unidad": "g",
            "fecha": "2026-01-10",
        },
    )
    assert r.status_code == 303
    assert "Momento+no+v%C3%A1lido" in r.headers["location"]


def test_post_anadir_momento_brunch_y_media_manana(settings, con):
    c = cliente_web(settings)

    # Con momento "brunch" -> 303 con "Momento no válido" y nada guardado
    r_brunch = c.post(
        "/comida/anadir",
        data={
            "nombre": "Croissant",
            "momento": "brunch",
            "cantidad": "80",
            "unidad": "g",
            "kcal": "320",
            "fecha": "2026-01-10",
        },
    )
    assert r_brunch.status_code == 303
    assert "Momento+no+v%C3%A1lido" in r_brunch.headers["location"]
    assert len(db.comidas(con, "2026-01-10")) == 0

    # Con "media mañana" -> guardado
    r_mm = c.post(
        "/comida/anadir",
        data={
            "nombre": "Yogur",
            "momento": "media mañana",
            "cantidad": "125",
            "unidad": "g",
            "kcal": "85",
            "fecha": "2026-01-10",
        },
    )
    assert r_mm.status_code == 303
    comidas = db.comidas(con, "2026-01-10")
    assert len(comidas) == 1
    assert comidas[0].momento == "media mañana"
    assert comidas[0].nombre == "Yogur"


def test_borrar_comida(settings, con):
    id_comida = db.anadir_comida(
        con,
        "2026-01-10",
        "desayuno",
        "Café con leche",
        200.0,
        "ml",
        kcal=80.0,
        proteinas=4.0,
        hidratos=6.0,
        grasas=4.0,
    )
    c = cliente_web(settings)
    r_borrar = c.post(f"/comida/{id_comida}/borrar?fecha=2026-01-10")
    assert r_borrar.status_code == 303
    assert "/comida?fecha=2026-01-10" in r_borrar.headers["location"]

    assert len(db.comidas(con, "2026-01-10")) == 0


def test_recientes(settings, con):
    db.guardar_producto(
        con,
        codigo=EAN13,
        nombre="Avena crunchy",
        marca="Marca Test",
        nutriscore=None,
        unidad="g",
        kcal=380.0,
        proteinas=None,
        hidratos=None,
        grasas=None,
        datos={},
    )
    db.anadir_comida(con, "2026-01-10", "desayuno", "Avena crunchy", 50.0, "g", codigo=EAN13)

    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "Recientes" in r.text
    assert f'href="/comida/producto?codigo={EAN13}&fecha=2026-01-10"' in r.text


def test_post_csrf_cross_site_rechazado(settings):
    c = cliente_web(settings)
    r = c.post(
        "/comida/anadir",
        data={"nombre": "Ataque", "momento": "comida", "cantidad": "100", "unidad": "g"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 403


def test_cantidad_nan_e_inf_rechazadas(settings):
    c = cliente_web(settings)
    for valor in ("nan", "inf", "-inf"):
        r = c.post(
            "/comida/anadir",
            data={
                "nombre": "Pan",
                "momento": "comida",
                "cantidad": valor,
                "unidad": "g",
                "fecha": "2026-01-10",
            },
        )
        assert r.status_code == 303
        assert "finito" in r.headers["location"]

    # Nutriente a mano no finito
    r_nutriente = c.post(
        "/comida/anadir",
        data={
            "nombre": "Pan",
            "momento": "comida",
            "cantidad": "100",
            "unidad": "g",
            "kcal": "nan",
            "fecha": "2026-01-10",
        },
    )
    assert r_nutriente.status_code == 303
    assert "finito" in r_nutriente.headers["location"]


def test_fecha_futura_rechazada(settings):
    c = cliente_web(settings)  # hoy es 2026-01-10
    r = c.post(
        "/comida/anadir",
        data={
            "nombre": "Pan",
            "momento": "comida",
            "cantidad": "100",
            "unidad": "g",
            "fecha": "2026-01-15",
        },
    )
    assert r.status_code == 303
    assert "posterior+a+hoy" in r.headers["location"]


def test_producto_preparado_muestra_aviso(settings):
    payload = {
        "status": 1,
        "product": {
            "code": EAN13,
            "product_name": "Sopa deshidratada",
            "brands": "Marca Test",
            "nutriscore_grade": "a",
            "product_quantity_unit": "g",
            "nutriments": {
                "energy-kcal_prepared_100g": 40.0,
                "proteins_prepared_100g": 1.5,
                "carbohydrates_prepared_100g": 6.0,
                "fat_prepared_100g": 0.5,
            },
        },
    }
    off, _ = cliente_off_mock({EAN13: payload})
    c = cliente_web(settings, cliente_off=off)
    r = c.get(f"/comida/producto?codigo={EAN13}&fecha=2026-01-10")
    assert r.status_code == 200
    assert "Valores del producto preparado, según Open Food Facts" in r.text


def test_obtener_producto_con_producto_propio_prevalece_sobre_off(con):
    db.guardar_producto(
        con,
        codigo=EAN13,
        nombre="Cereal OFF",
        marca="Marca OFF",
        nutriscore="A",
        unidad="g",
        kcal=350.0,
        proteinas=8.0,
        hidratos=70.0,
        grasas=3.0,
        datos={"test": 1},
    )
    db.guardar_producto_propio(
        con,
        id_=None,
        codigo=EAN13,
        nombre="Cereal Corregido",
        marca="Mi Marca",
        unidad="g",
        kcal=390.0,
        proteinas=11.0,
        hidratos=65.0,
        grasas=4.0,
        origen="manual",
    )

    off, peticiones = cliente_off_mock()
    res = alimentos.obtener_producto(con, EAN13, cliente=off)
    assert res.desde_cache is True
    assert res.producto.nombre == "Cereal Corregido"
    assert res.producto.origen == "manual"
    assert res.producto.nutriscore == "A"
    assert len(peticiones) == 0


def test_datos_producto_casos_validos_y_errores():
    # Válido completo con comas decimales
    dp = datos_producto(
        {
            "codigo": EAN13,
            "nombre": "Copos de avena",
            "marca": "AvenaBio",
            "unidad": "g",
            "kcal": "370,5",
            "proteinas": "12,2",
            "hidratos": "60,0",
            "grasas": "7,1",
            "ia": "1",
        }
    )
    assert dp.codigo == EAN13
    assert dp.nombre == "Copos de avena"
    assert dp.marca == "AvenaBio"
    assert dp.unidad == "g"
    assert dp.kcal == 370.5
    assert dp.proteinas == 12.2
    assert dp.hidratos == 60.0
    assert dp.grasas == 7.1
    assert dp.origen == "etiqueta-ia"

    # Vacíos a None y origen manual (ia != 1)
    dp_vacio = datos_producto(
        {
            "codigo": "",
            "nombre": "A" * 150,
            "marca": "B" * 150,
            "unidad": "ml",
            "kcal": "",
            "proteinas": "",
            "hidratos": "",
            "grasas": "",
        }
    )
    assert dp_vacio.codigo is None
    assert len(dp_vacio.nombre) == 120
    assert dp_vacio.marca is not None and len(dp_vacio.marca) == 120
    assert dp_vacio.unidad == "ml"
    assert dp_vacio.kcal is None
    assert dp_vacio.proteinas is None
    assert dp_vacio.hidratos is None
    assert dp_vacio.grasas is None
    assert dp_vacio.origen == "manual"

    # Marca vacía → None
    dp_sin_marca = datos_producto(
        {
            "nombre": "Algo",
            "marca": "   ",
            "unidad": "g",
        }
    )
    assert dp_sin_marca.marca is None

    # ValueErrors:
    # Código no válido
    with pytest.raises(ValueError, match="Código de barras no válido"):
        datos_producto({"nombre": "X", "unidad": "g", "codigo": "123"})

    # Falta el nombre
    with pytest.raises(ValueError, match="Falta el nombre"):
        datos_producto({"nombre": "   ", "unidad": "g"})

    # Unidad no válida
    with pytest.raises(ValueError, match="Unidad no válida"):
        datos_producto({"nombre": "X", "unidad": "kg"})

    # Nutrientes no numéricos o no finitos
    for campo in ("kcal", "proteinas", "hidratos", "grasas"):
        with pytest.raises(ValueError, match=f"El valor de {campo} no es un número"):
            datos_producto({"nombre": "X", "unidad": "g", campo: "abc"})
        with pytest.raises(ValueError, match=f"El valor de {campo} no es un número"):
            datos_producto({"nombre": "X", "unidad": "g", campo: "nan"})
        with pytest.raises(ValueError, match=f"El valor de {campo} no es un número"):
            datos_producto({"nombre": "X", "unidad": "g", campo: "inf"})

    # Fuera de rango
    with pytest.raises(ValueError, match="El valor de kcal está fuera de rango"):
        datos_producto({"nombre": "X", "unidad": "g", "kcal": "-0.1"})
    with pytest.raises(ValueError, match="El valor de kcal está fuera de rango"):
        datos_producto({"nombre": "X", "unidad": "g", "kcal": "950.1"})

    for campo in ("proteinas", "hidratos", "grasas"):
        with pytest.raises(ValueError, match=f"El valor de {campo} está fuera de rango"):
            datos_producto({"nombre": "X", "unidad": "g", campo: "-0.5"})
        with pytest.raises(ValueError, match=f"El valor de {campo} está fuera de rango"):
            datos_producto({"nombre": "X", "unidad": "g", campo: "100.1"})


def test_post_comida_productos_flujos(settings, con):
    c = cliente_web(settings)

    # Con código válido → 303 a /comida/producto?codigo=...
    r_con = c.post(
        "/comida/productos",
        data={
            "codigo": EAN13,
            "nombre": "Barrita de sésamo",
            "marca": "SesameCo",
            "unidad": "g",
            "kcal": "520",
            "proteinas": "14",
            "hidratos": "45",
            "grasas": "30",
            "fecha": "2026-01-10",
        },
    )
    assert r_con.status_code == 303
    assert f"/comida/producto?codigo={EAN13}&fecha=2026-01-10" in r_con.headers["location"]

    # GET de esa página muestra los valores nuevos y "corregido por ti"
    r_ver = c.get(f"/comida/producto?codigo={EAN13}&fecha=2026-01-10")
    assert r_ver.status_code == 200
    assert "Barrita de sésamo" in r_ver.text
    assert "520 kcal" in r_ver.text
    assert "corregido por ti" in r_ver.text

    # Sin código → 303 a /comida/propio/{id}
    r_sin = c.post(
        "/comida/productos",
        data={
            "nombre": "Guiso de la abuela",
            "unidad": "g",
            "kcal": "120",
            "proteinas": "8",
            "hidratos": "10",
            "grasas": "4",
            "fecha": "2026-01-10",
        },
    )
    assert r_sin.status_code == 303
    loc_sin = r_sin.headers["location"]
    assert "/comida/propio/" in loc_sin

    # Datos no válidos → 303 con el mensaje
    r_invalido = c.post(
        "/comida/productos",
        data={
            "nombre": "",
            "unidad": "g",
            "fecha": "2026-01-10",
        },
    )
    assert r_invalido.status_code == 303
    assert "Falta+el+nombre" in r_invalido.headers["location"]

    # Con ia=1 → el producto queda etiqueta-ia
    r_ia = c.post(
        "/comida/productos",
        data={
            "codigo": EAN13_BIS,
            "nombre": "Leche de almendras",
            "unidad": "ml",
            "kcal": "25",
            "proteinas": "1",
            "hidratos": "2",
            "grasas": "1.5",
            "ia": "1",
            "fecha": "2026-01-10",
        },
    )
    assert r_ia.status_code == 303
    propio_ia = db.producto_propio_por_codigo(con, EAN13_BIS)
    assert propio_ia is not None
    assert propio_ia["origen"] == "etiqueta-ia"

    r_ver_ia = c.get(f"/comida/producto?codigo={EAN13_BIS}&fecha=2026-01-10")
    assert r_ver_ia.status_code == 200
    assert "leído de la etiqueta con IA y revisado" in r_ver_ia.text


def test_propio_404_y_anadir_comida_con_propio(settings, con):
    c = cliente_web(settings)

    # GET /comida/propio/999 → 404
    r_404 = c.get("/comida/propio/999?fecha=2026-01-10")
    assert r_404.status_code == 404

    # Crear producto propio sin código
    id_propio = db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Bizcocho casero",
        marca=None,
        unidad="g",
        kcal=400.0,
        proteinas=6.0,
        hidratos=50.0,
        grasas=20.0,
        origen="manual",
    )

    # POST /comida/anadir con propio → comida guardada con los nutrientes calculados
    r_anadir = c.post(
        "/comida/anadir",
        data={
            "propio": str(id_propio),
            "cantidad": "50",
            "momento": "merienda",
            "fecha": "2026-01-10",
        },
    )
    assert r_anadir.status_code == 303
    comidas = db.comidas(con, "2026-01-10")
    assert len(comidas) == 1
    c_entrada = comidas[0]
    assert c_entrada.nombre == "Bizcocho casero"
    assert c_entrada.codigo is None
    assert c_entrada.cantidad == 50.0
    # 400 * 0.5 = 200 kcal, 6 * 0.5 = 3 g proteina
    assert c_entrada.kcal == 200.0
    assert c_entrada.proteinas == 3.0
    assert c_entrada.hidratos == 25.0
    assert c_entrada.grasas == 10.0


def test_post_comida_etiqueta(settings, monkeypatch):
    from salud import ia

    lector_llamado = False

    def lector_falso(_datos: bytes) -> ia.Etiqueta:
        nonlocal lector_llamado
        lector_llamado = True
        return ia.Etiqueta(
            nombre="Kéfir",
            marca="KefirCo",
            unidad="ml",
            kcal=64.0,
            proteinas=3.5,
            hidratos=4.8,
            grasas=3.0,
            por_racion=False,
            cuadra=True,
            avisos=("Etiqueta clara",),
        )

    c = cliente_web(settings, lector_etiqueta=lector_falso)

    # 1. JPEG falso pequeño y lector falso → 200 y JSON con kcal, avisos (lista)
    jpeg_falso = b"\xff\xd8\xff" + b"0" * 10
    r_ok = c.post(
        "/comida/etiqueta",
        content=jpeg_falso,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_ok.status_code == 200
    assert lector_llamado is True
    data = r_ok.json()
    assert data["kcal"] == 64.0
    assert data["avisos"] == ["Etiqueta clara"]
    assert isinstance(data["avisos"], list)

    # 2. b"hola" → 415 sin llamar al lector
    lector_llamado = False
    r_415 = c.post(
        "/comida/etiqueta",
        content=b"hola",
        headers={"Content-Type": "text/plain"},
    )
    assert r_415.status_code == 415
    assert lector_llamado is False
    assert "no es una imagen" in r_415.json()["error"]

    # 3. cuerpo mayor que ia.MAX_BYTES_IMAGEN (monkeypatch a 100) → 413 sin llamar al lector
    monkeypatch.setattr(ia, "MAX_BYTES_IMAGEN", 100)
    cuerpo_grande = b"\xff\xd8\xff" + b"A" * 150
    lector_llamado = False
    r_413 = c.post(
        "/comida/etiqueta",
        content=cuerpo_grande,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_413.status_code == 413
    assert lector_llamado is False
    assert "demasiado grande" in r_413.json()["error"]

    # 4. lector que lanza ErrorIA("x") → 502 {"error": "x"}; ia.IAOcupada → 429
    monkeypatch.setattr(ia, "MAX_BYTES_IMAGEN", 8 * 1024 * 1024)

    def lector_error(_datos: bytes) -> ia.Etiqueta:
        raise ia.ErrorIA("fallo de motor")

    c_err = cliente_web(settings, lector_etiqueta=lector_error)
    r_502 = c_err.post("/comida/etiqueta", content=jpeg_falso)
    assert r_502.status_code == 502
    assert r_502.json() == {"error": "fallo de motor"}

    def lector_ocupada(_datos: bytes) -> ia.Etiqueta:
        raise ia.IAOcupada("ocupada")

    c_ocupada = cliente_web(settings, lector_etiqueta=lector_ocupada)
    r_429 = c_ocupada.post("/comida/etiqueta", content=jpeg_falso)
    assert r_429.status_code == 429
    assert r_429.json() == {"error": "ocupada"}


def test_producto_incompleto_incluye_data_leer_etiqueta_sin_capture(settings):
    payload = producto_payload(EAN13, nombre="Producto Incompleto", kcal=None)
    off, _ = cliente_off_mock({EAN13: payload})
    c = cliente_web(settings, cliente_off=off)
    r = c.get(f"/comida/producto?codigo={EAN13}&fecha=2026-01-10")
    assert r.status_code == 200
    assert "data-leer-etiqueta" in r.text
    assert "data-foto-etiqueta" in r.text
    assert "capture" not in r.text


def test_gramos_con_un_decimal_solo_si_lo_tiene():
    from salud.web import gramos

    assert gramos(16.2) == "16,2"
    assert gramos(3.0) == "3"
    assert gramos(1.04) == "1"
    assert gramos(None) == "—"


def test_web_comida_sin_datos_garmin(settings):
    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "Sin objetivo" in r.text


def test_web_comida_con_datos_garmin(settings, con):
    hoy = date(2026, 1, 10)
    for i in range(1, 29):
        f = (hoy - timedelta(days=i)).isoformat()
        db.guardar_metricas(
            con,
            [
                Metrica(f, "kcal", "garmin", 2600.0),
                Metrica(f, "kcal_activas", "garmin", 330.0),
            ],
        )
    db.guardar_metricas(con, [Metrica(hoy.isoformat(), "peso", "garmin", 88.0)])

    c = cliente_web(settings, hoy=hoy)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "Objetivo dinámico" in r.text
    assert "progreso-barra" in r.text


def test_web_comida_objetivos_get_desglose(settings, con):
    hoy = date(2026, 1, 10)
    for i in range(1, 29):
        f = (hoy - timedelta(days=i)).isoformat()
        db.guardar_metricas(
            con,
            [
                Metrica(f, "kcal", "garmin", 2600.0),
                Metrica(f, "kcal_activas", "garmin", 330.0),
            ],
        )
    db.guardar_metricas(con, [Metrica(hoy.isoformat(), "peso", "garmin", 88.0)])

    c = cliente_web(settings, hoy=hoy)
    r = c.get("/comida/objetivos")
    assert r.status_code == 200
    assert "Basal (Garmin)" in r.text
    assert "Total objetivo" in r.text
    assert "reparto-0" in r.text


def test_web_comida_objetivos_post_guardar_y_error(settings, con):
    c = cliente_web(settings)
    payload_valido = {
        "deficit_kcal": "350",
        "proteinas_g_kg": "1.7",
        "grasas_g_kg": "0.8",
        "peso_kg": "82.0",
        "reparto-0": "20",
        "reparto-1": "10",
        "reparto-2": "35",
        "reparto-3": "10",
        "reparto-4": "25",
    }
    r = c.post("/comida/objetivos", data=payload_valido)
    assert r.status_code == 303
    assert "/comida/objetivos" in r.headers["location"]

    ajustes = db.ajustes_comida(con)
    assert ajustes.deficit_kcal == 350
    assert ajustes.proteinas_g_kg == 1.7
    assert ajustes.peso_kg == 82.0

    # POST con reparto erróneo (suma 90) -> 303 y no guarda cambios
    payload_invalido = dict(payload_valido, **{"deficit_kcal": "400", "reparto-0": "10"})
    r_err = c.post("/comida/objetivos", data=payload_invalido)
    assert r_err.status_code == 303

    ajustes_despues = db.ajustes_comida(con)
    # El déficit sigue siendo 350, no 400
    assert ajustes_despues.deficit_kcal == 350


def test_datos_producto_raciones():
    # Válida con peso y nombre
    dp = datos_producto(
        {
            "nombre": "Plátano",
            "unidad": "g",
            "racion_g": "120",
            "racion_nombre": "plátano",
        }
    )
    assert dp.racion_g == 120.0
    assert dp.racion_nombre == "plátano"

    # Válida con peso sin nombre -> defecto "ración"
    dp_sin_nombre = datos_producto(
        {
            "nombre": "Plátano",
            "unidad": "g",
            "racion_g": "120",
        }
    )
    assert dp_sin_nombre.racion_g == 120.0
    assert dp_sin_nombre.racion_nombre == "ración"

    # Con descripción larga -> se limpia el nombre de la ración
    dp_limpio = datos_producto(
        {
            "nombre": "Café",
            "unidad": "ml",
            "racion_g": "250",
            "racion_nombre": "Una taza (250 ml)",
        }
    )
    assert dp_limpio.racion_nombre == "taza"

    # Nombre sin peso -> error
    with pytest.raises(ValueError, match="Falta el peso de la ración"):
        datos_producto(
            {
                "nombre": "Plátano",
                "unidad": "g",
                "racion_nombre": "plátano",
            }
        )

    # Peso 0 -> error
    with pytest.raises(ValueError, match="entre 1 y 2000"):
        datos_producto(
            {
                "nombre": "Plátano",
                "unidad": "g",
                "racion_g": "0",
            }
        )

    # Peso 5000 -> error
    with pytest.raises(ValueError, match="entre 1 y 2000"):
        datos_producto(
            {
                "nombre": "Plátano",
                "unidad": "g",
                "racion_g": "5000",
            }
        )


def test_web_comida_propio_con_racion(settings, con):
    id_ = db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Plátano de Canarias",
        marca=None,
        unidad="g",
        kcal=89.0,
        proteinas=1.1,
        hidratos=22.8,
        grasas=0.3,
        origen="manual",
        racion_g=120.0,
        racion_nombre="plátano",
    )
    c = cliente_web(settings)
    r = c.get(f"/comida/propio/{id_}")
    assert r.status_code == 200
    assert "1 plátano (120 g)" in r.text


def test_guardar_producto_racion_nombre_limpio_web(settings, con):
    c = cliente_web(settings)
    r = c.post(
        "/comida/productos",
        data={
            "nombre": "Té matcha",
            "unidad": "ml",
            "racion_g": "250",
            "racion_nombre": "Una taza (250 ml)",
            "fecha": "2026-01-10",
        },
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    id_propio = int(loc.split("/comida/propio/")[1].split("?")[0])
    prod = db.leer_producto_propio(con, id_propio)
    assert prod is not None
    assert prod["racion_nombre"] == "taza"


def test_web_comida_generico_ciqual(settings, con, monkeypatch):
    from salud import ciqual

    generico_test = ciqual.Generico(
        codigo="99999",
        nombre="Manzana reineta",
        nombre_original="Reinette apple",
        grupo="frutas",
        kcal=52.0,
        proteinas=0.3,
        hidratos=12.0,
        grasas=0.2,
        fibra=2.4,
        azucares=10.0,
        sal=0.01,
    )

    monkeypatch.setattr(
        ciqual,
        "por_codigo",
        lambda cod: generico_test if cod == "99999" else None,
    )

    c = cliente_web(settings)

    # 1. GET /comida/generico/99999 -> 200 con "CIQUAL" y nombre
    r = c.get("/comida/generico/99999")
    assert r.status_code == 200
    assert "CIQUAL" in r.text
    assert "Manzana reineta" in r.text
    assert "Guardar en Mis productos" in r.text

    # 2. GET con código inexistente -> 404
    r_404 = c.get("/comida/generico/00000")
    assert r_404.status_code == 404

    # 3. POST /comida/anadir con generico -> comida guardada con nutrientes calculados
    r_anadir = c.post(
        "/comida/anadir",
        data={
            "generico": "99999",
            "cantidad": "200",
            "momento": "comida",
            "fecha": "2026-01-10",
        },
    )
    assert r_anadir.status_code == 303
    comidas = db.comidas(con, "2026-01-10")
    assert len(comidas) == 1
    assert comidas[0].nombre == "Manzana reineta"
    assert comidas[0].cantidad == 200.0
    assert comidas[0].kcal == 104.0
    assert comidas[0].proteinas == 0.6
    assert comidas[0].codigo is None

    # 4. /comida/nuevo?generico=99999 -> formulario prerrellenado
    r_nuevo = c.get("/comida/nuevo?generico=99999")
    assert r_nuevo.status_code == 200
    assert "Manzana reineta" in r_nuevo.text
    assert 'value="52.0"' in r_nuevo.text or 'value="52"' in r_nuevo.text
    assert 'name="origen" value="ciqual"' in r_nuevo.text


def test_buscar_comida_codigo_barras(settings):
    c = cliente_web(settings)
    r = c.get("/comida/buscar?q=8410297112041&fecha=2026-01-10&momento=comida")
    assert r.status_code == 303
    assert (
        r.headers["location"]
        == "/comida/producto?codigo=8410297112041&fecha=2026-01-10&momento=comida"
    )


def test_buscar_comida_vacio(settings):
    c = cliente_web(settings)
    r = c.get("/comida/buscar?q=&fecha=2026-01-10&momento=comida")
    assert r.status_code == 303
    assert r.headers["location"] == "/comida?fecha=2026-01-10&momento=comida"


def test_buscar_comida_cuatro_grupos(settings, con, monkeypatch):
    # 1. Tu producto
    db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Plátano de Canarias",
        marca="Canarias",
        unidad="g",
        kcal=90.0,
        proteinas=1.0,
        hidratos=20.0,
        grasas=0.2,
        origen="manual",
    )
    # 2. Comida anterior
    db.anadir_comida(
        con,
        fecha="2026-01-08",
        momento="merienda",
        nombre="Plátano frito",
        cantidad=100.0,
        unidad="g",
        kcal=140.0,
        codigo=None,
    )
    # 3. Genérico CIQUAL
    monkeypatch.setattr(
        ciqual,
        "buscar",
        lambda _q, *_args, **_kwargs: [
            ciqual.Generico(
                codigo="13000",
                nombre="Plátano, crudo",
                nombre_original="Banana, raw",
                grupo="Frutas",
                kcal=89.0,
                proteinas=1.1,
                hidratos=22.8,
                grasas=0.3,
                fibra=2.6,
                azucares=12.2,
                sal=0.01,
            )
        ],
    )
    # 4. Open Food Facts
    cliente_off, _ = cliente_off_mock(
        busqueda={
            "count": 1,
            "hits": [
                {
                    "code": EAN13,
                    "product_name": "Plátano desecado",
                    "brands": "Bio",
                    "nutriments": {"energy-kcal_100g": 320.0},
                }
            ],
        }
    )

    c = cliente_web(settings, cliente_off=cliente_off)
    r = c.get("/comida/buscar?q=platano&fecha=2026-01-10&momento=comida")
    assert r.status_code == 200

    # Aparecen los cuatro títulos
    assert "Tus productos" in r.text
    assert "Lo que has apuntado antes" in r.text
    assert "Alimentos genéricos" in r.text
    assert "Open Food Facts (España)" in r.text

    # Aparece el botón / sección "Estimar con IA"
    assert "Estimar con IA" in r.text
    assert "Repetir" in r.text
    assert ciqual.FUENTE in r.text


def test_buscar_comida_off_caido(settings, con):
    # OFF devuelve 500
    cliente_off, _ = cliente_off_mock(busqueda=500)
    db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Pan de centeno propio",
        marca=None,
        unidad="g",
        kcal=220.0,
        proteinas=8.0,
        hidratos=45.0,
        grasas=1.0,
        origen="manual",
    )

    c = cliente_web(settings, cliente_off=cliente_off)
    r = c.get("/comida/buscar?q=centeno&fecha=2026-01-10")
    assert r.status_code == 200
    assert "Tus productos" in r.text
    assert "no responde ahora" in r.text


def test_buscar_comida_sin_nada(settings, monkeypatch):
    monkeypatch.setattr(ciqual, "buscar", lambda _q, *_args, **_kwargs: [])
    cliente_off, _ = cliente_off_mock(busqueda={"count": 0, "hits": []})

    c = cliente_web(settings, cliente_off=cliente_off)
    r = c.get("/comida/buscar?q=inexistente&fecha=2026-01-10")
    assert r.status_code == 200
    assert "No hay resultados" in r.text
    assert "Estimar con IA" in r.text


def test_repetir_comida(settings, con):
    id_orig = db.anadir_comida(
        con,
        fecha="2026-01-05",
        momento="cena",
        nombre="Tortilla francesa",
        cantidad=100.0,
        unidad="g",
        kcal=140.0,
        proteinas=11.0,
        hidratos=1.0,
        grasas=10.0,
        codigo=None,
    )
    c = cliente_web(settings)
    r = c.post(
        "/comida/repetir",
        data={"id": str(id_orig), "fecha": "2026-01-10", "momento": "comida"},
    )
    assert r.status_code == 303
    assert "/comida?fecha=2026-01-10" in r.headers["location"]

    nuevas = db.comidas(con, "2026-01-10")
    assert len(nuevas) == 1
    assert nuevas[0].nombre == "Tortilla francesa"
    assert nuevas[0].momento == "comida"
    assert nuevas[0].cantidad == 100.0
    assert nuevas[0].kcal == 140.0
    assert nuevas[0].proteinas == 11.0
    assert nuevas[0].codigo is None


def test_repetir_comida_id_malo(settings, con):
    c = cliente_web(settings)
    r = c.post(
        "/comida/repetir",
        data={"id": "99999", "fecha": "2026-01-10", "momento": "comida"},
    )
    assert r.status_code == 303
    assert "mensaje=" in r.headers["location"]
    assert len(db.comidas(con, "2026-01-10")) == 0


def test_estimar_comida_web_exito(settings):
    def estimar_falso(texto: str) -> ia.Estimacion:
        return ia.Estimacion(
            nombre=texto.capitalize(),
            unidad="g",
            kcal=89.0,
            proteinas=1.1,
            hidratos=22.8,
            grasas=0.3,
            racion_g=120.0,
            racion_nombre="plátano",
            avisos=("Valores estimados por IA: revísalos", "Aviso de prueba"),
        )

    c = cliente_web(settings, estimar=estimar_falso)
    r = c.post(
        "/comida/estimar",
        data={"q": "platano", "fecha": "2026-01-10", "momento": "merienda"},
    )
    assert r.status_code == 200
    assert 'name="ia" value="1"' in r.text
    assert "chip-ia" in r.text
    assert "de-ia" in r.text
    assert "Valores estimados por IA: revísalos" in r.text
    assert "Aviso de prueba" in r.text
    assert 'value="Platano"' in r.text


def test_estimar_comida_web_error_ia(settings):
    def estimar_falla(_texto: str) -> ia.Estimacion:
        raise ia.ErrorIA("La IA no reconoce ese alimento")

    c = cliente_web(settings, estimar=estimar_falla)
    r = c.post(
        "/comida/estimar",
        data={"q": "desconocido", "fecha": "2026-01-10", "momento": "comida"},
    )
    assert r.status_code == 303
    assert "/comida/buscar?" in r.headers["location"]
    assert "La+IA+no+reconoce+ese+alimento" in r.headers["location"]


def test_web_comida_proteinas_primero_y_minimo(settings, con):
    hoy = date(2026, 1, 10)
    ayer = hoy - timedelta(days=1)
    ayer_str = ayer.isoformat()

    for i in range(1, 29):
        f = (hoy - timedelta(days=i)).isoformat()
        db.guardar_metricas(
            con,
            [
                Metrica(f, "kcal", "garmin", 2500.0),
                Metrica(f, "kcal_activas", "garmin", 500.0),
            ],
        )
    db.guardar_metricas(con, [Metrica(hoy.isoformat(), "peso", "garmin", 80.0)])

    c = cliente_web(settings, hoy=hoy)

    db.anadir_comida(
        con,
        fecha=ayer_str,
        momento="comida",
        nombre="Manzana",
        cantidad=100.0,
        unidad="g",
        kcal=400.0,
        proteinas=1.0,
        hidratos=20.0,
        grasas=0.5,
    )

    r_ayer = c.get(f"/comida?fecha={ayer_str}")
    assert r_ayer.status_code == 200
    texto = r_ayer.text

    pos_prot = texto.find("Proteínas")
    pos_kcal = texto.find("Calorías")
    assert pos_prot != -1 and pos_kcal != -1
    assert pos_prot < pos_kcal

    assert "progreso-minimo" in texto
    assert "Por debajo del mínimo" in texto

    db.anadir_comida(
        con,
        fecha=ayer_str,
        momento="cena",
        nombre="Cena abundante",
        cantidad=500.0,
        unidad="g",
        kcal=2000.0,
        proteinas=50.0,
        hidratos=200.0,
        grasas=50.0,
    )

    r_ayer_ok = c.get(f"/comida?fecha={ayer_str}")
    assert r_ayer_ok.status_code == 200
    assert "Por debajo del mínimo" not in r_ayer_ok.text
