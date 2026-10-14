"""Sincronización de Garmin con la base de datos.

Por cada día pide el resumen, el sueño, la VFC, la preparación para
entrenar y el estado de entrenamiento; para todo el rango, los pesajes y las
actividades; y una vez, el FTP y el umbral. Guarda cada respuesta tal cual
(`crudos`) y las métricas que salen de ella. Garmin falla a ratos: un fallo
en una petición no para el resto, queda apuntado y la sincronización sale
"parcial".
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import db, metricas
from .garmin import FuenteGarmin
from .metricas import GARMIN, Metrica

# Pausa entre peticiones: Garmin corta (429) si se le pide mucho seguido.
PAUSA_S = 0.6

# (tipo de crudo, cómo pedirlo, cómo sacar las métricas)
_POR_DIA: tuple[
    tuple[str, Callable[[FuenteGarmin, str], Any], Callable[[Any], list[Metrica]]], ...
] = (
    ("resumen", lambda g, d: g.get_user_summary(d), metricas.de_resumen),
    ("sueno", lambda g, d: g.get_sleep_data(d), metricas.de_sueno),
    ("vfc", lambda g, d: g.get_hrv_data(d), metricas.de_vfc),
    ("preparacion", lambda g, d: g.get_morning_training_readiness(d), metricas.de_preparacion),
    ("estado_entreno", lambda g, d: g.get_training_status(d), metricas.de_estado_entreno),
)


@dataclass
class Resultado:
    """Qué se trajo y qué falló."""

    metricas: int = 0
    actividades: int = 0
    errores: list[str] = field(default_factory=list)

    @property
    def estado(self) -> str:
        """ "ok", "parcial" (algo falló) o "error" (no se trajo nada)."""
        if not self.errores:
            return "ok"
        return "parcial" if self.metricas or self.actividades else "error"


def dias(desde: date, hasta: date) -> Iterator[date]:
    """Los días de un rango, ambos incluidos."""
    d = desde
    while d <= hasta:
        yield d
        d += timedelta(days=1)


def sincronizar(
    con: sqlite3.Connection,
    garmin: FuenteGarmin,
    desde: date,
    hasta: date,
    *,
    pausa: float = PAUSA_S,
    avisar: Callable[[str], None] = lambda _: None,
) -> Resultado:
    """Trae de Garmin los días entre `desde` y `hasta` (incluidos) y los guarda."""
    r = Resultado()

    def pedir(tipo: str, fecha: str, llamada: Callable[[], Any]) -> Any:
        try:
            datos = llamada()
        except Exception as exc:  # la librería lanza varios tipos según el fallo
            r.errores.append(f"{tipo} {fecha}: {type(exc).__name__}: {exc}"[:300])
            return None
        finally:
            time.sleep(pausa)
        if datos:
            db.guardar_crudo(con, GARMIN, tipo, fecha, datos)
        return datos

    for d in dias(desde, hasta):
        fecha = d.isoformat()
        avisar(f"Garmin {fecha}")
        for tipo, llamada, extraer in _POR_DIA:
            datos = pedir(tipo, fecha, lambda: llamada(garmin, fecha))
            if datos:
                r.metricas += db.guardar_metricas(con, extraer(datos))

    inicio, fin = desde.isoformat(), hasta.isoformat()
    composicion = pedir("composicion", fin, lambda: garmin.get_body_composition(inicio, fin))
    if composicion:
        r.metricas += db.guardar_metricas(con, metricas.de_composicion(composicion))
    lista = pedir("actividades", fin, lambda: garmin.get_activities_by_date(inicio, fin))
    if lista:
        acts = [a for a in (metricas.de_actividad(x) for x in lista) if a is not None]
        r.actividades += db.guardar_actividades(con, acts)
    ftp = pedir("ftp", fin, garmin.get_cycling_ftp)
    if ftp:
        r.metricas += db.guardar_metricas(con, metricas.de_ftp(ftp))
    umbral = pedir("umbral", fin, garmin.get_lactate_threshold)
    if umbral:
        r.metricas += db.guardar_metricas(con, metricas.de_umbral(umbral, fin))

    db.registrar_sincronizacion(con, GARMIN, inicio, fin, r.estado, "\n".join(r.errores))
    return r
