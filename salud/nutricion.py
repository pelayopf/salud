"""Cálculo dinámico de objetivos de nutrición y macros diarios.

Objetivo diario de calorías = gasto basal + actividad − déficit calórico.
- Basal: mediana de (kcal − kcal_activas) de los últimos 28 días completos.
- Actividad:
  - Día pasado con dato real: kcal_activas de ese día.
  - Hoy: max(kcal_activas hasta ahora, estimada con NEAT + actividades + planificados no hechos).
  - Futuro o sin datos: NEAT + kcal típica de lo planificado.
- Macros:
  - Proteínas y grasas por peso corporal (g/kg).
  - Hidratos de carbono completan las calorías restantes.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from . import calendario, db, metricas

DIAS_BASE = 28
DIAS_TIPICA = 90
KCAL_MIN = {"suave": 4, "moderada": 8, "alta": 10}


@dataclass(frozen=True)
class Macros:
    """Objetivo de nutrientes en gramos y calorías."""

    kcal: int
    proteinas: int
    hidratos: int
    grasas: int


@dataclass(frozen=True)
class ObjetivoDia:
    """Objetivo nutricional del día con su desglose y reparto por comida."""

    total: Macros
    por_momento: dict[str, Macros]
    desglose: list[tuple[str, int]]
    peso_kg: float


def basal(con: sqlite3.Connection, hoy: date) -> int | None:
    """Gasto basal: mediana de (kcal − kcal_activas) de los últimos 28 días completos."""
    desde = hoy - timedelta(days=DIAS_BASE)
    hasta = hoy - timedelta(days=1)
    series_datos = db.series(con, ["kcal", "kcal_activas"], desde.isoformat(), hasta.isoformat())

    kcal_por_fecha = {v.fecha: v.valor for v in series_datos["kcal"] if v.valor is not None}
    activas_por_fecha = {
        v.fecha: v.valor for v in series_datos["kcal_activas"] if v.valor is not None
    }

    difs = [
        kcal_por_fecha[f] - activas_por_fecha[f] for f in kcal_por_fecha if f in activas_por_fecha
    ]
    if not difs:
        return None
    return round(statistics.median(difs))


def neat(con: sqlite3.Connection, hoy: date) -> int:
    """Gasto en movimiento cotidiano (NEAT): mediana de (kcal_activas − actividades) de 28 días."""
    desde = hoy - timedelta(days=DIAS_BASE)
    hasta = hoy - timedelta(days=1)
    series_datos = db.series(con, ["kcal_activas"], desde.isoformat(), hasta.isoformat())

    activas_por_fecha = {
        v.fecha: v.valor for v in series_datos["kcal_activas"] if v.valor is not None
    }
    if not activas_por_fecha:
        return 0

    acts = db.actividades(con, desde.isoformat(), hasta.isoformat())
    kcal_acts_por_fecha: dict[str, float] = defaultdict(float)
    for a in acts:
        if a["kcal"] is not None:
            kcal_acts_por_fecha[a["fecha"]] += float(a["kcal"])

    difs = [max(0.0, activas_por_fecha[f] - kcal_acts_por_fecha[f]) for f in activas_por_fecha]
    if not difs:
        return 0
    return max(0, round(statistics.median(difs)))


def kcal_tipica(con: sqlite3.Connection, p: db.Planificado, hoy: date) -> int:
    """Calorías típicas de una sesión deportiva según el historial o su duración e intensidad."""
    if p.tipo is not None:
        desde = hoy - timedelta(days=DIAS_TIPICA)
        acts = db.actividades(con, desde.isoformat(), hoy.isoformat())
        kcals = [float(a["kcal"]) for a in acts if a["tipo"] == p.tipo and a["kcal"] is not None]
        if kcals:
            return round(statistics.median(kcals))

    duracion = p.duracion_min if p.duracion_min is not None else 60
    tasa = KCAL_MIN.get(p.intensidad, 8)
    return round(duracion * tasa)


def _nombre(p: db.Planificado) -> str:
    """Nombre para mostrar de una actividad planificada."""
    return metricas.nombre_tipo(p.tipo) if p.tipo else p.titulo


def _previstos(
    con: sqlite3.Connection, planificados: list[db.Planificado], hoy: date
) -> list[tuple[str, int]]:
    """Genera las líneas de desglose para actividades planificadas no hechas."""
    resultado: list[tuple[str, int]] = []
    for p in planificados:
        k = kcal_tipica(con, p, hoy)
        nombre = _nombre(p)
        etiqueta = f"{nombre} {p.inicio[:5]} (previsto)" if p.inicio else f"{nombre} (previsto)"
        resultado.append((etiqueta, k))
    return resultado


def objetivo_dia(
    con: sqlite3.Connection,
    fecha: date,
    hoy: date,
    ajustes: db.AjustesComida | None = None,
) -> ObjetivoDia | None:
    """Calcula el objetivo nutricional dinámico para un día o None si faltan datos de Garmin."""
    ajustes = ajustes or db.ajustes_comida(con)
    b = basal(con, hoy)
    if b is None:
        return None

    if ajustes.peso_kg is not None:
        peso = ajustes.peso_kg
    else:
        v_peso = db.ultimo(con, "peso", hoy.isoformat())
        if v_peso is None or v_peso.valor is None:
            return None
        peso = v_peso.valor

    peso_kg = float(peso)
    desglose: list[tuple[str, int]] = [("Basal (Garmin)", b)]

    if fecha < hoy:
        v_act = db.serie(con, "kcal_activas", fecha.isoformat(), fecha.isoformat())
        if v_act and v_act[0].valor is not None:
            kcal_act = round(v_act[0].valor)
            desglose.append(("Actividad (Garmin)", kcal_act))
            gasto_actividad = kcal_act
        else:
            neat_val = neat(con, hoy)
            desglose.append(("Día normal", neat_val))
            gasto_actividad = neat_val
            plan_items = calendario.plan(con, fecha, fecha, hoy)
            prev = _previstos(con, plan_items, hoy)
            desglose.extend(prev)
            gasto_actividad += sum(k for _, k in prev)
    elif fecha == hoy:
        v_act = db.serie(con, "kcal_activas", fecha.isoformat(), fecha.isoformat())
        real = round(v_act[0].valor) if (v_act and v_act[0].valor is not None) else 0

        neat_val = neat(con, hoy)
        acts_hoy = db.actividades(con, fecha.isoformat(), fecha.isoformat())
        plan_items = calendario.plan(con, fecha, fecha, hoy)

        plan_no_hechos: list[db.Planificado] = []
        for p in plan_items:
            hecho = (
                any(a["tipo"] == p.tipo for a in acts_hoy)
                if p.tipo is not None
                else len(acts_hoy) > 0
            )
            if not hecho:
                plan_no_hechos.append(p)

        prev = _previstos(con, plan_no_hechos, hoy)
        kcal_acts = [round(a["kcal"] or 0) for a in acts_hoy]
        estimada = neat_val + sum(kcal_acts) + sum(k for _, k in prev)

        if real > estimada:
            gasto_actividad = real
            desglose.append(("Actividad hasta ahora (Garmin)", real))
        else:
            gasto_actividad = estimada
            desglose.append(("Día normal", neat_val))
            for a in sorted(acts_hoy, key=lambda x: x["inicio"] or ""):
                nombre = metricas.nombre_tipo(a["tipo"])
                desglose.append((f"{nombre} (hecho)", round(a["kcal"] or 0)))
            desglose.extend(prev)
    else:
        neat_val = neat(con, hoy)
        desglose.append(("Día normal", neat_val))
        gasto_actividad = neat_val
        plan_items = calendario.plan(con, fecha, fecha, hoy)
        prev = _previstos(con, plan_items, hoy)
        desglose.extend(prev)
        gasto_actividad += sum(k for _, k in prev)

    desglose.append(("Déficit", -ajustes.deficit_kcal))

    kcal_sin_redondeo = b + gasto_actividad - ajustes.deficit_kcal
    kcal_total = int(round(kcal_sin_redondeo / 10.0) * 10)
    proteinas = round(peso_kg * ajustes.proteinas_g_kg)
    grasas = round(peso_kg * ajustes.grasas_g_kg)
    hidratos = max(0, round((kcal_total - 4 * proteinas - 9 * grasas) / 4))

    total = Macros(kcal=kcal_total, proteinas=proteinas, hidratos=hidratos, grasas=grasas)

    por_momento: dict[str, Macros] = {}
    for m, pct in ajustes.reparto.items():
        if pct > 0:
            por_momento[m] = Macros(
                kcal=round(total.kcal * pct / 100),
                proteinas=round(total.proteinas * pct / 100),
                hidratos=round(total.hidratos * pct / 100),
                grasas=round(total.grasas * pct / 100),
            )

    return ObjetivoDia(total=total, por_momento=por_momento, desglose=desglose, peso_kg=peso_kg)


def ajustes_desde_formulario(datos: dict[str, str]) -> db.AjustesComida:
    """Valida los campos del formulario de objetivos y construye el objeto AjustesComida."""
    def_str = datos.get("deficit_kcal", "").strip()
    try:
        deficit = int(def_str)
    except (ValueError, TypeError):
        raise ValueError("El déficit debe ser un número entero entre -500 y 1000 kcal") from None
    if not (-500 <= deficit <= 1000):
        raise ValueError("El déficit debe ser un número entero entre -500 y 1000 kcal")

    prot_str = datos.get("proteinas_g_kg", "").strip().replace(",", ".")
    try:
        prot = float(prot_str)
    except (ValueError, TypeError):
        raise ValueError("Las proteínas deben ser un número entre 0,8 y 3,0 g/kg") from None
    if not (0.8 <= prot <= 3.0):
        raise ValueError("Las proteínas deben estar entre 0,8 y 3,0 g/kg")

    gras_str = datos.get("grasas_g_kg", "").strip().replace(",", ".")
    try:
        gras = float(gras_str)
    except (ValueError, TypeError):
        raise ValueError("Las grasas deben ser un número entre 0,5 y 2,0 g/kg") from None
    if not (0.5 <= gras <= 2.0):
        raise ValueError("Las grasas deben estar entre 0,5 y 2,0 g/kg")

    peso_str = datos.get("peso_kg", "").strip().replace(",", ".")
    if peso_str:
        try:
            peso: float | None = float(peso_str)
        except (ValueError, TypeError):
            raise ValueError("El peso debe ser un número entre 30 y 250 kg") from None
        if not (30.0 <= (peso or 0.0) <= 250.0):
            raise ValueError("El peso debe estar entre 30 y 250 kg")
    else:
        peso = None

    reparto: dict[str, int] = {}
    for i, m in enumerate(db.REPARTO_DEFECTO):
        val_str = datos.get(f"reparto-{i}", "").strip()
        if not val_str:
            raise ValueError(f"Falta el porcentaje para {m}")
        try:
            pct = int(val_str)
        except (ValueError, TypeError):
            raise ValueError(f"El porcentaje de {m} debe ser un entero entre 0 y 100") from None
        if not (0 <= pct <= 100):
            raise ValueError(f"El porcentaje de {m} debe ser un entero entre 0 y 100")
        reparto[m] = pct

    if sum(reparto.values()) != 100:
        raise ValueError("El reparto tiene que sumar 100 %")

    return db.AjustesComida(
        deficit_kcal=deficit,
        proteinas_g_kg=prot,
        grasas_g_kg=gras,
        peso_kg=peso,
        reparto=reparto,
    )
