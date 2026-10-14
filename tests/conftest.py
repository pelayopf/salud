"""Fixtures comunes. Todos los datos son inventados."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, TypeVar

import pytest

from salud import db
from salud.config import Settings

T = TypeVar("T")


def hay(valor: T | None) -> T:
    """Comprueba que hay valor (y se lo dice a pyright)."""
    assert valor is not None
    return valor


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "salud.db",
        garmin_tokens=tmp_path / "garmin",
        perfil_path=tmp_path / "perfil.toml",
        claude="claude",
        lectura_modelo="sonnet",
    )


@pytest.fixture
def con(settings):
    c = db.conectar(settings.db_path)
    yield c
    c.close()


def ms_local(fecha: str, hora: str) -> int:
    """Como Garmin: la hora local escrita como milisegundos de época."""
    d = datetime.fromisoformat(f"{fecha}T{hora}:00+00:00")
    return int(d.timestamp() * 1000)


def resumen(fecha: str, pasos: int = 9000, fc: int = 50, bb: int = 80) -> dict[str, Any]:
    return {
        "calendarDate": fecha,
        "totalSteps": pasos,
        "restingHeartRate": fc,
        "averageStressLevel": 25,
        "bodyBatteryAtWakeTime": bb,
        "bodyBatteryHighestValue": bb + 5,
        "bodyBatteryLowestValue": 20,
        "bodyBatteryChargedValue": 60,
        "bodyBatteryDrainedValue": 55,
        "totalKilocalories": 2500.0,
        "activeKilocalories": 600.0,
        "moderateIntensityMinutes": 10,
        "vigorousIntensityMinutes": 20,
        "averageSpo2": 96.0,
        "avgWakingRespirationValue": 14.0,
    }


def sueno(fecha: str, horas: float = 7.5, puntuacion: int = 80) -> dict[str, Any]:
    anterior = (date.fromisoformat(fecha) - timedelta(days=1)).isoformat()
    return {
        "dailySleepDTO": {
            "calendarDate": fecha,
            "sleepTimeSeconds": int(horas * 3600),
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 14400,
            "remSleepSeconds": 6000,
            "awakeSleepSeconds": 900,
            "sleepStartTimestampLocal": ms_local(anterior, "23:30"),
            "sleepEndTimestampLocal": ms_local(fecha, "07:15"),
            "sleepScores": {"overall": {"value": puntuacion, "qualifierKey": "GOOD"}},
        }
    }


def vfc(fecha: str, noche: int = 60) -> dict[str, Any]:
    return {
        "hrvSummary": {
            "calendarDate": fecha,
            "weeklyAvg": 58,
            "lastNightAvg": noche,
            "baseline": {"balancedLow": 50, "balancedUpper": 70},
            "status": "BALANCED",
        }
    }


def preparacion(fecha: str, score: int = 72) -> dict[str, Any]:
    return {
        "calendarDate": fecha,
        "score": score,
        "level": "MODERATE",
        "feedbackShort": "WELL_RECOVERED",
        "recoveryTime": 600,
    }


def estado(fecha: str) -> dict[str, Any]:
    return {
        "mostRecentVO2Max": {
            "generic": {"calendarDate": "2026-01-03", "vo2MaxPreciseValue": 47.6},
            "cycling": {"calendarDate": "2026-01-03", "vo2MaxValue": 48.0},
        },
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "111": {"calendarDate": fecha, "primaryTrainingDevice": False},
                "222": {
                    "calendarDate": fecha,
                    "primaryTrainingDevice": True,
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_3",
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 500,
                        "dailyTrainingLoadChronic": 450,
                        "dailyAcuteChronicWorkloadRatio": 1.1,
                    },
                },
            }
        },
    }


def actividad(id_: int, inicio: str, tipo: str = "cycling", carga: float = 120.0) -> dict[str, Any]:
    return {
        "activityId": id_,
        "activityName": "Salida inventada",
        "startTimeLocal": inicio,
        "activityType": {"typeKey": tipo},
        "duration": 3600.0,
        "distance": 30000.0,
        "calories": 700.0,
        "averageHR": 140.0,
        "maxHR": 175.0,
        "avgPower": 180.0,
        "normPower": 195.0,
        "trainingStressScore": 80.0,
        "activityTrainingLoad": carga,
        "aerobicTrainingEffect": 3.2,
        "anaerobicTrainingEffect": 1.1,
    }


class GarminFalso:
    """Doble de `garminconnect.Garmin` con datos inventados."""

    def __init__(self, fallar: tuple[str, ...] = ()) -> None:
        self.fallar = set(fallar)
        self.llamadas: list[str] = []

    def _llamada(self, nombre: str) -> None:
        self.llamadas.append(nombre)
        if nombre in self.fallar or "todo" in self.fallar:
            raise RuntimeError(f"fallo inventado en {nombre}")

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        self._llamada("resumen")
        return resumen(cdate)

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        self._llamada("sueno")
        return sueno(cdate)

    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None:
        self._llamada("vfc")
        return vfc(cdate)

    def get_morning_training_readiness(self, cdate: str) -> dict[str, Any] | None:
        self._llamada("preparacion")
        return preparacion(cdate)

    def get_training_status(self, cdate: str) -> dict[str, Any]:
        self._llamada("estado")
        return estado(cdate)

    def get_body_composition(self, startdate: str, enddate: str | None = None) -> dict[str, Any]:
        self._llamada("composicion")
        return {
            "dateWeightList": [
                {"calendarDate": startdate, "date": 1, "weight": 80500.0, "bodyFat": 20.0},
                {"calendarDate": enddate, "date": 2, "weight": 80100.0, "bodyFat": 19.8},
            ]
        }

    def get_activities_by_date(
        self, startdate: str, enddate: str | None = None
    ) -> list[dict[str, Any]]:
        self._llamada("actividades")
        return [
            actividad(1, f"{startdate} 18:00:00"),
            actividad(2, f"{enddate} 19:30:00", "soccer"),
        ]

    def get_cycling_ftp(self) -> Any:
        self._llamada("ftp")
        return {"calendarDate": "2026-01-02T10:00:00.0", "functionalThresholdPower": 230}

    def get_lactate_threshold(self) -> Any:
        self._llamada("umbral")
        return {"speed_and_heart_rate": {"heartRateCycling": 165}}


@pytest.fixture
def garmin() -> GarminFalso:
    return GarminFalso()
