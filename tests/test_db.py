from conftest import hay

from salud import db, metricas
from salud.metricas import Metrica


def test_manda_la_fuente_con_mas_prioridad(con):
    db.guardar_metricas(
        con,
        [
            Metrica("2026-01-10", "peso", "samsung", 81.0),
            Metrica("2026-01-10", "peso", "garmin", 80.5),
            Metrica("2026-01-11", "peso", "samsung", 80.9),
        ],
    )
    assert db.dia(con, "2026-01-10")["peso"].fuente == "garmin"
    serie = db.serie(con, "peso", "2026-01-01", "2026-01-31")
    assert [(v.fecha, v.valor, v.fuente) for v in serie] == [
        ("2026-01-10", 80.5, "garmin"),
        ("2026-01-11", 80.9, "samsung"),
    ]
    db.guardar_metricas(con, [Metrica("2026-01-10", "peso", "manual", 80.0)])
    assert db.dia(con, "2026-01-10")["peso"].valor == 80.0


def test_ultimo_y_anterior(con):
    db.guardar_metricas(
        con,
        [
            Metrica("2026-01-03", "ftp", "garmin", 220),
            Metrica("2026-01-20", "ftp", "garmin", 230),
        ],
    )
    assert hay(db.ultimo(con, "ftp", "2026-01-19")).valor == 220
    assert hay(db.ultimo(con, "ftp", "2026-02-01")).valor == 230
    assert db.ultimo(con, "ftp", "2026-01-01") is None
    assert hay(db.anterior(con, "ftp", "2026-01-20")).valor == 220


def test_reemplazar_una_metrica_y_una_actividad_no_duplica(con):
    db.guardar_metricas(con, [Metrica("2026-01-10", "pasos", "garmin", 100)])
    db.guardar_metricas(con, [Metrica("2026-01-10", "pasos", "garmin", 9000)])
    assert db.dia(con, "2026-01-10")["pasos"].valor == 9000

    a = metricas.Actividad(
        "garmin", "1", "2026-01-10 18:00:00", "cycling", "Rodaje", "{}", carga=50
    )
    db.guardar_actividades(con, [a])
    db.guardar_actividades(con, [a])
    filas = db.actividades(con, "2026-01-10", "2026-01-10")
    assert len(filas) == 1
    assert filas[0]["carga"] == 50


def test_lecturas(con):
    db.guardar_lectura(con, "2026-01-10", error="sin conexión")
    db.guardar_lectura(con, "2026-01-11", contenido={"titular": "Todo bien"}, modelo="sonnet")
    assert hay(db.lectura(con, "2026-01-10"))["estado"] == "error"
    assert hay(db.lectura(con, "2026-01-11"))["contenido"] == {"titular": "Todo bien"}
    assert [x["fecha"] for x in db.lecturas(con)] == ["2026-01-11", "2026-01-10"]
    assert db.lectura(con, "2026-01-12") is None
