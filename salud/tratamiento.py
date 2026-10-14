"""Lógica de tratamiento farmacológico, cálculo de próxima dosis y rotación de zonas."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, TypedDict

from . import db

ZONAS = (
    "muslo izquierdo",
    "muslo derecho",
    "abdomen izquierdo",
    "abdomen derecho",
    "brazo izquierdo",
    "brazo derecho",
)

_OPUESTOS = {
    "muslo izquierdo": "muslo derecho",
    "muslo derecho": "muslo izquierdo",
    "abdomen izquierdo": "abdomen derecho",
    "abdomen derecho": "abdomen izquierdo",
    "brazo izquierdo": "brazo derecho",
    "brazo derecho": "brazo izquierdo",
}

EstadoProxima = Literal["esperar", "puede_adelantar", "hoy", "retrasada", "saltar"]


@dataclass(frozen=True)
class Proxima:
    """Información y estado de la próxima dosis calculada a partir de la pauta y la última."""

    estado: EstadoProxima
    prevista: date
    adelantar_desde: date
    limite: date
    siguiente_habitual: date
    dias_desde_ultima: int
    zona_sugerida: str


class DosisArgs(TypedDict):
    """Argumentos validados para registrar una dosis."""

    fecha: str
    farmaco: str
    dosis_mg: float
    zona: str
    nota: str


def zona_sugerida(ultima_zona: str | None) -> str:
    """Zona sugerida para la próxima dosis: lado contrario o muslo derecho por defecto."""
    if not ultima_zona:
        return "muslo derecho"
    return _OPUESTOS.get(ultima_zona, "muslo derecho")


def proxima(pauta: db.Pauta, ultima: db.Dosis | None, hoy: date) -> Proxima | None:
    """Calcula el estado y fechas de la próxima dosis o None si no hay dosis registradas."""
    if ultima is None:
        return None

    u_fecha = date.fromisoformat(ultima.fecha)
    dias_desde_ultima = (hoy - u_fecha).days
    prevista = u_fecha + timedelta(days=pauta.intervalo_dias)
    adelantar_desde = u_fecha + timedelta(days=pauta.separacion_min_dias)
    limite = prevista + timedelta(days=pauta.margen_olvido_dias)

    sig = prevista
    while sig < hoy:
        sig += timedelta(days=pauta.intervalo_dias)
    siguiente_habitual = sig

    if hoy < adelantar_desde:
        estado: EstadoProxima = "esperar"
    elif hoy < prevista:
        estado = "puede_adelantar"
    elif hoy == prevista:
        estado = "hoy"
    elif hoy <= limite:
        estado = "retrasada"
    else:
        estado = "saltar"

    return Proxima(
        estado=estado,
        prevista=prevista,
        adelantar_desde=adelantar_desde,
        limite=limite,
        siguiente_habitual=siguiente_habitual,
        dias_desde_ultima=dias_desde_ultima,
        zona_sugerida=zona_sugerida(ultima.zona),
    )


def pauta_desde_formulario(datos: dict[str, str]) -> db.Pauta:
    """Valida los campos del formulario de pauta y construye el objeto Pauta."""
    farmaco = datos.get("farmaco", "").strip()
    if not (1 <= len(farmaco) <= 40):
        raise ValueError("El fármaco debe tener entre 1 y 40 caracteres")

    dosis_str = datos.get("dosis_mg", "").strip().replace(",", ".")
    try:
        dosis_mg = float(dosis_str)
    except (ValueError, TypeError):
        raise ValueError("La dosis debe ser un número mayor que 0 y hasta 50 mg") from None
    if not (0 < dosis_mg <= 50):
        raise ValueError("La dosis debe ser un número mayor que 0 y hasta 50 mg")

    int_str = datos.get("intervalo_dias", "").strip()
    try:
        intervalo = int(int_str)
    except (ValueError, TypeError):
        raise ValueError("El intervalo debe ser un número entero entre 1 y 60 días") from None
    if not (1 <= intervalo <= 60):
        raise ValueError("El intervalo debe ser un número entero entre 1 y 60 días")

    margen_str = datos.get("margen_olvido_dias", "").strip()
    try:
        margen = int(margen_str)
    except (ValueError, TypeError):
        raise ValueError(
            "El margen de olvido debe ser un número entero entre 0 y el intervalo"
        ) from None
    if not (0 <= margen <= intervalo):
        raise ValueError("El margen de olvido debe estar entre 0 y el intervalo de días")

    sep_str = datos.get("separacion_min_dias", "").strip()
    try:
        separacion = int(sep_str)
    except (ValueError, TypeError):
        raise ValueError(
            "La separación mínima debe ser un número entero entre 1 y el intervalo"
        ) from None
    if not (1 <= separacion <= intervalo):
        raise ValueError("La separación mínima debe estar entre 1 y el intervalo de días")

    prev_str = datos.get("dosis_previstas", "").strip()
    if prev_str:
        try:
            dosis_previstas: int | None = int(prev_str)
        except (ValueError, TypeError):
            raise ValueError("Las dosis previstas deben ser un número entero mayor que 0") from None
        if dosis_previstas <= 0:
            raise ValueError("Las dosis previstas deben ser un número entero mayor que 0")
    else:
        dosis_previstas = None

    return db.Pauta(
        farmaco=farmaco,
        dosis_mg=dosis_mg,
        intervalo_dias=intervalo,
        margen_olvido_dias=margen,
        separacion_min_dias=separacion,
        dosis_previstas=dosis_previstas,
    )


def dosis_desde_formulario(datos: dict[str, str], pauta: db.Pauta, hoy: date) -> DosisArgs:
    """Valida los campos de una dosis apuntada y devuelve los argumentos para anadir_dosis."""
    fecha_str = datos.get("fecha", "").strip()
    if not fecha_str:
        fecha_str = hoy.isoformat()
    else:
        try:
            f_date = date.fromisoformat(fecha_str)
        except ValueError:
            raise ValueError("La fecha debe tener formato AAAA-MM-DD") from None
        if f_date > hoy:
            raise ValueError("La fecha no puede ser posterior a hoy")

    dosis_str = datos.get("dosis_mg", "").strip().replace(",", ".")
    if not dosis_str:
        dosis_mg = pauta.dosis_mg
    else:
        try:
            dosis_mg = float(dosis_str)
        except (ValueError, TypeError):
            raise ValueError("La dosis debe ser un número positivo") from None
        if not (0 < dosis_mg <= 50):
            raise ValueError("La dosis debe ser mayor que 0 y hasta 50 mg")

    zona = datos.get("zona", "").strip()
    if zona not in ZONAS:
        raise ValueError("La zona de inyección no es válida")

    nota = datos.get("nota", "").strip()[:200]

    return {
        "fecha": fecha_str,
        "farmaco": pauta.farmaco,
        "dosis_mg": dosis_mg,
        "zona": zona,
        "nota": nota,
    }


def resumen(con: sqlite3.Connection, hoy: date) -> dict[str, Any] | None:
    """Resumen del tratamiento actual para el contexto de IA, o None si no hay dosis."""
    lista = db.dosis(con, limite=1)
    if not lista:
        return None
    ultima = lista[0]
    p = db.pauta(con)
    prox = proxima(p, ultima, hoy)
    if prox is None:
        return None
    dosis_hechas = db.contar_dosis(con, farmaco=p.farmaco, dosis_mg=p.dosis_mg)
    return {
        "farmaco": p.farmaco,
        "dosis_mg": p.dosis_mg,
        "intervalo_dias": p.intervalo_dias,
        "ultima_dosis": ultima.fecha,
        "dias_desde_ultima": prox.dias_desde_ultima,
        "estado": prox.estado,
        "prevista": prox.prevista.isoformat(),
        "dosis_hechas": dosis_hechas,
        "dosis_previstas": p.dosis_previstas,
    }
