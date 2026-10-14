"""Calendario de lo planificado: Google Calendar por iCal y semana tipo.

Pelayo apunta sus deportes de equipo en un calendario de Google privado,
mezclados con eventos personales/familiares. La dirección iCal secreta
es privada (un secreto: nunca se escribe en el log, en errores ni en la base
de datos). Este módulo descarga el iCal, se queda solo con los eventos
de deporte y descarta el resto sin guardarlo. Para lo que no está en el
calendario (como bici o correr), propone una "semana tipo" a partir de
las actividades de Garmin de las últimas semanas.
"""

from __future__ import annotations

import math
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from . import db, metricas

ZONA = ZoneInfo("Europe/Madrid")
TIMEOUT = 15.0
DIAS_ATRAS = 28
DIAS_ADELANTE = 14
SEMANAS_TIPO = 8
DURACION_POR_DEFECTO_MIN = 60
GOOGLE = "google"
SEMANA_TIPO = "semana-tipo"

# (trozo de palabra normalizado, tipo de Garmin o None si no se sabe el deporte).
# Se busca como subcadena del título normalizado: "padelchuen" entra por "padel".
# Orden: la primera que encaja manda.
PALABRAS: tuple[tuple[str, str | None], ...] = (
    ("padel", "paddelball"),
    ("futbol", "soccer"),
    ("futbito", "soccer"),
    ("futsal", "soccer"),
    ("pachanga", "soccer"),
    ("tenis", "tennis"),
    ("correr", "running"),
    ("carrera", "running"),
    ("bici", "cycling"),
    ("ciclismo", "cycling"),
    ("gimnasio", "strength_training"),
    ("gym", "strength_training"),
    ("natacion", "lap_swimming"),
    ("nadar", "lap_swimming"),
    ("partido", None),
    ("entreno", None),
)

# Palabras que solo valen como palabra COMPLETA (no subcadena) por ser cortas.
PALABRAS_COMPLETAS: tuple[tuple[str, str | None], ...] = (
    ("f5", "soccer"),
    ("f7", "soccer"),
    ("f11", "soccer"),
)

INTENSIDAD_POR_TIPO = {
    "soccer": "alta",
    "paddelball": "alta",
    "tennis": "alta",
    "running": "moderada",
    "cycling": "moderada",
    "road_biking": "moderada",
    "mountain_biking": "moderada",
    "strength_training": "moderada",
    "lap_swimming": "moderada",
    "walking": "suave",
}
INTENSIDAD_POR_DEFECTO = "moderada"


class ErrorCalendario(Exception):
    """Error al descargar o procesar el calendario."""


@dataclass(frozen=True)
class Evento:
    """Evento extraído del calendario de Google."""

    uid: str  # UID del evento + "@" + fecha y hora de la ocurrencia (únicos en recurrentes)
    fecha: str
    inicio: str | None
    duracion_min: int | None
    titulo: str


@dataclass(frozen=True)
class ResultadoCalendario:
    """Resultado de la sincronización del calendario."""

    deportes: int
    descartados: int


@dataclass
class _Grupo:
    """Grupo de sesiones históricas para una combinación de día y tipo."""

    semanas: set[tuple[int, int]] = field(default_factory=set)
    inicios: list[int] = field(default_factory=list)
    duraciones: list[float] = field(default_factory=list)


def clasificar(
    titulo: str, marcados: dict[str, tuple[bool, str | None]]
) -> tuple[bool, str | None]:
    """Clasifica un evento según su título: si es deporte y qué tipo de Garmin es."""
    t_norm = db.normalizar_titulo(titulo)
    if t_norm in marcados:
        return marcados[t_norm]
    for trozo, tipo in PALABRAS:
        if trozo in t_norm:
            return True, tipo
    palabras_titulo = set(re.split(r"[^a-z0-9]+", t_norm))
    for palabra, tipo in PALABRAS_COMPLETAS:
        if palabra in palabras_titulo:
            return True, tipo
    return False, None


def intensidad(tipo: str | None) -> str:
    """Intensidad estimada para un tipo de deporte."""
    if tipo is not None and tipo in INTENSIDAD_POR_TIPO:
        return INTENSIDAD_POR_TIPO[tipo]
    return INTENSIDAD_POR_DEFECTO


def descargar_ics(url: str, cliente: httpx.Client | None = None) -> bytes:
    """Descarga el iCal desde la URL privada dada.

    Por seguridad, los mensajes de error nunca contienen la URL ni excepciones que la lleven.
    """
    try:
        if cliente is not None:
            resp = cliente.get(url, timeout=TIMEOUT)
        else:
            with httpx.Client(timeout=TIMEOUT) as c:
                resp = c.get(url)
        if resp.status_code != 200:
            raise ErrorCalendario(f"No se pudo descargar el calendario (HTTP {resp.status_code})")
        return resp.content
    except ErrorCalendario:
        raise
    except Exception as exc:
        raise ErrorCalendario(f"No se pudo descargar el calendario: {type(exc).__name__}") from None


def eventos(ics: bytes, desde: date, hasta: date) -> list[Evento]:
    """Extrae los eventos del iCal en la ventana entre `desde` y `hasta` (ambos incluidos)."""
    try:
        cal = icalendar.Calendar.from_ical(ics)
        componentes = recurring_ical_events.of(cal).between(desde, hasta + timedelta(days=1))
    except Exception:
        raise ErrorCalendario("El calendario no tiene un formato válido") from None

    originales_con_fin = {
        str(c.get("UID", ""))
        for c in cal.walk("vevent")
        if c.get("DTEND") is not None or c.get("DURATION") is not None
    }

    resultado: list[Evento] = []
    for comp in componentes:
        if comp.name != "VEVENT":
            continue

        raw_summary = comp.get("SUMMARY")
        titulo = str(raw_summary).strip() if raw_summary is not None else ""
        if not titulo:
            continue

        uid_base = str(comp.get("UID", ""))
        dtstart_prop = comp.get("DTSTART")
        if dtstart_prop is None:
            continue
        dtstart = dtstart_prop.dt

        if isinstance(dtstart, datetime):
            if dtstart.tzinfo is None:
                dt_madrid = dtstart.replace(tzinfo=ZONA)
            else:
                dt_madrid = dtstart.astimezone(ZONA)
            fecha = dt_madrid.date().isoformat()
            inicio = dt_madrid.strftime("%H:%M")
            ocurrencia = dt_madrid.isoformat()

            # Duración: solo si el evento original especificaba DTEND o DURATION
            if uid_base in originales_con_fin:
                dtend_prop = comp.get("DTEND")
                duration_prop = comp.get("DURATION")
                if dtend_prop is not None and isinstance(dtend_prop.dt, datetime):
                    dtend = dtend_prop.dt
                    if dtend.tzinfo is None:
                        dtend_madrid = dtend.replace(tzinfo=ZONA)
                    else:
                        dtend_madrid = dtend.astimezone(ZONA)
                    duracion_min = int((dtend_madrid - dt_madrid).total_seconds() / 60)
                elif duration_prop is not None and isinstance(duration_prop.dt, timedelta):
                    duracion_min = int(duration_prop.dt.total_seconds() / 60)
                else:
                    duracion_min = None
            else:
                duracion_min = None
        elif isinstance(dtstart, date):
            fecha = dtstart.isoformat()
            inicio = None
            duracion_min = None
            ocurrencia = fecha
        else:
            continue

        uid = f"{uid_base}@{ocurrencia}"
        resultado.append(
            Evento(
                uid=uid,
                fecha=fecha,
                inicio=inicio,
                duracion_min=duracion_min,
                titulo=titulo,
            )
        )

    return resultado


def duracion_tipica(
    con: sqlite3.Connection, tipo: str, hasta: date, semanas: int = SEMANAS_TIPO
) -> int | None:
    """Mediana de duración en minutos de actividades de ese tipo en las últimas semanas."""
    desde = hasta - timedelta(weeks=semanas) + timedelta(days=1)
    filas = con.execute(
        """
        SELECT duracion_s FROM actividades
        WHERE tipo = ? AND fecha >= ? AND fecha <= ? AND duracion_s IS NOT NULL
        """,
        (tipo, desde.isoformat(), hasta.isoformat()),
    ).fetchall()
    if not filas:
        return None
    minutos = [f["duracion_s"] / 60.0 for f in filas]
    return round(statistics.median(minutos))


def sincronizar_calendario(
    con: sqlite3.Connection,
    url: str,
    hoy: date,
    cliente: httpx.Client | None = None,
) -> ResultadoCalendario:
    """Descarga y sincroniza los eventos de deporte de Google Calendar en la base de datos."""
    desde = hoy - timedelta(days=DIAS_ATRAS)
    hasta = hoy + timedelta(days=DIAS_ADELANTE)
    desde_str = desde.isoformat()
    hasta_str = hasta.isoformat()

    try:
        ics = descargar_ics(url, cliente=cliente)
        evs = eventos(ics, desde, hasta)
    except ErrorCalendario as exc:
        db.registrar_sincronizacion(con, GOOGLE, desde_str, hasta_str, "error", str(exc))
        raise

    marcados = db.titulos_calendario(con)
    filas: list[db.Planificado] = []
    descartados = 0

    for ev in evs:
        es_deporte, tipo = clasificar(ev.titulo, marcados)
        if not es_deporte:
            descartados += 1
            continue

        if ev.inicio is None:
            dur = None
        else:
            if ev.duracion_min is not None:
                dur = ev.duracion_min
            else:
                dur = (
                    duracion_tipica(con, tipo, hoy) if tipo else None
                ) or DURACION_POR_DEFECTO_MIN

        filas.append(
            db.Planificado(
                fecha=ev.fecha,
                inicio=ev.inicio,
                duracion_min=dur,
                titulo=ev.titulo,
                tipo=tipo,
                intensidad=intensidad(tipo),
                origen=GOOGLE,
                uid=ev.uid,
            )
        )

    db.reemplazar_planificado(con, origen=GOOGLE, desde=desde_str, hasta=hasta_str, filas=filas)
    db.registrar_sincronizacion(
        con,
        GOOGLE,
        desde_str,
        hasta_str,
        "ok",
        f"{len(filas)} deportes, {descartados} descartados",
    )
    return ResultadoCalendario(deportes=len(filas), descartados=descartados)


def proponer_semana_tipo(
    con: sqlite3.Connection, hasta: date, semanas: int = SEMANAS_TIPO
) -> list[db.EntradaSemana]:
    """Propone una plantilla semanal a partir de los hábitos de Garmin de las últimas semanas."""
    desde = hasta - timedelta(weeks=semanas) + timedelta(days=1)
    filas = db.actividades(con, desde.isoformat(), hasta.isoformat())

    agrupados: dict[tuple[int, str], _Grupo] = {}
    for a in filas:
        f_obj = date.fromisoformat(a["fecha"])
        dia_sem = f_obj.weekday()
        semana_iso = f_obj.isocalendar()[:2]
        tipo = a["tipo"]
        clave = (dia_sem, tipo)

        if clave not in agrupados:
            agrupados[clave] = _Grupo()
        grupo = agrupados[clave]
        grupo.semanas.add(semana_iso)

        if a["inicio"] and len(a["inicio"]) >= 16:
            h_str, m_str = a["inicio"][11:16].split(":")
            grupo.inicios.append(int(h_str) * 60 + int(m_str))

        if a["duracion_s"] is not None:
            grupo.duraciones.append(a["duracion_s"] / 60.0)

    min_semanas = max(2, math.ceil(semanas / 4))
    entradas: list[db.EntradaSemana] = []

    for (dia_sem, tipo), grupo in agrupados.items():
        if len(grupo.semanas) < min_semanas:
            continue

        if grupo.inicios:
            med_ini = statistics.median(grupo.inicios)
            ini_red = round(med_ini / 15.0) * 15
            h = (int(ini_red) // 60) % 24
            m = int(ini_red) % 60
            inicio_str: str | None = f"{h:02d}:{m:02d}"
        else:
            inicio_str = None

        if grupo.duraciones:
            med_dur = statistics.median(grupo.duraciones)
            dur_red = int(round(med_dur / 5.0) * 5)
        else:
            dur_red = None

        entradas.append(
            db.EntradaSemana(
                dia=dia_sem,
                inicio=inicio_str,
                duracion_min=dur_red,
                tipo=tipo,
                intensidad=intensidad(tipo),
            )
        )

    entradas.sort(key=lambda e: (e.dia, 0 if e.inicio is None else 1, e.inicio or ""))
    return entradas


def plan(con: sqlite3.Connection, desde: date, hasta: date, hoy: date) -> list[db.Planificado]:
    """Combina los eventos de Google y la semana tipo para los días sin eventos de Google."""
    filas_db = db.planificado(con, desde.isoformat(), hasta.isoformat())
    dias_con_google = {f.fecha for f in filas_db if f.origen == GOOGLE}

    plantilla = db.semana_tipo(con)
    por_dia: dict[int, list[db.EntradaSemana]] = {}
    for e in plantilla:
        por_dia.setdefault(e.dia, []).append(e)

    resultado = list(filas_db)
    inicio_rango = max(desde, hoy)
    d = inicio_rango
    while d <= hasta:
        fecha_str = d.isoformat()
        if fecha_str not in dias_con_google:
            entradas_dia = por_dia.get(d.weekday(), [])
            for e in entradas_dia:
                resultado.append(
                    db.Planificado(
                        fecha=fecha_str,
                        inicio=e.inicio,
                        duracion_min=e.duracion_min,
                        titulo=metricas.nombre_tipo(e.tipo),
                        tipo=e.tipo,
                        intensidad=e.intensidad,
                        origen=SEMANA_TIPO,
                        uid="",
                    )
                )
        d += timedelta(days=1)

    resultado.sort(key=lambda p: (p.fecha, 0 if p.inicio is None else 1, p.inicio or ""))
    return resultado
