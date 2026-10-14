"""Catálogo de métricas y extracción desde las respuestas de Garmin.

Cada valor se guarda con su fuente (`garmin`, más adelante `samsung`,
`propia`...) y nunca se mezclan sin decir de dónde salen. Las funciones
`de_*` reciben la respuesta tal cual de `garminconnect` y no llaman a nada:
así se pueden reprocesar las respuestas guardadas sin volver a pedirlas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

GARMIN = "garmin"


@dataclass(frozen=True)
class Definicion:
    """Cómo se llama y se enseña una métrica."""

    clave: str
    nombre: str
    unidad: str = ""
    decimales: int = 0
    # "alto" si subir es bueno, "bajo" si bajar es bueno, None si ninguna.
    mejor: str | None = None


CATALOGO: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion("pasos", "Pasos", mejor="alto"),
        Definicion("fc_reposo", "FC en reposo", "lpm", mejor="bajo"),
        Definicion("estres", "Estrés medio", mejor="bajo"),
        Definicion("bb_despertar", "Body Battery al despertar", mejor="alto"),
        Definicion("bb_max", "Body Battery máxima", mejor="alto"),
        Definicion("bb_min", "Body Battery mínima"),
        Definicion("bb_cargada", "Body Battery cargada"),
        Definicion("bb_gastada", "Body Battery gastada"),
        Definicion("kcal", "Calorías totales", "kcal"),
        Definicion("kcal_activas", "Calorías activas", "kcal"),
        Definicion("min_intensidad", "Minutos de intensidad", "min", mejor="alto"),
        Definicion("spo2", "SpO₂ media", "%", mejor="alto"),
        Definicion("respiracion", "Respiración en vigilia", "rpm"),
        Definicion("sueno_h", "Sueño", "h", 1, mejor="alto"),
        Definicion("sueno_puntuacion", "Puntuación del sueño", mejor="alto"),
        Definicion("sueno_profundo_h", "Sueño profundo", "h", 1),
        Definicion("sueno_ligero_h", "Sueño ligero", "h", 1),
        Definicion("sueno_rem_h", "Sueño REM", "h", 1),
        Definicion("sueno_despierto_h", "Despierto", "h", 1, mejor="bajo"),
        Definicion("sueno_inicio", "Hora de dormirse", "h", 2),
        Definicion("sueno_fin", "Hora de despertar", "h", 2),
        Definicion("vfc_noche", "VFC nocturna", "ms", mejor="alto"),
        Definicion("vfc_semana", "VFC media de 7 días", "ms", mejor="alto"),
        Definicion("vfc_base_baja", "VFC equilibrada desde", "ms"),
        Definicion("vfc_base_alta", "VFC equilibrada hasta", "ms"),
        Definicion("preparacion", "Preparación para entrenar", mejor="alto"),
        Definicion("preparacion_motivo", "Motivo de la preparación"),
        Definicion("recuperacion_h", "Tiempo de recuperación", "h", mejor="bajo"),
        Definicion("estado_entreno", "Estado de entrenamiento"),
        Definicion("carga_aguda", "Carga aguda"),
        Definicion("carga_cronica", "Carga crónica"),
        Definicion("ratio_carga", "Ratio carga aguda/crónica", decimales=1),
        Definicion("vo2max", "VO2max", decimales=1, mejor="alto"),
        Definicion("vo2max_ciclismo", "VO2max ciclismo", decimales=1, mejor="alto"),
        Definicion("ftp", "FTP", "W", mejor="alto"),
        Definicion("fc_umbral_ciclismo", "FC de umbral (ciclismo)", "lpm"),
        Definicion("peso", "Peso", "kg", 1),
        Definicion("grasa", "Grasa corporal", "%", 1, mejor="bajo"),
        Definicion("musculo", "Masa muscular", "kg", 1, mejor="alto"),
        Definicion("agua", "Agua corporal", "%", 1),
        Definicion("hueso", "Masa ósea", "kg", 1),
        Definicion("imc", "IMC", decimales=1),
    )
}

# Calificativos de Garmin, en castellano (se guardan tal cual y se traducen al enseñarlos).
TEXTOS = {
    # Sueño
    "EXCELLENT": "excelente",
    "GOOD": "bueno",
    "FAIR": "regular",
    "POOR": "malo",
    # VFC
    "BALANCED": "equilibrada",
    "UNBALANCED": "desequilibrada",
    "LOW": "baja",
    # Preparación para entrenar
    "PRIME": "óptima",
    "HIGH": "alta",
    "MODERATE": "moderada",
    # Estado de entrenamiento
    "PRODUCTIVE": "productivo",
    "MAINTAINING": "mantenimiento",
    "RECOVERY": "recuperación",
    "UNPRODUCTIVE": "no productivo",
    "DETRAINING": "desentrenando",
    "PEAKING": "pico de forma",
    "OVERREACHING": "sobrecarga",
    "STRAINED": "forzado",
    "NO_STATUS": "sin estado",
}

# El mismo código vale distinto según la métrica (LOW: VFC baja, preparación baja...).
_TEXTOS_POR_METRICA = {
    ("preparacion", "POOR"): "mala",
    ("vfc_noche", "POOR"): "deficiente",
}

TIPOS_ACTIVIDAD = {
    "soccer": "Fútbol",
    "cycling": "Ciclismo",
    "road_biking": "Ciclismo en carretera",
    "mountain_biking": "BTT",
    "gravel_cycling": "Gravel",
    "indoor_cycling": "Rodillo",
    "virtual_ride": "Rodillo",
    "running": "Carrera",
    "trail_running": "Trail",
    "treadmill_running": "Cinta",
    "walking": "Caminar",
    "hiking": "Senderismo",
    "paddelball": "Pádel",
    "padel": "Pádel",
    "tennis": "Tenis",
    "strength_training": "Fuerza",
    "lap_swimming": "Natación",
    "open_water_swimming": "Aguas abiertas",
    "yoga": "Yoga",
    "cardio": "Cardio",
}


@dataclass(frozen=True)
class Metrica:
    """Un valor de un día, con su fuente."""

    fecha: str
    clave: str
    fuente: str
    valor: float | None
    texto: str | None = None


@dataclass(frozen=True)
class Actividad:
    """Una actividad registrada por un dispositivo."""

    fuente: str
    id_fuente: str
    inicio: str  # local, "AAAA-MM-DD HH:MM:SS"
    tipo: str
    nombre: str
    bruto: str
    duracion_s: float | None = None
    distancia_m: float | None = None
    kcal: float | None = None
    fc_media: float | None = None
    fc_max: float | None = None
    potencia_media: float | None = None
    potencia_normalizada: float | None = None
    tss: float | None = None
    carga: float | None = None
    efecto_aerobico: float | None = None
    efecto_anaerobico: float | None = None
    desnivel_m: float | None = None

    @property
    def fecha(self) -> str:
        """Día de la actividad."""
        return self.inicio[:10]


def texto(clave: str, codigo: str | None) -> str:
    """El calificativo de Garmin en castellano ("BALANCED" -> "equilibrada")."""
    if not codigo:
        return ""
    base = _sin_sufijo(codigo)
    return (
        _TEXTOS_POR_METRICA.get((clave, base)) or TEXTOS.get(base) or base.replace("_", " ").lower()
    )


def nombre_tipo(tipo: str) -> str:
    """Nombre en castellano de un tipo de actividad de Garmin."""
    return TIPOS_ACTIVIDAD.get(tipo, tipo.replace("_", " ").capitalize())


def _sin_sufijo(codigo: str) -> str:
    """Quita el número final de las frases de Garmin ("PRODUCTIVE_6" -> "PRODUCTIVE")."""
    base, _, cola = codigo.rpartition("_")
    return base if base and cola.isdigit() else codigo


def _num(valor: Any) -> float | None:
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return None
    return float(valor)


def _metricas(
    fecha: str | None, valores: dict[str, float | None], textos: dict[str, str | None] | None = None
) -> list[Metrica]:
    """Las métricas con valor (o con texto) de un día."""
    if not fecha:
        return []
    textos = textos or {}
    claves = [k for k in valores if valores[k] is not None or textos.get(k)]
    return [Metrica(fecha[:10], k, GARMIN, valores[k], textos.get(k)) for k in claves]


def _horas(segundos: Any) -> float | None:
    s = _num(segundos)
    return round(s / 3600, 2) if s is not None else None


def de_resumen(datos: dict[str, Any]) -> list[Metrica]:
    """Resumen diario (`get_user_summary`): pasos, FC en reposo, Body Battery..."""
    if _num(datos.get("totalSteps")) is None and _num(datos.get("restingHeartRate")) is None:
        return []  # día sin datos (o todavía no ha llegado)
    estres = _num(datos.get("averageStressLevel"))
    moderados = _num(datos.get("moderateIntensityMinutes"))
    intensos = _num(datos.get("vigorousIntensityMinutes"))
    return _metricas(
        datos.get("calendarDate"),
        {
            "pasos": _num(datos.get("totalSteps")),
            "fc_reposo": _num(datos.get("restingHeartRate")),
            # Garmin pone -1 o -2 cuando no hay datos de estrés.
            "estres": estres if estres is not None and estres >= 0 else None,
            "bb_despertar": _num(datos.get("bodyBatteryAtWakeTime")),
            "bb_max": _num(datos.get("bodyBatteryHighestValue")),
            "bb_min": _num(datos.get("bodyBatteryLowestValue")),
            "bb_cargada": _num(datos.get("bodyBatteryChargedValue")),
            "bb_gastada": _num(datos.get("bodyBatteryDrainedValue")),
            "kcal": _num(datos.get("totalKilocalories")),
            "kcal_activas": _num(datos.get("activeKilocalories")),
            # Como en Garmin: los minutos intensos cuentan doble.
            "min_intensidad": (
                (moderados or 0) + 2 * (intensos or 0)
                if moderados is not None or intensos is not None
                else None
            ),
            "spo2": _num(datos.get("averageSpo2")),
            "respiracion": _num(datos.get("avgWakingRespirationValue")),
        },
    )


def _hora_local(ms: Any) -> tuple[float, str] | None:
    """Garmin da las horas "locales" como milisegundos de época: (horas decimales, "HH:MM")."""
    valor = _num(ms)
    if valor is None:
        return None
    momento = datetime.fromtimestamp(valor / 1000, UTC)
    return momento.hour + momento.minute / 60, momento.strftime("%H:%M")


def de_sueno(datos: dict[str, Any]) -> list[Metrica]:
    """Sueño (`get_sleep_data`): duración, fases, puntuación y horas."""
    dto = datos.get("dailySleepDTO") or {}
    if not _num(dto.get("sleepTimeSeconds")):
        return []
    puntuacion = (dto.get("sleepScores") or {}).get("overall") or {}
    valores: dict[str, float | None] = {
        "sueno_h": _horas(dto.get("sleepTimeSeconds")),
        "sueno_profundo_h": _horas(dto.get("deepSleepSeconds")),
        "sueno_ligero_h": _horas(dto.get("lightSleepSeconds")),
        "sueno_rem_h": _horas(dto.get("remSleepSeconds")),
        "sueno_despierto_h": _horas(dto.get("awakeSleepSeconds")),
        "sueno_puntuacion": _num(puntuacion.get("value")),
    }
    textos: dict[str, str | None] = {"sueno_puntuacion": puntuacion.get("qualifierKey")}
    inicio = _hora_local(dto.get("sleepStartTimestampLocal"))
    if inicio:
        # Dormirse a la 1:00 es "más tarde" que a las 23:00: 25 h, no 1 h.
        horas, hhmm = inicio
        valores["sueno_inicio"] = round(horas + 24 if horas < 12 else horas, 2)
        textos["sueno_inicio"] = hhmm
    fin = _hora_local(dto.get("sleepEndTimestampLocal"))
    if fin:
        valores["sueno_fin"] = round(fin[0], 2)
        textos["sueno_fin"] = fin[1]
    return _metricas(dto.get("calendarDate"), valores, textos)


def de_vfc(datos: dict[str, Any] | None) -> list[Metrica]:
    """Variabilidad de la frecuencia cardiaca (`get_hrv_data`)."""
    resumen = (datos or {}).get("hrvSummary") or {}
    base = resumen.get("baseline") or {}
    return _metricas(
        resumen.get("calendarDate"),
        {
            "vfc_noche": _num(resumen.get("lastNightAvg")),
            "vfc_semana": _num(resumen.get("weeklyAvg")),
            "vfc_base_baja": _num(base.get("balancedLow")),
            "vfc_base_alta": _num(base.get("balancedUpper")),
        },
        {"vfc_noche": resumen.get("status")},
    )


def de_preparacion(datos: dict[str, Any] | None) -> list[Metrica]:
    """Preparación para entrenar de la mañana (`get_morning_training_readiness`)."""
    datos = datos or {}
    recuperacion = _num(datos.get("recoveryTime"))  # minutos
    return _metricas(
        datos.get("calendarDate"),
        {
            "preparacion": _num(datos.get("score")),
            "preparacion_motivo": None,
            "recuperacion_h": round(recuperacion / 60, 1) if recuperacion is not None else None,
        },
        {"preparacion": datos.get("level"), "preparacion_motivo": datos.get("feedbackShort")},
    )


def de_estado_entreno(datos: dict[str, Any] | None) -> list[Metrica]:
    """Estado de entrenamiento y carga del día, y el último VO2max con su fecha."""
    datos = datos or {}
    filas: list[Metrica] = []
    estado = (datos.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    principal = next(
        (v for v in estado.values() if isinstance(v, dict) and v.get("primaryTrainingDevice")),
        None,
    )
    if principal:
        carga = principal.get("acuteTrainingLoadDTO") or {}
        filas += _metricas(
            principal.get("calendarDate"),
            {
                "estado_entreno": None,
                "carga_aguda": _num(carga.get("dailyTrainingLoadAcute")),
                "carga_cronica": _num(carga.get("dailyTrainingLoadChronic")),
                "ratio_carga": _num(carga.get("dailyAcuteChronicWorkloadRatio")),
            },
            {"estado_entreno": principal.get("trainingStatusFeedbackPhrase")},
        )
    vo2 = datos.get("mostRecentVO2Max") or {}
    for clave, tipo in (("vo2max", "generic"), ("vo2max_ciclismo", "cycling")):
        medida = vo2.get(tipo) or {}
        valor = _num(medida.get("vo2MaxPreciseValue")) or _num(medida.get("vo2MaxValue"))
        filas += _metricas(medida.get("calendarDate"), {clave: valor})
    return filas


def de_composicion(datos: dict[str, Any]) -> list[Metrica]:
    """Pesajes (`get_body_composition`): el último de cada día."""
    ultimos: dict[str, dict[str, Any]] = {}
    for pesaje in datos.get("dateWeightList") or []:
        dia = pesaje.get("calendarDate")
        if not dia or _num(pesaje.get("weight")) is None:
            continue
        previo = ultimos.get(dia)
        if previo is None or (pesaje.get("date") or 0) >= (previo.get("date") or 0):
            ultimos[dia] = pesaje
    filas: list[Metrica] = []
    for dia, p in sorted(ultimos.items()):

        def kg(gramos: Any) -> float | None:
            g = _num(gramos)
            return round(g / 1000, 2) if g is not None else None

        filas += _metricas(
            dia,
            {
                "peso": kg(p.get("weight")),
                "grasa": _num(p.get("bodyFat")),
                "musculo": kg(p.get("muscleMass")),
                "agua": _num(p.get("bodyWater")),
                "hueso": kg(p.get("boneMass")),
                "imc": _num(p.get("bmi")),
            },
        )
    return filas


def de_ftp(datos: Any) -> list[Metrica]:
    """FTP de ciclismo (`get_cycling_ftp`), en la fecha en que se fijó."""
    if not isinstance(datos, dict):
        return []
    return _metricas(
        datos.get("calendarDate"), {"ftp": _num(datos.get("functionalThresholdPower"))}
    )


def de_umbral(datos: Any, fecha: str) -> list[Metrica]:
    """FC de umbral de lactato en ciclismo (`get_lactate_threshold`); no trae fecha propia."""
    if not isinstance(datos, dict):
        return []
    fc = (datos.get("speed_and_heart_rate") or {}).get("heartRateCycling")
    return _metricas(fecha, {"fc_umbral_ciclismo": _num(fc)})


def de_actividad(datos: dict[str, Any]) -> Actividad | None:
    """Una actividad de `get_activities_by_date`."""
    if not datos.get("activityId") or not datos.get("startTimeLocal"):
        return None
    tipo = (datos.get("activityType") or {}).get("typeKey") or "otro"
    return Actividad(
        fuente=GARMIN,
        id_fuente=str(datos["activityId"]),
        inicio=str(datos["startTimeLocal"])[:19],
        tipo=tipo,
        nombre=str(datos.get("activityName") or ""),
        bruto=json.dumps(datos, ensure_ascii=False),
        duracion_s=_num(datos.get("duration")),
        distancia_m=_num(datos.get("distance")),
        kcal=_num(datos.get("calories")),
        fc_media=_num(datos.get("averageHR")),
        fc_max=_num(datos.get("maxHR")),
        potencia_media=_num(datos.get("avgPower")),
        potencia_normalizada=_num(datos.get("normPower")),
        tss=_num(datos.get("trainingStressScore")),
        carga=_num(datos.get("activityTrainingLoad")),
        efecto_aerobico=_num(datos.get("aerobicTrainingEffect")),
        efecto_anaerobico=_num(datos.get("anaerobicTrainingEffect")),
        desnivel_m=_num(datos.get("elevationGain")),
    )
