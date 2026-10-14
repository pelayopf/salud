"""Tests para la web del calendario: planificado, sincronización y semana tipo."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from urllib.parse import unquote_plus

import httpx
import pytest
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import db
from salud.config import Settings
from salud.metricas import Actividad
from salud.web import crear_app, dias_plan, entradas_semana


def cliente_web(
    settings: Settings,
    hoy: date = date(2026, 1, 10),
    cliente_calendario: httpx.Client | None = None,
) -> TestClient:
    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=GarminFalso,
        hoy=lambda: hoy,
        cliente_calendario=cliente_calendario,
    )
    return TestClient(app, follow_redirects=False)


def test_dias_plan():
    hoy = date(2026, 1, 10)
    desde = hoy - timedelta(days=1)  # 2026-01-09 (Ayer)
    hasta = hoy + timedelta(days=1)  # 2026-01-11 (Mañana)

    con = db.conectar(":memory:")
    a1 = Actividad(
        fuente="garmin",
        id_fuente="1",
        inicio="2026-01-09 10:00:00",
        tipo="cycling",
        nombre="Bici suave",
        bruto="{}",
        duracion_s=3600.0,
    )
    a2 = Actividad(
        fuente="garmin",
        id_fuente="2",
        inicio="2026-01-10 19:00:00",
        tipo="soccer",
        nombre="Fútbol pachanga",
        bruto="{}",
        duracion_s=3600.0,
    )
    db.guardar_actividades(con, [a1, a2])
    filas_act = db.actividades(con, "2026-01-09", "2026-01-11")

    p_ayer = db.Planificado(
        fecha="2026-01-09",
        inicio="10:00",
        duracion_min=60,
        titulo="Partido o entreno",
        tipo=None,
        intensidad="moderada",
        origen="google",
    )
    p_hoy = db.Planificado(
        fecha="2026-01-10",
        inicio="19:00",
        duracion_min=60,
        titulo="Fútbol",
        tipo="soccer",
        intensidad="alta",
        origen="google",
    )
    p_manana = db.Planificado(
        fecha="2026-01-11",
        inicio="11:00",
        duracion_min=90,
        titulo="Pádel",
        tipo="paddelball",
        intensidad="alta",
        origen="google",
    )

    dias = dias_plan([p_ayer, p_hoy, p_manana], filas_act, desde, hasta, hoy)

    assert len(dias) == 3
    assert dias[0].titulo == "Ayer"
    assert dias[0].fecha == "2026-01-09"
    assert dias[0].plan[0]["hecho"] is True

    assert dias[1].titulo == "Hoy"
    assert dias[1].fecha == "2026-01-10"
    assert dias[1].plan[0]["hecho"] is True

    assert dias[2].titulo == "Mañana"
    assert dias[2].fecha == "2026-01-11"
    assert dias[2].plan[0]["hecho"] is False


def test_get_actividades_planificado(settings):
    hoy = date(2026, 1, 10)
    con = db.conectar(settings.db_path)
    p_google = db.Planificado(
        fecha="2026-01-10",
        inicio="19:00",
        duracion_min=60,
        titulo="Pachanga",
        tipo="soccer",
        intensidad="alta",
        origen="google",
        uid="g1",
    )
    db.reemplazar_planificado(
        con, origen="google", desde="2026-01-10", hasta="2026-01-10", filas=[p_google]
    )
    db.guardar_semana_tipo(
        con,
        [
            db.EntradaSemana(
                dia=6, inicio="10:00", duracion_min=90, tipo="cycling", intensidad="moderada"
            )
        ],
    )
    con.close()

    c = cliente_web(settings, hoy=hoy)
    r = c.get("/actividades")
    assert r.status_code == 200
    assert "calendario" in r.text
    assert "semana tipo" in r.text
    assert "No es deporte" in r.text
    assert "Calendario sin leer todavía" in r.text


def test_post_calendario_titulo(settings):
    hoy = date(2026, 1, 10)
    con = db.conectar(settings.db_path)
    db.reemplazar_planificado(
        con,
        origen="google",
        desde="2026-01-10",
        hasta="2026-01-10",
        filas=[
            db.Planificado(
                fecha="2026-01-10",
                inicio="12:00",
                duracion_min=60,
                titulo="Cumple Ana",
                tipo=None,
                intensidad="moderada",
                origen="google",
                uid="cumple1",
            )
        ],
    )
    con.close()

    c = cliente_web(settings, hoy=hoy)

    # Título vacío -> error
    r_vacio = c.post("/calendario/titulo", data={"titulo": "", "deporte": "0"})
    assert r_vacio.status_code == 303
    assert "mensaje" in r_vacio.headers["location"]

    # deporte=0 -> 303, marcado y filas borradas
    r0 = c.post("/calendario/titulo", data={"titulo": "Cumple Ana", "deporte": "0"})
    assert r0.status_code == 303
    con = db.conectar(settings.db_path)
    assert db.titulos_calendario(con)["cumple ana"] == (False, None)
    assert len(db.planificado(con, "2026-01-10", "2026-01-10")) == 0
    con.close()

    # deporte=1 con tipo válido -> filas con ese tipo e intensidad
    con = db.conectar(settings.db_path)
    db.reemplazar_planificado(
        con,
        origen="google",
        desde="2026-01-10",
        hasta="2026-01-10",
        filas=[
            db.Planificado(
                fecha="2026-01-10",
                inicio="20:00",
                duracion_min=60,
                titulo="Pachanga Rara",
                tipo=None,
                intensidad="moderada",
                origen="google",
                uid="pachanga1",
            )
        ],
    )
    con.close()

    r1 = c.post(
        "/calendario/titulo",
        data={"titulo": "Pachanga Rara", "deporte": "1", "tipo": "soccer"},
    )
    assert r1.status_code == 303
    con = db.conectar(settings.db_path)
    assert db.titulos_calendario(con)["pachanga rara"] == (True, "soccer")
    filas_p = db.planificado(con, "2026-01-10", "2026-01-10")
    assert len(filas_p) == 1
    assert filas_p[0].tipo == "soccer"
    assert filas_p[0].intensidad == "alta"
    con.close()

    # deporte=1 con tipo no válido -> 303 con error y nada cambia
    r_inv = c.post(
        "/calendario/titulo",
        data={"titulo": "Pachanga Rara", "deporte": "1", "tipo": "tipo_inexistente"},
    )
    assert r_inv.status_code == 303
    con = db.conectar(settings.db_path)
    assert db.titulos_calendario(con)["pachanga rara"] == (True, "soccer")
    filas_p = db.planificado(con, "2026-01-10", "2026-01-10")
    assert filas_p[0].tipo == "soccer"
    con.close()


def test_post_calendario_sincronizar(settings):
    hoy = date(2026, 1, 10)
    c = cliente_web(settings, hoy=hoy)
    r_sin = c.post("/calendario/sincronizar")
    assert r_sin.status_code == 303
    assert "Falta SALUD_GCAL_ICS" in unquote_plus(r_sin.headers["location"])

    secret_url = "https://calendar.google.com/private-secret-token/basic.ics"
    settings_con_url = replace(settings, gcal_ics=secret_url)

    ics_content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:pachanga_manana_1
DTSTART;TZID=Europe/Madrid:20260111T190000
DTEND;TZID=Europe/Madrid:20260111T200000
SUMMARY:Pachanga F7
END:VEVENT
END:VCALENDAR"""

    def handler_ok(_peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ics_content)

    client_ok = httpx.Client(transport=httpx.MockTransport(handler_ok))
    c_ok = cliente_web(settings_con_url, hoy=hoy, cliente_calendario=client_ok)
    r_ok = c_ok.post("/calendario/sincronizar")
    assert r_ok.status_code == 303
    assert "Calendario al día: 1 deportes" in unquote_plus(r_ok.headers["location"])

    r_act = c_ok.get("/actividades")
    assert r_act.status_code == 200
    assert "Pachanga F7" in r_act.text or "Fútbol" in r_act.text

    def handler_err(_peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client_err = httpx.Client(transport=httpx.MockTransport(handler_err))
    c_err = cliente_web(settings_con_url, hoy=hoy, cliente_calendario=client_err)
    r_err = c_err.post("/calendario/sincronizar")
    assert r_err.status_code == 303
    loc = unquote_plus(r_err.headers["location"])
    assert "HTTP 500" in loc or "error" in loc.lower()
    assert secret_url not in loc
    assert "private-secret-token" not in loc


def test_semana_tipo(settings):
    hoy = date(2026, 1, 10)
    c = cliente_web(settings, hoy=hoy)

    r_vacio = c.get("/actividades/semana-tipo")
    assert r_vacio.status_code == 200
    assert "Semana tipo" in r_vacio.text

    # GET ?proponer=1 con actividades en 3 semanas
    con = db.conectar(settings.db_path)
    a1 = Actividad(
        fuente="garmin",
        id_fuente="1",
        inicio="2026-01-06 18:00:00",
        tipo="cycling",
        nombre="Salida",
        bruto="{}",
        duracion_s=3600.0,
    )
    a2 = Actividad(
        fuente="garmin",
        id_fuente="2",
        inicio="2025-12-30 18:00:00",
        tipo="cycling",
        nombre="Salida",
        bruto="{}",
        duracion_s=3600.0,
    )
    a3 = Actividad(
        fuente="garmin",
        id_fuente="3",
        inicio="2025-12-23 18:00:00",
        tipo="cycling",
        nombre="Salida",
        bruto="{}",
        duracion_s=3600.0,
    )
    db.guardar_actividades(con, [a1, a2, a3])
    con.close()

    r_prop = c.get("/actividades/semana-tipo?proponer=1")
    assert r_prop.status_code == 200
    assert "Propuesta a partir de tus actividades" in r_prop.text
    assert 'value="cycling"' in r_prop.text
    con = db.conectar(settings.db_path)
    assert len(db.semana_tipo(con)) == 0
    con.close()

    # POST con 2 filas válidas, 1 con quitar y 1 vacía -> se guardan 2
    datos_post = {
        "filas": "4",
        "dia-0": "1",
        "inicio-0": "18:00",
        "tipo-0": "cycling",
        "duracion-0": "60",
        "intensidad-0": "moderada",
        "dia-1": "3",
        "inicio-1": "20:00",
        "tipo-1": "soccer",
        "duracion-1": "90",
        "intensidad-1": "alta",
        "dia-2": "4",
        "inicio-2": "19:00",
        "tipo-2": "running",
        "duracion-2": "45",
        "intensidad-2": "moderada",
        "quitar-2": "on",
        "dia-3": "5",
        "inicio-3": "",
        "tipo-3": "",
        "duracion-3": "",
        "intensidad-3": "moderada",
    }
    r_post = c.post("/actividades/semana-tipo", data=datos_post)
    assert r_post.status_code == 303
    con = db.conectar(settings.db_path)
    guardadas = db.semana_tipo(con)
    assert len(guardadas) == 2
    assert guardadas[0].dia == 1
    assert guardadas[0].tipo == "cycling"
    assert guardadas[1].dia == 3
    assert guardadas[1].tipo == "soccer"
    con.close()

    # entradas_semana con hora "25:00", duración 3, tipo desconocido y filas = "x" -> ValueError
    with pytest.raises(ValueError, match="hora no válida"):
        entradas_semana(
            {
                "filas": "1",
                "dia-0": "0",
                "inicio-0": "25:00",
                "tipo-0": "soccer",
                "intensidad-0": "alta",
            }
        )

    with pytest.raises(ValueError, match="duración no válida"):
        entradas_semana(
            {
                "filas": "1",
                "dia-0": "0",
                "tipo-0": "soccer",
                "duracion-0": "3",
                "intensidad-0": "alta",
            }
        )

    with pytest.raises(ValueError, match="tipo no válido"):
        entradas_semana(
            {
                "filas": "1",
                "dia-0": "0",
                "tipo-0": "tipo_desconocido",
                "intensidad-0": "alta",
            }
        )

    with pytest.raises(ValueError, match="Formulario no válido"):
        entradas_semana({"filas": "x"})


def test_actualizar_tipo_titulo_db():
    con = db.conectar(":memory:")
    p1 = db.Planificado(
        fecha="2026-01-10",
        inicio="19:00",
        duracion_min=60,
        titulo="Pachanga F7",
        tipo=None,
        intensidad="moderada",
        origen="google",
        uid="u1",
    )
    p2 = db.Planificado(
        fecha="2026-01-11",
        inicio="20:00",
        duracion_min=60,
        titulo="Pachanga f-7",
        tipo=None,
        intensidad="moderada",
        origen="google",
        uid="u2",
    )
    p3 = db.Planificado(
        fecha="2026-01-12",
        inicio="10:00",
        duracion_min=90,
        titulo="Pachanga F7",
        tipo=None,
        intensidad="moderada",
        origen="semana-tipo",
        uid="",
    )
    db.reemplazar_planificado(
        con, origen="google", desde="2026-01-10", hasta="2026-01-11", filas=[p1, p2]
    )
    db.reemplazar_planificado(
        con, origen="semana-tipo", desde="2026-01-12", hasta="2026-01-12", filas=[p3]
    )

    n = db.actualizar_tipo_titulo(con, titulo="pachanga f7", tipo="soccer", intensidad="alta")
    assert n == 1
    filas = db.planificado(con, "2026-01-10", "2026-01-12")
    google_p1 = next(f for f in filas if f.uid == "u1")
    assert google_p1.tipo == "soccer"
    assert google_p1.intensidad == "alta"
    sem_p3 = next(f for f in filas if f.origen == "semana-tipo")
    assert sem_p3.tipo is None
