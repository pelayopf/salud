"""Tests de la sección /comida: diario, productos de Open Food Facts y alta manual."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import db
from salud.config import Settings
from salud.web import crear_app, momento_por_hora

EAN13 = "2000000000008"
EAN13_BIS = "2000000000015"


def cliente_off_mock(
    productos: dict[str, Any] | None = None,
) -> tuple[httpx.Client, list[httpx.Request]]:
    peticiones: list[httpx.Request] = []
    datos = productos or {}

    def manejar(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
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
) -> TestClient:
    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=GarminFalso,
        hoy=lambda: hoy,
        cliente_off=cliente_off,
    )
    return TestClient(app, follow_redirects=False)


def producto_payload(
    codigo: str,
    nombre: str = "Avena crunchy",
    marca: str = "Marca Test",
    nutriscore: str = "b",
    unidad: str = "g",
    kcal: float = 380.0,
    proteinas: float = 12.0,
    hidratos: float = 60.0,
    grasas: float = 7.0,
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
    assert momento_por_hora(10) == "desayuno"
    assert momento_por_hora(11) == "comida"
    assert momento_por_hora(15) == "comida"
    assert momento_por_hora(16) == "merienda"
    assert momento_por_hora(19) == "merienda"
    assert momento_por_hora(20) == "cena"


def test_dia_vacio(settings):
    c = cliente_web(settings)
    r = c.get("/comida?fecha=2026-01-10")
    assert r.status_code == 200
    assert "Sin comidas registradas este día" in r.text
    assert "10 de enero" in r.text


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
    assert "no encontrado" in r.text.lower()
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
    assert "momento+no+v%C3%A1lido" in r.headers["location"]


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
        unidad="g",
        kcal=380.0,
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
