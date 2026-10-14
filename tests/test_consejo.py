"""Tests de consejo.py y del panel de consejo en la web."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import consejo, db, ia
from salud.config import Settings
from salud.metricas import Actividad, Metrica
from salud.web import crear_app

ZONA = ZoneInfo("Europe/Madrid")


def test_contexto(con, settings: Settings):
    hoy = date(2026, 1, 10)
    ahora = datetime(2026, 1, 10, 18, 0, tzinfo=ZONA)

    # 28 días de basal para que objetivo_dia devuelva datos
    for i in range(1, 29):
        f = (hoy - timedelta(days=i)).isoformat()
        db.guardar_metricas(
            con,
            [
                Metrica(fecha=f, clave="kcal", fuente="garmin", valor=2500.0),
                Metrica(fecha=f, clave="kcal_activas", fuente="garmin", valor=500.0),
            ],
        )
    db.guardar_metricas(
        con,
        [Metrica(fecha=hoy.isoformat(), clave="peso", fuente="garmin", valor=75.0)],
    )

    # Comida con creado en UTC
    db.anadir_comida(
        con,
        fecha="2026-01-10",
        momento="comida",
        nombre="Arroz con pollo",
        cantidad=200.0,
        unidad="g",
        kcal=500.0,
        proteinas=40.0,
        hidratos=60.0,
        grasas=10.0,
        creado="2026-01-10T16:40:00+00:00",
    )

    # Planificado y actividad para probar 'hecho'
    db.reemplazar_planificado(
        con,
        origen="google",
        desde="2026-01-10",
        hasta="2026-01-11",
        filas=[
            db.Planificado(
                fecha="2026-01-10",
                inicio="20:00",
                duracion_min=90,
                titulo="Pádel",
                tipo="paddelball",
                intensidad="alta",
                origen="google",
                uid="p1",
            ),
            db.Planificado(
                fecha="2026-01-11",
                inicio="10:00",
                duracion_min=60,
                titulo="Carrera",
                tipo="running",
                intensidad="moderada",
                origen="google",
                uid="p2",
            ),
        ],
    )
    db.guardar_actividades(
        con,
        [
            Actividad(
                fuente="garmin",
                id_fuente="act-1",
                inicio="2026-01-10 19:00:00",
                tipo="paddelball",
                nombre="Pádel tarde",
                duracion_s=5400.0,
                kcal=600.0,
                bruto="{}",
            )
        ],
    )

    ctx = consejo.contexto(con, settings, ahora)

    # Comprobaciones
    assert ctx["ahora"] == "2026-01-10 18:00"
    assert ctx["dia_semana"] == "sábado"

    comidas = ctx["comido_hoy"]
    assert len(comidas) == 1
    assert comidas[0]["apuntado"] == "17:40"
    assert comidas[0]["nombre"] == "Arroz con pollo"

    assert ctx["objetivo_hoy"] is not None
    assert ctx["objetivo_hoy"]["kcal_minimo"] is None
    assert ctx["queda_hoy"] is not None
    obj_kcal = ctx["objetivo_hoy"]["total"]["kcal"]
    tot_kcal = ctx["totales_hoy"]["kcal"]
    assert ctx["queda_hoy"]["kcal"] == round(obj_kcal - tot_kcal, 1)

    plan = ctx["planificado"]
    assert len(plan) == 2
    padel = next(p for p in plan if p["deporte"] == "Pádel")
    assert padel["hecho"] is True
    carrera = next(p for p in plan if p["deporte"] == "Carrera")
    assert carrera["hecho"] is False

    # Serializable con json.dumps
    volcado = json.dumps(ctx, ensure_ascii=False)
    assert "Arroz con pollo" in volcado


def test_mensaje():
    ctx = {"ahora": "2026-01-10 18:00", "dia_semana": "sábado"}

    # Con nota
    msg = consejo.mensaje(ctx, nota="ceno fuera")
    assert consejo.INSTRUCCIONES in msg
    assert '"ahora": "2026-01-10 18:00"' in msg
    assert "Nota de la persona: ceno fuera" in msg

    # Sin nota
    msg_sin = consejo.mensaje(ctx, nota="   ")
    assert consejo.INSTRUCCIONES in msg_sin
    assert "Nota de la persona:" not in msg_sin


def test_interpretar():
    # Válido
    valido = {
        "resumen": "Come un plátano antes de jugar",
        "ahora": "1 plátano y agua",
        "antes": "300 ml de agua",
        "durante": "",
        "despues": "Cena con 30 g de proteína",
        "avisos": ["Bebe suficiente agua"],
    }
    res = consejo.interpretar(valido)
    assert res["resumen"] == "Come un plátano antes de jugar"
    assert res["ahora"] == "1 plátano y agua"
    assert res["avisos"] == ["Bebe suficiente agua"]

    # Falta "ahora" -> ErrorIA
    sin_ahora = dict(valido)
    sin_ahora["ahora"] = ""
    with pytest.raises(ia.ErrorIA, match="La IA no devolvió un consejo válido"):
        consejo.interpretar(sin_ahora)

    del sin_ahora["ahora"]
    with pytest.raises(ia.ErrorIA, match="La IA no devolvió un consejo válido"):
        consejo.interpretar(sin_ahora)

    # Texto de 1000 -> recortado a MAX_TEXTO
    largo = dict(valido)
    largo["resumen"] = "X" * 1000
    res_largo = consejo.interpretar(largo)
    assert len(res_largo["resumen"]) == consejo.MAX_TEXTO

    # 5 avisos -> 3
    cinco_avisos = dict(valido)
    cinco_avisos["avisos"] = ["A1", "A2", "A3", "A4", "A5"]
    res_cinco = consejo.interpretar(cinco_avisos)
    assert len(res_cinco["avisos"]) == 3
    assert res_cinco["avisos"] == ["A1", "A2", "A3"]

    # Aviso no str -> descartado
    raros = dict(valido)
    raros["avisos"] = [123, "Válido", None, "   ", "Otro válido"]
    res_raros = consejo.interpretar(raros)
    assert res_raros["avisos"] == ["Válido", "Otro válido"]


def test_pedir_consejo(con, settings: Settings):
    ahora = datetime(2026, 1, 10, 18, 0, tzinfo=ZONA)
    llamadas: list[ia.Tarea] = []

    def fake_ejecutar(tarea: ia.Tarea, _settings: Settings) -> dict[str, Any]:
        llamadas.append(tarea)
        return {
            "resumen": "Resumen de prueba",
            "ahora": "Toma algo ligero",
            "antes": "Agua",
            "durante": "",
            "despues": "Cena completa",
            "avisos": ["Aviso 1"],
        }

    res = consejo.pedir_consejo(
        con,
        settings,
        ahora,
        nota="  tengo poco tiempo  ",
        ejecutar=fake_ejecutar,
    )
    assert len(llamadas) == 1
    tarea = llamadas[0]
    assert tarea.modelo_agy == settings.ia_modelo_consejo
    assert tarea.esquema == consejo.ESQUEMA

    assert res["resumen"] == "Resumen de prueba"
    assert res["nota"] == "tengo poco tiempo"
    assert res["modelo"] == settings.ia_modelo_consejo

    guardado = db.ultimo_consejo(con, "2026-01-10")
    assert guardado is not None
    assert guardado["resumen"] == "Resumen de prueba"

    # Si lanza ErrorIA, no guarda nada
    def fake_error(_tarea: ia.Tarea, _settings: Settings) -> dict[str, Any]:
        raise ia.ErrorIA("Fallo del modelo")

    with pytest.raises(ia.ErrorIA, match="Fallo del modelo"):
        consejo.pedir_consejo(
            con,
            settings,
            datetime(2026, 1, 11, 10, 0, tzinfo=ZONA),
            ejecutar=fake_error,
        )
    assert db.ultimo_consejo(con, "2026-01-11") is None


def test_web_consejo(con, settings: Settings):
    hoy = date(2026, 1, 10)
    evento = threading.Event()

    def pedir_falso(conexion: Any, nota: str) -> dict[str, Any]:
        evento.wait(timeout=5)
        db.guardar_consejo(
            conexion,
            fecha="2026-01-10",
            nota=nota,
            contenido={
                "resumen": "Comer algo rico",
                "ahora": "Una manzana",
                "antes": "Agua fresca",
                "durante": "Nada",
                "despues": "Cena nutritiva",
                "avisos": ["Descansa"],
            },
            modelo="gemini-test",
            segundos=1.5,
        )
        return db.ultimo_consejo(conexion, "2026-01-10") or {}

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=GarminFalso,
        hoy=lambda: hoy,
        pedir_consejo=pedir_falso,
    )
    client = TestClient(app, follow_redirects=False)

    # POST responde 303 sin esperar al evento
    r_post = client.post("/comida/consejo", data={"nota": "ceno fuera"})
    assert r_post.status_code == 303
    loc = r_post.headers["location"]
    assert loc.startswith("/comida/consejo")
    assert "#consejo" not in loc

    # Con el evento sin soltar, GET /comida/consejo contiene data-esperando-consejo
    # y no el formulario action="/comida/consejo"
    r_esperando = client.get("/comida/consejo")
    assert r_esperando.status_code == 200
    assert "data-esperando-consejo" in r_esperando.text
    assert 'action="/comida/consejo"' not in r_esperando.text

    # /comida/consejo/estado da {"preparando": true}
    r_estado = client.get("/comida/consejo/estado")
    assert r_estado.status_code == 200
    assert r_estado.json() == {"preparando": True}

    # Un segundo POST redirige con "Ya estoy preparando el consejo"
    r_post2 = client.post("/comida/consejo", data={"nota": "otra nota"})
    assert r_post2.status_code == 303
    loc2 = r_post2.headers["location"]
    assert "Ya+estoy+preparando+el+consejo" in loc2 or "Ya estoy preparando el consejo" in loc2

    # Suelta el evento y espera a que /comida/consejo/estado dé false
    evento.set()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        if not client.get("/comida/consejo/estado").json()["preparando"]:
            break
        time.sleep(0.01)
    else:
        pytest.fail("El consejo no terminó a tiempo")

    # GET /comida/consejo muestra el consejo y el formulario
    r_listo = client.get("/comida/consejo")
    assert r_listo.status_code == 200
    assert 'action="/comida/consejo"' in r_listo.text
    assert "Pedir consejo" in r_listo.text
    assert "Consejo de las" in r_listo.text
    assert "Comer algo rico" in r_listo.text
    assert "Ahora" in r_listo.text
    assert "Antes" in r_listo.text
    assert "Durante" in r_listo.text
    assert "Después" in r_listo.text
    assert "Descansa" in r_listo.text
    assert "linea-tiempo" in r_listo.text

    # Falso que lanza ia.ErrorIA("Error simulado de IA")
    def pedir_error(_conexion: Any, _nota: str) -> dict[str, Any]:
        raise ia.ErrorIA("Error simulado de IA")

    app_err = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=GarminFalso,
        hoy=lambda: hoy,
        pedir_consejo=pedir_error,
    )
    client_err = TestClient(app_err, follow_redirects=False)
    r_post_err = client_err.post("/comida/consejo", data={"nota": ""})
    assert r_post_err.status_code == 303

    # Tras esperar a que termine
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        if not client_err.get("/comida/consejo/estado").json()["preparando"]:
            break
        time.sleep(0.01)
    else:
        pytest.fail("El consejo con error no terminó a tiempo")

    # GET /comida/consejo muestra "Error simulado de IA"; un segundo GET ya no lo muestra
    r_err1 = client_err.get("/comida/consejo")
    assert r_err1.status_code == 200
    assert "Error simulado de IA" in r_err1.text

    r_err2 = client_err.get("/comida/consejo")
    assert r_err2.status_code == 200
    assert "Error simulado de IA" not in r_err2.text

    # GET /comida/consejo de otro día -> no aparece el panel del consejo guardado
    r_otro = client.get("/comida/consejo?fecha=2026-01-09")
    assert r_otro.status_code == 200
    assert "Consejo de las" not in r_otro.text

    # Con planificado de hoy futuro -> "Próximo ejercicio"
    db.reemplazar_planificado(
        con,
        origen="google",
        desde="2026-01-10",
        hasta="2026-01-10",
        filas=[
            db.Planificado(
                fecha="2026-01-10",
                inicio="23:59",
                duracion_min=90,
                titulo="Pádel",
                tipo="paddelball",
                intensidad="alta",
                origen="google",
                uid="p_futuro",
            )
        ],
    )
    r_prox = client.get("/comida/consejo")
    assert r_prox.status_code == 200
    assert "Próximo ejercicio" in r_prox.text
    assert "Pádel hoy · 23:59" in r_prox.text
    assert "90 min · alta" in r_prox.text
