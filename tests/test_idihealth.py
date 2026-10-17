"""API de IA para IdiHealth: token, solo en la tailnet y sin guardar lo que llega."""

import base64
from dataclasses import replace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from salud import db, ia, idihealth
from salud.garmin import GarminNoConectado
from salud.web import crear_app

TOKEN = "token-de-prueba-largo"
CABECERA = {"Authorization": f"Bearer {TOKEN}"}


def _eco(tarea, _settings):
    return ia.RespuestaIA({"titulo": tarea.nombre}, "vertex", "m")


def _cliente(settings, ejecutar=None, analizar=None) -> TestClient:
    rutas = idihealth.rutas(
        settings,
        ejecutar=ejecutar or _eco,
        analizar=analizar,
    )
    return TestClient(Starlette(routes=rutas))


@pytest.fixture
def con_token(settings):
    return replace(settings, idihealth_token=TOKEN)


def test_sin_token_configurado_no_hay_rutas(settings):
    assert idihealth.rutas(settings) == []


def test_pide_el_token(con_token):
    cliente = _cliente(con_token)
    datos = {"datos": {"recuperacion": 70}}
    assert cliente.post("/api/idihealth/lectura", json=datos).status_code == 401
    malo = {"Authorization": "Bearer otro"}
    assert cliente.post("/api/idihealth/lectura", json=datos, headers=malo).status_code == 401
    assert cliente.post("/api/idihealth/lectura", json=datos, headers=CABECERA).status_code == 200


def test_lectura_manda_los_datos_a_la_ia_y_devuelve_su_respuesta(con_token):
    vistas: list[ia.Tarea] = []

    def ejecutar(tarea, _settings):
        vistas.append(tarea)
        return ia.RespuestaIA(
            {"titulo": "Día tranquilo", "resumen": "r", "entreno": "e", "comida": "c"},
            "vertex",
            "m",
        )

    cliente = _cliente(con_token, ejecutar=ejecutar)
    r = cliente.post(
        "/api/idihealth/lectura", json={"datos": {"recuperacion": 64}}, headers=CABECERA
    )
    assert r.status_code == 200
    assert r.json()["respuesta"]["titulo"] == "Día tranquilo"
    assert vistas[0].nombre == "idihealth-lectura"
    assert '"recuperacion": 64' in vistas[0].instrucciones


def test_que_como(con_token):
    cliente = _cliente(con_token)
    datos = {"datos": {"kcal_quedan": 800}}
    r = cliente.post("/api/idihealth/que-como", json=datos, headers=CABECERA)
    assert r.json()["respuesta"]["titulo"] == "idihealth-que-como"


def test_peticiones_mal_formadas(con_token):
    cliente = _cliente(con_token)
    url = "/api/idihealth/lectura"
    assert cliente.post(url, content=b"no es json", headers=CABECERA).status_code == 400
    assert cliente.post(url, json=[1, 2], headers=CABECERA).status_code == 400
    assert cliente.post(url, json={"otra": 1}, headers=CABECERA).status_code == 400
    grande = {"datos": {"x": "a" * (idihealth.MAX_CUERPO + 10)}}
    assert cliente.post(url, json=grande, headers=CABECERA).status_code == 413


def test_errores_de_la_ia(con_token):
    def ocupada(_tarea, _settings):
        raise ia.IAOcupada("ocupada")

    def tope(_tarea, _settings):
        raise ia.TopeGasto("tope")

    datos = {"datos": {"a": 1}}
    url = "/api/idihealth/lectura"
    r = _cliente(con_token, ejecutar=ocupada).post(url, json=datos, headers=CABECERA)
    assert r.status_code == 409
    r = _cliente(con_token, ejecutar=tope).post(url, json=datos, headers=CABECERA)
    assert r.status_code == 503


def test_comida_con_texto_y_foto(con_token):
    recibido: dict = {}

    def analizar(texto, imagen):
        recibido["texto"], recibido["imagen"] = texto, imagen
        return [ia.AlimentoIA("Tortilla", 150, "g", 220, 12, 4, 16, "comida", None)]

    cliente = _cliente(con_token, analizar=analizar)
    foto = base64.b64encode(b"\xff\xd8\xff falsa").decode()
    r = cliente.post(
        "/api/idihealth/comida", json={"texto": "tortilla", "imagen": foto}, headers=CABECERA
    )
    assert r.status_code == 200
    assert r.json()["alimentos"][0]["nombre"] == "Tortilla"
    assert recibido == {"texto": "tortilla", "imagen": b"\xff\xd8\xff falsa"}
    malo = {"texto": "x", "imagen": "@@@"}
    assert cliente.post("/api/idihealth/comida", json=malo, headers=CABECERA).status_code == 400


def test_no_guarda_nada_salvo_el_registro_de_uso(con_token):
    cliente = _cliente(con_token)
    cliente.post("/api/idihealth/lectura", json={"datos": {"secreto": "x"}}, headers=CABECERA)
    con = db.conectar(con_token.db_path)
    try:
        tablas = [f[0] for f in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for tabla in tablas:
            for fila in con.execute(f"SELECT * FROM {tabla}"):
                assert "secreto" not in str(tuple(fila))
    finally:
        con.close()


def _web(settings, publica: bool) -> TestClient:
    def conectar():
        raise GarminNoConectado("sin sesión")

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=conectar,
        publica=publica,
        analizar=lambda _t, _img: [],
    )
    return TestClient(app)


def test_solo_en_la_web_de_la_tailnet(con_token, tmp_path):
    datos = {"texto": "pan"}
    url = "/api/idihealth/comida"
    tailnet = _web(con_token, publica=False).post(url, json=datos, headers=CABECERA)
    assert tailnet.status_code == 200
    oauth = tmp_path / "oauth.json"
    oauth.write_text('{"client_id": "id", "client_secret": "secreto"}')
    ajustes_publica = replace(con_token, google_oauth=oauth, url_publica="https://salud.example")
    publica = _web(ajustes_publica, publica=True).post(url, json=datos, headers=CABECERA)
    assert publica.status_code in (401, 404)
