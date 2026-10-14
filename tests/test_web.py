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
    db.guardar_semana_tipo(
        con,
        [
            db.EntradaSemana(
                dia=HOY.weekday(),
                inicio="20:00",
                duracion_min=90,
                tipo="paddelball",
                intensidad="alta",
            )
        ],
    )
    db.guardar_pauta(
        con,
        db.Pauta(
            farmaco="Mounjaro",
            dosis_mg=5.0,
            intervalo_dias=7,
            margen_olvido_dias=4,
            separacion_min_dias=3,
        ),
    )
    db.anadir_dosis(
        con, farmaco="Mounjaro", fecha=HOY.isoformat(), dosis_mg=5.0, zona="muslo derecho"
    )
    db.anadir_comida(
        con,
        fecha=HOY.isoformat(),
        momento="comida",
        nombre="Pechuga",
        cantidad=200,
        unidad="g",
        kcal=220,
        proteinas=46,
        hidratos=0,
        grasas=3,
    )
    con.close()
    r = cliente.get("/")
    assert r.status_code == 200
    assert "sábado 10 de enero" in r.text
    assert "Carga alta: hoy suave" in r.text and "Con cabeza" in r.text
    assert "Lectura completa" in r.text
    assert "FC en reposo alta" in r.text
    assert "7 h 30 min" in r.text  # sueño
    assert "Dentro de tu rango (50–70)" in r.text
    assert "80,1" in r.text  # peso
    assert 'aria-label="Sueño"' in r.text
    assert 'aria-label="VFC noche"' in r.text
    assert 'aria-label="Body Battery"' in r.text
    assert 'aria-label="Peso"' in r.text
    assert 'aria-label="FC en reposo"' in r.text
    assert "Hoy toca" in r.text and "Pádel · 20:00" in r.text
    assert "Mounjaro 5 mg" in r.text
    assert "Comida de hoy" in r.text and "g proteína" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_hoy_vacio_y_otro_dia(settings):
    c = TestClient(_app(settings))
    r = c.get("/")
    assert r.status_code == 200
    assert "Aún no hay lectura de hoy" in r.text
    assert "Preparar la lectura" in r.text
    assert "sin sincronizar todavía" in r.text
    r = c.get("/?fecha=2026-01-05")
    assert "lunes 5 de enero" in r.text and "No hubo lectura este día" in r.text
    assert "Preparar la lectura" not in r.text
    assert "Hoy toca" not in r.text
    assert "Comida de hoy" not in r.text
    # Una fecha futura se queda en hoy.
    assert "sábado 10 de enero" in c.get("/?fecha=2030-01-01").text


def test_salud_actividades_y_lecturas(cliente, settings):
    # Redirección 301 de /tendencias a /salud
    r_redir = cliente.get("/tendencias?dias=30", follow_redirects=False)
    assert r_redir.status_code == 301
    assert r_redir.headers["location"] == "/salud?dias=30"

    # /salud responde 200 con "Peso" y "Analíticas"
    r_salud = cliente.get("/salud?dias=30")
    assert r_salud.status_code == 200
    assert "Peso" in r_salud.text
    assert "Analíticas" in r_salud.text
    assert "Ver tabla" in r_salud.text

    # Tarjeta de tratamiento en /salud cuando hay una dosis
    con = db.conectar(settings.db_path)
    db.anadir_dosis(
        con, farmaco="Mounjaro", fecha=HOY.isoformat(), dosis_mg=5.0, zona="muslo derecho"
    )
    con.close()
    r_salud_dosis = cliente.get("/salud")
    assert r_salud_dosis.status_code == 200
    assert "Apuntar dosis" in r_salud_dosis.text

    # /actividades contiene "Rendimiento", "Productivo", "Últimos 7 días",
    # "VO2max", "Fútbol" y "Ciclismo"
    r_act = cliente.get("/actividades")
    assert r_act.status_code == 200
    assert "Rendimiento" in r_act.text
    assert "Productivo" in r_act.text
    assert "Últimos 7 días" in r_act.text
    assert "VO2max" in r_act.text
    assert "Fútbol" in r_act.text and "Ciclismo" in r_act.text

    # Lecturas
    assert "Todavía no hay lecturas" in cliente.get("/lecturas").text

    # Nav con 4 enlaces y aria-current en la sección correcta
    r_hoy = cliente.get("/")
    assert 'href="/"' in r_hoy.text
    assert 'href="/comida"' in r_hoy.text
    assert 'href="/actividades"' in r_hoy.text
    assert 'href="/salud"' in r_hoy.text

    # Hoy en /lecturas
    r_lec = cliente.get("/lecturas")
    assert '<a href="/" aria-current="page">' in r_lec.text

    # Salud en /tratamiento
    r_trat = cliente.get("/tratamiento")
    assert '<a href="/salud" aria-current="page">' in r_trat.text

    # Actividad en /actividades
    assert '<a href="/actividades" aria-current="page">' in r_act.text

    # Salud en /salud
    assert '<a href="/salud" aria-current="page">' in r_salud.text


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


def test_salud_rediseno_completo(cliente, settings):
    # 1. Sin pauta ni dosis: tarjeta de "Apuntar tratamiento" sin error
    r_vacio = cliente.get("/salud")
    assert r_vacio.status_code == 200
    assert "Apuntar tratamiento" in r_vacio.text

    # 2. Con datos de prueba
    con = db.conectar(settings.db_path)
    db.guardar_pauta(
        con,
        db.Pauta(
            farmaco="Mounjaro",
            dosis_mg=5.0,
            intervalo_dias=7,
            margen_olvido_dias=4,
            separacion_min_dias=3,
            dosis_previstas=8,
        ),
    )
    db.anadir_dosis(
        con,
        farmaco="Mounjaro",
        fecha="2026-01-03",
        dosis_mg=5.0,
        zona="muslo izquierdo",
    )
    db.anadir_dosis(
        con,
        farmaco="Mounjaro",
        fecha=HOY.isoformat(),
        dosis_mg=5.0,
        zona="muslo derecho",
    )
    db.guardar_ajustes_comida(con, db.AjustesComida(peso_objetivo_kg=85.0))
    con.close()

    r = cliente.get("/salud?dias=30")
    assert r.status_code == 200

    # Tira de días con 8 casillas y leyenda
    assert r.text.count('class="ciclo-columna"') == 8
    assert "Puesta" in r.text
    assert "Puedes ponerla" in r.text
    assert "Toca" in r.text

    # Esquema de zonas
    assert "esquema-zonas" in r.text
    assert "Última: muslo derecho" in r.text
    assert "Próxima: muslo izquierdo" in r.text

    # Dosis 2 de 8
    assert "dosis 2 de 8" in r.text

    # Objetivo 85 kg
    assert "Objetivo 85 kg" in r.text

    # La referencia y las marcas en el SVG del peso
    assert 'class="linea-referencia"' in r.text
    assert 'class="marca-linea"' in r.text

    # Los dos mosaicos
    assert "FC en reposo" in r.text
    assert "VFC semanal" in r.text
    assert 'href="#grafica-fc_reposo"' in r.text
    assert 'href="#grafica-vfc_noche"' in r.text

    # Tendencias
    assert "Tendencias" in r.text
    assert 'id="grafica-fc_reposo"' in r.text
    assert 'id="grafica-vfc_noche"' in r.text

    # En Hoy se ve la dosis en Hoy toca y en /tratamiento _tarjeta_tratamiento.html
    r_hoy = cliente.get("/")
    assert r_hoy.status_code == 200
    assert "Mounjaro 5 mg" in r_hoy.text
    r_trat = cliente.get("/tratamiento")
    assert r_trat.status_code == 200
    assert "tarjeta-tratamiento" in r_trat.text
