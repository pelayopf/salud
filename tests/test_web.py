import time
from dataclasses import replace
from datetime import date

import pytest
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import db, sync
from salud.garmin import GarminNoConectado
from salud.web import crear_app

HOY = date(2026, 1, 10)
LECTURA = {
    "estado": "ambar",
    "titular": "Carga alta: hoy suave",
    "analisis": "Dos partidos seguidos.",
    "entreno": {
        "titulo": "Rodillo Z1",
        "detalle": "40 min",
        "duracion_min": 40,
        "intensidad": "suave",
    },
    "comida": "Proteína en la cena.",
    "alertas": ["FC en reposo alta"],
}


def _app(settings, garmin=None, lector=None):
    def conectar():
        if garmin is None:
            raise GarminNoConectado("No hay sesión de Garmin")
        return garmin

    return crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=conectar,
        lector=lector,
        hoy=lambda: HOY,
    )


@pytest.fixture
def cliente(settings, garmin):
    con = db.conectar(settings.db_path)
    sync.sincronizar(con, garmin, date(2025, 12, 20), HOY, pausa=0)
    con.close()
    return TestClient(_app(settings, garmin))


def test_hoy_con_datos_y_lectura(cliente, settings):
    con = db.conectar(settings.db_path)
    db.guardar_lectura(con, HOY.isoformat(), contenido=LECTURA)
    con.close()
    r = cliente.get("/")
    assert r.status_code == 200
    assert "sábado 10 de enero" in r.text
    assert "Carga alta: hoy suave" in r.text and "Con cabeza" in r.text
    assert "FC en reposo alta" in r.text
    assert "7 h 30 min" in r.text  # sueño
    assert "Equilibrada · tu franja equilibrada: 50–70 ms" in r.text
    assert "80,1" in r.text  # peso
    assert "Productivo" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_hoy_vacio_y_otro_dia(settings):
    c = TestClient(_app(settings))
    r = c.get("/")
    assert r.status_code == 200
    assert "Aún no hay lectura de hoy" in r.text
    assert "sin sincronizar todavía" in r.text
    r = c.get("/?fecha=2026-01-05")
    assert "lunes 5 de enero" in r.text and "No hubo lectura este día" in r.text
    assert "Preparar la lectura" not in r.text
    # Una fecha futura se queda en hoy.
    assert "sábado 10 de enero" in c.get("/?fecha=2030-01-01").text


def test_tendencias_actividades_y_lecturas(cliente):
    r = cliente.get("/tendencias?dias=30")
    assert r.status_code == 200
    assert r.text.count('class="grafica"') >= 5
    assert "Ver tabla" in r.text
    r = cliente.get("/actividades")
    assert "Fútbol" in r.text and "Ciclismo" in r.text
    assert "Todavía no hay lecturas" in cliente.get("/lecturas").text


def test_solo_entran_los_usuarios_permitidos(settings):
    c = TestClient(_app(replace(settings, usuarios=frozenset({"yo@ejemplo.com"}))))
    assert c.get("/").status_code == 403
    assert c.get("/", headers={"Tailscale-User-Login": "Yo@Ejemplo.com"}).status_code == 200


def test_formulario_desde_otra_web(cliente):
    r = cliente.post("/sincronizar", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_sincronizar_desde_la_web(settings):
    garmin = GarminFalso()
    c = TestClient(_app(settings, garmin))
    r = c.post("/sincronizar", follow_redirects=False)
    assert r.status_code == 303 and "Garmin+al+d%C3%ADa" in r.headers["location"]
    assert garmin.llamadas.count("resumen") == 2

    sin_garmin = TestClient(_app(settings))
    r = sin_garmin.post("/sincronizar", follow_redirects=False)
    assert "No+hay+sesi%C3%B3n" in r.headers["location"]


def test_pedir_la_lectura_desde_la_web(settings):
    def lector(con, fecha):
        db.guardar_lectura(con, fecha.isoformat(), contenido=LECTURA)
        return LECTURA

    c = TestClient(_app(settings, lector=lector))
    r = c.post("/lectura", follow_redirects=False)
    assert r.status_code == 303
    for _ in range(50):
        con = db.conectar(settings.db_path)
        hecha = db.lectura(con, HOY.isoformat())
        con.close()
        if hecha:
            break
        time.sleep(0.05)
    assert hecha and hecha["contenido"]["titular"] == LECTURA["titular"]
    assert "Rehacer la lectura" in c.get("/").text or "Preparando" in c.get("/").text
