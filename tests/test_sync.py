from datetime import date

from conftest import GarminFalso, hay

from salud import db, sync


def test_trae_cada_dia_y_lo_del_rango(con, garmin):
    r = sync.sincronizar(con, garmin, date(2026, 1, 9), date(2026, 1, 10), pausa=0)
    assert r.estado == "ok"
    assert r.actividades == 2
    assert garmin.llamadas.count("resumen") == 2
    assert garmin.llamadas.count("composicion") == 1
    dia = db.dia(con, "2026-01-10")
    assert dia["pasos"].valor == 9000
    assert dia["vfc_noche"].valor == 60
    assert dia["peso"].valor == 80.1
    assert dia["estado_entreno"].texto == "PRODUCTIVE_3"
    assert hay(db.ultimo(con, "ftp", "2026-01-10")).valor == 230
    assert hay(db.ultimo(con, "vo2max", "2026-01-10")).fecha == "2026-01-03"
    crudo = con.execute(
        "SELECT COUNT(*) FROM crudos WHERE tipo = 'sueno' AND fecha = '2026-01-10'"
    ).fetchone()[0]
    assert crudo == 1
    assert hay(db.ultima_sincronizacion(con, "garmin"))["estado"] == "ok"


def test_un_fallo_no_para_el_resto(con):
    garmin = GarminFalso(fallar=("vfc",))
    r = sync.sincronizar(con, garmin, date(2026, 1, 10), date(2026, 1, 10), pausa=0)
    assert r.estado == "parcial"
    assert len(r.errores) == 1 and r.errores[0].startswith("vfc 2026-01-10")
    assert "vfc_noche" not in db.dia(con, "2026-01-10")
    assert db.dia(con, "2026-01-10")["pasos"].valor == 9000
    assert "fallo inventado" in hay(db.ultima_sincronizacion(con, "garmin"))["detalle"]


def test_si_falla_todo_es_error(con):
    r = sync.sincronizar(
        con, GarminFalso(fallar=("todo",)), date(2026, 1, 10), date(2026, 1, 10), pausa=0
    )
    assert r.estado == "error"
    assert r.metricas == 0


def test_repetir_la_sincronizacion_no_duplica(con, garmin):
    for _ in range(2):
        sync.sincronizar(con, garmin, date(2026, 1, 10), date(2026, 1, 10), pausa=0)
    assert con.execute("SELECT COUNT(*) FROM actividades").fetchone()[0] == 2
    filas = con.execute(
        "SELECT COUNT(*) FROM metricas WHERE clave = 'pasos' AND fecha = '2026-01-10'"
    ).fetchone()[0]
    assert filas == 1
