"""Tests para salud/calendario.py. Todos los datos son inventados y sin red."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import httpx
import pytest

from salud import calendario, db, metricas
from salud.cli import main


def test_normalizar_titulo():
    assert db.normalizar_titulo("  Pádel  Chuen ") == "padel chuen"
    assert db.normalizar_titulo("FÚTBOL") == "futbol"


def test_clasificar():
    marcados = {
        "partido de lucia": (False, None),
        "entreno especial": (True, "strength_training"),
    }
    assert calendario.clasificar("padelchuen", marcados) == (True, "paddelball")
    assert calendario.clasificar("Fútbol 7", marcados) == (True, "soccer")
    assert calendario.clasificar("Partido", marcados) == (True, None)
    assert calendario.clasificar("Cumple de Ana", marcados) == (False, None)
    # Título marcado como no deporte manda
    assert calendario.clasificar("Partido de Lucía", marcados) == (False, None)
    # Título marcado como deporte con tipo manda
    assert calendario.clasificar("Entreno Especial", marcados) == (True, "strength_training")
    # Casos de pachanga y palabras completas (F5, F7, F11)
    assert calendario.clasificar("Pachanga F7", marcados) == (True, "soccer")
    assert calendario.clasificar("F7 La Carisa", marcados) == (True, "soccer")
    assert calendario.clasificar("Buff7x", marcados) == (False, None)
    assert calendario.clasificar("Partido f-7", marcados) == (True, None)


def test_eventos():
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:madrid1
DTSTART;TZID=Europe/Madrid:20260504T190000
DTEND;TZID=Europe/Madrid:20260504T203000
SUMMARY:Futbol Madrid
END:VEVENT
BEGIN:VEVENT
UID:utc1
DTSTART:20260505T180000Z
DTEND:20260505T190000Z
SUMMARY:Tenis UTC
END:VEVENT
BEGIN:VEVENT
UID:allday1
DTSTART;VALUE=DATE:20260506
SUMMARY:Torneo Todo Dia
END:VEVENT
BEGIN:VEVENT
UID:recur1
DTSTART;TZID=Europe/Madrid:20260504T100000
DURATION:PT1H
RRULE:FREQ=WEEKLY
SUMMARY:Padel Recurrente
END:VEVENT
BEGIN:VEVENT
UID:fuera1
DTSTART;TZID=Europe/Madrid:20260601T100000
DURATION:PT1H
SUMMARY:Fuera de rango
END:VEVENT
END:VCALENDAR"""

    evs = calendario.eventos(ics, date(2026, 5, 1), date(2026, 5, 20))
    # (a) Evento con TZID Europe/Madrid
    madrid = next(e for e in evs if e.titulo == "Futbol Madrid")
    assert madrid.fecha == "2026-05-04"
    assert madrid.inicio == "19:00"
    assert madrid.duracion_min == 90

    # (b) Evento en UTC ("...Z"): en Madrid (CEST, UTC+2) son las 20:00
    utc = next(e for e in evs if e.titulo == "Tenis UTC")
    assert utc.fecha == "2026-05-05"
    assert utc.inicio == "20:00"
    assert utc.duracion_min == 60

    # (c) Evento de todo el día
    allday = next(e for e in evs if e.titulo == "Torneo Todo Dia")
    assert allday.fecha == "2026-05-06"
    assert allday.inicio is None
    assert allday.duracion_min is None

    # (d) Evento recurrente semanal: 3 ocurrencias en la ventana con UIDs distintos
    recurrentes = [e for e in evs if e.titulo == "Padel Recurrente"]
    assert len(recurrentes) == 3
    assert len({e.uid for e in recurrentes}) == 3
    assert [e.fecha for e in recurrentes] == ["2026-05-04", "2026-05-11", "2026-05-18"]

    # (e) Fuera de rango no aparece
    assert not any(e.titulo == "Fuera de rango" for e in evs)

    # ICS roto lanza ErrorCalendario
    with pytest.raises(calendario.ErrorCalendario, match="no tiene un formato válido"):
        calendario.eventos(b"roto", date(2026, 5, 1), date(2026, 5, 20))


def test_descargar_ics():
    url = "https://calendar.google.com/calendar/ical/secreto123/basic.ics"

    # 200 OK -> devuelve bytes
    transport_ok = httpx.MockTransport(lambda _req: httpx.Response(200, content=b"BEGIN:VCALENDAR"))
    with httpx.Client(transport=transport_ok) as client:
        res = calendario.descargar_ics(url, cliente=client)
        assert res == b"BEGIN:VCALENDAR"

    # 404 -> ErrorCalendario sin la URL en el mensaje
    transport_404 = httpx.MockTransport(lambda _req: httpx.Response(404))
    with httpx.Client(transport=transport_404) as client:
        with pytest.raises(calendario.ErrorCalendario) as exc_info:
            calendario.descargar_ics(url, cliente=client)
        assert "secreto123" not in str(exc_info.value)
        assert "HTTP 404" in str(exc_info.value)

    # Excepción de red -> ErrorCalendario sin la URL en el mensaje
    def _err(_req):
        raise httpx.ConnectError("Connection refused")

    transport_err = httpx.MockTransport(_err)
    with httpx.Client(transport=transport_err) as client:
        with pytest.raises(calendario.ErrorCalendario) as exc_info:
            calendario.descargar_ics(url, cliente=client)
        assert "secreto123" not in str(exc_info.value)
        assert "ConnectError" in str(exc_info.value)


def test_sincronizar_calendario(con):
    ics_1 = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:padel1
DTSTART;TZID=Europe/Madrid:20260504T190000
DTEND;TZID=Europe/Madrid:20260504T203000
SUMMARY:Padel Chuen
END:VEVENT
BEGIN:VEVENT
UID:futbol1
DTSTART;TZID=Europe/Madrid:20260505T200000
SUMMARY:Futbol 7
END:VEVENT
BEGIN:VEVENT
UID:cumple1
DTSTART;TZID=Europe/Madrid:20260506T140000
SUMMARY:Cumple de Ana
END:VEVENT
END:VCALENDAR"""

    # Insertamos actividades de fútbol para duración típica
    act = metricas.Actividad(
        "garmin",
        "act_1",
        "2026-05-01 19:00:00",
        "soccer",
        "Partido",
        "{}",
        duracion_s=4200,  # 70 min
    )
    db.guardar_actividades(con, [act])

    url = "https://calendar.google.com/calendar/ical/privado/basic.ics"
    hoy = date(2026, 5, 4)

    transport_1 = httpx.MockTransport(lambda _req: httpx.Response(200, content=ics_1))
    with httpx.Client(transport=transport_1) as client:
        resultado = calendario.sincronizar_calendario(con, url, hoy, cliente=client)

    assert resultado.deportes == 2
    assert resultado.descartados == 1

    planificados = db.planificado(con, "2026-05-01", "2026-05-10")
    titulos = [p.titulo for p in planificados]
    assert "Padel Chuen" in titulos
    assert "Futbol 7" in titulos
    # El no deporte no se guarda
    assert "Cumple de Ana" not in titulos

    futbol = next(p for p in planificados if p.titulo == "Futbol 7")
    # Al no tener DTEND, toma la duración típica de Garmin (70 min)
    assert futbol.duracion_min == 70

    # Comprobamos sincronización registrada
    s = db.ultima_sincronizacion(con, "google")
    assert s is not None
    assert s["estado"] == "ok"
    assert "2 deportes, 1 descartados" in s["detalle"]

    # Segunda sincronización: el fútbol ya no está en el ICS
    ics_2 = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:padel1
DTSTART;TZID=Europe/Madrid:20260504T190000
DTEND;TZID=Europe/Madrid:20260504T203000
SUMMARY:Padel Chuen
END:VEVENT
END:VCALENDAR"""

    transport_2 = httpx.MockTransport(lambda _req: httpx.Response(200, content=ics_2))
    with httpx.Client(transport=transport_2) as client:
        calendario.sincronizar_calendario(con, url, hoy, cliente=client)

    planificados_2 = db.planificado(con, "2026-05-01", "2026-05-10")
    assert [p.titulo for p in planificados_2] == ["Padel Chuen"]

    # Error de descarga: estado "error" y lo anterior sigue
    transport_err = httpx.MockTransport(lambda _req: httpx.Response(500))
    with httpx.Client(transport=transport_err) as client:
        with pytest.raises(calendario.ErrorCalendario):
            calendario.sincronizar_calendario(con, url, hoy, cliente=client)

    s_err = db.ultima_sincronizacion(con, "google")
    assert s_err is not None
    assert s_err["estado"] == "error"
    # Lo planificado no fue borrado
    assert [p.titulo for p in db.planificado(con, "2026-05-01", "2026-05-10")] == ["Padel Chuen"]


def test_proponer_semana_tipo(con):
    # Martes: fútbol 19:05 / 19:10 / 18:55 en 3 semanas distintas
    # 2026-05-05, 2026-05-12, 2026-05-19 son martes
    acts = [
        metricas.Actividad(
            "garmin", "f1", "2026-05-05 19:05:00", "soccer", "Futbol 1", "{}", duracion_s=3600
        ),
        metricas.Actividad(
            "garmin", "f2", "2026-05-12 19:10:00", "soccer", "Futbol 2", "{}", duracion_s=3600
        ),
        metricas.Actividad(
            "garmin", "f3", "2026-05-19 18:55:00", "soccer", "Futbol 3", "{}", duracion_s=3600
        ),
        # Pádel un solo día: 2026-05-06 (miércoles)
        metricas.Actividad(
            "garmin", "p1", "2026-05-06 18:00:00", "paddelball", "Padel", "{}", duracion_s=5400
        ),
    ]
    db.guardar_actividades(con, acts)

    entradas = calendario.proponer_semana_tipo(con, hasta=date(2026, 5, 20), semanas=8)
    assert len(entradas) == 1
    entrada = entradas[0]
    assert entrada.dia == 1  # martes
    assert entrada.inicio == "19:00"
    assert entrada.tipo == "soccer"
    assert entrada.intensidad == "alta"
    assert entrada.duracion_min == 60


def test_plan(con):
    # Semana tipo: martes fútbol 19:00, jueves bici 18:00
    st1 = db.EntradaSemana(dia=1, inicio="19:00", duracion_min=60, tipo="soccer", intensidad="alta")
    st2 = db.EntradaSemana(
        dia=3, inicio="18:00", duracion_min=90, tipo="cycling", intensidad="moderada"
    )
    db.guardar_semana_tipo(con, [st1, st2])

    # En google:
    # 1) Día pasado con google: martes 2026-05-05 pádel
    # 2) Día futuro con google: martes 2026-05-19 tenis
    g_pasado = db.Planificado(
        fecha="2026-05-05",
        inicio="19:30",
        duracion_min=90,
        titulo="Pádel Pasado",
        tipo="paddelball",
        intensidad="alta",
        origen="google",
        uid="g1",
    )
    g_futuro = db.Planificado(
        fecha="2026-05-19",
        inicio="10:00",
        duracion_min=60,
        titulo="Tenis Futuro",
        tipo="tennis",
        intensidad="alta",
        origen="google",
        uid="g2",
    )
    db.reemplazar_planificado(
        con, origen="google", desde="2026-05-01", hasta="2026-05-25", filas=[g_pasado, g_futuro]
    )

    hoy = date(2026, 5, 12)  # martes
    # Rango de consulta: 2026-05-01 al 2026-05-25
    resultado = calendario.plan(con, desde=date(2026, 5, 1), hasta=date(2026, 5, 25), hoy=hoy)

    por_fecha: dict[str, list[db.Planificado]] = {}
    for p in resultado:
        por_fecha.setdefault(p.fecha, []).append(p)

    # 1. Día pasado con google (2026-05-05): solo google
    assert len(por_fecha["2026-05-05"]) == 1
    assert por_fecha["2026-05-05"][0].origen == "google"
    assert por_fecha["2026-05-05"][0].titulo == "Pádel Pasado"

    # 2. Día pasado sin google (ej. jueves 2026-05-07): NO recibe semana tipo (es < hoy)
    assert "2026-05-07" not in por_fecha

    # 3. Día futuro (>= hoy) sin google (martes 2026-05-12 y jueves 2026-05-14): recibe semana tipo
    assert "2026-05-12" in por_fecha
    assert por_fecha["2026-05-12"][0].origen == "semana-tipo"
    assert por_fecha["2026-05-12"][0].titulo == "Fútbol"

    assert "2026-05-14" in por_fecha
    assert por_fecha["2026-05-14"][0].origen == "semana-tipo"
    assert por_fecha["2026-05-14"][0].titulo == "Ciclismo"

    # 4. Día futuro con google (martes 2026-05-19): solo google, no añade semana tipo
    assert len(por_fecha["2026-05-19"]) == 1
    assert por_fecha["2026-05-19"][0].origen == "google"
    assert por_fecha["2026-05-19"][0].titulo == "Tenis Futuro"


def test_cli_calendario_sin_gcal_ics(monkeypatch, capsys):
    monkeypatch.delenv("SALUD_GCAL_ICS", raising=False)
    # Sin SALUD_GCAL_ICS debe salir con 2 e imprimir "Falta SALUD_GCAL_ICS en el .env"
    codigo = main(["calendario"])
    assert codigo == 2
    captura = capsys.readouterr()
    assert "Falta SALUD_GCAL_ICS en el .env" in (captura.out + captura.err)


def test_cli_calendario_exito_y_error(monkeypatch, tmp_path, capsys):
    db_file = tmp_path / "salud.db"
    con = db.conectar(db_file)
    con.close()

    monkeypatch.setenv("SALUD_DB_PATH", str(db_file))
    monkeypatch.setenv("SALUD_GCAL_ICS", "https://example.com/privado.ics")

    # Éxito
    with patch("salud.calendario.sincronizar_calendario") as mock_sync:
        mock_sync.return_value = calendario.ResultadoCalendario(deportes=3, descartados=2)
        codigo = main(["calendario"])
        assert codigo == 0
        captura = capsys.readouterr()
        assert "Calendario: 3 deportes (2 eventos descartados)" in captura.out

    # ErrorCalendario
    with patch("salud.calendario.sincronizar_calendario") as mock_sync:
        mock_sync.side_effect = calendario.ErrorCalendario(
            "No se pudo descargar el calendario (HTTP 404)"
        )
        codigo = main(["calendario"])
        assert codigo == 1
        captura = capsys.readouterr()
        assert "HTTP 404" in captura.err


def test_cli_cmd_sync_incluye_calendario(monkeypatch, tmp_path, capsys):
    db_file = tmp_path / "salud.db"
    con = db.conectar(db_file)
    con.close()

    monkeypatch.setenv("SALUD_DB_PATH", str(db_file))
    monkeypatch.setenv("SALUD_GCAL_ICS", "https://example.com/privado.ics")

    with (
        patch("salud.cli._sincronizar", return_value=0),
        patch("salud.calendario.sincronizar_calendario") as mock_cal,
    ):
        mock_cal.return_value = calendario.ResultadoCalendario(deportes=1, descartados=0)
        codigo = main(["sync", "--dias", "1"])
        assert codigo == 0
        captura = capsys.readouterr()
        assert "Calendario: 1 deportes (0 eventos descartados)" in captura.out

    # Si calendario falla, la salida sigue siendo la de Garmin pero avisa por stderr
    with (
        patch("salud.cli._sincronizar", return_value=0),
        patch("salud.calendario.sincronizar_calendario") as mock_cal,
    ):
        mock_cal.side_effect = calendario.ErrorCalendario("Fallo descarga")
        codigo = main(["sync", "--dias", "1"])
        assert codigo == 0
        captura = capsys.readouterr()
        assert "Fallo descarga" in captura.err
