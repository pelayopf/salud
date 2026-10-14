"""Conexión con Garmin Connect (librería no oficial `garminconnect`).

Solo se guardan los tokens de la sesión, nunca la contraseña. La sesión se
crea una vez con `salud garmin-entrar` (pide el código de verificación si la
cuenta lo tiene activado) y la librería la renueva sola.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class GarminNoConectado(Exception):
    """No hay sesión de Garmin válida: hay que volver a entrar."""


class FuenteGarmin(Protocol):
    """Lo que se usa de `garminconnect.Garmin` (permite un doble en los tests)."""

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        """Resumen diario."""
        ...

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        """Sueño de la noche que termina ese día."""
        ...

    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None:
        """Variabilidad de la frecuencia cardiaca."""
        ...

    def get_morning_training_readiness(self, cdate: str) -> dict[str, Any] | None:
        """Preparación para entrenar de la mañana."""
        ...

    def get_training_status(self, cdate: str) -> dict[str, Any]:
        """Estado de entrenamiento, carga y último VO2max."""
        ...

    def get_body_composition(self, startdate: str, enddate: str | None = None) -> dict[str, Any]:
        """Pesajes entre dos fechas."""
        ...

    def get_activities_by_date(
        self, startdate: str, enddate: str | None = None
    ) -> list[dict[str, Any]]:
        """Actividades entre dos fechas."""
        ...

    def get_cycling_ftp(self) -> Any:
        """FTP de ciclismo actual."""
        ...

    def get_lactate_threshold(self) -> Any:
        """Umbral de lactato actual."""
        ...


def conectar(tokens: Path) -> FuenteGarmin:
    """Abre la sesión guardada; si no vale, `GarminNoConectado`."""
    from garminconnect import Garmin

    if not (tokens / "garmin_tokens.json").exists() and not tokens.is_file():
        raise GarminNoConectado(f"No hay sesión de Garmin en {tokens}: usa `salud garmin-entrar`")
    cliente = Garmin()
    try:
        cliente.login(str(tokens))
    except Exception as exc:  # la librería lanza varios tipos según el fallo
        raise GarminNoConectado(f"La sesión de Garmin no vale ({exc}): usa `salud garmin-entrar`")
    return cliente


def entrar(tokens: Path, correo: str, contrasena: str, pedir_codigo: Callable[[], str]) -> str:
    """Entra con usuario y contraseña (y código si hace falta) y guarda la sesión.

    Devuelve el nombre de la cuenta de Garmin.
    """
    from garminconnect import Garmin

    tokens.mkdir(parents=True, exist_ok=True)
    tokens.chmod(0o700)
    cliente = Garmin(correo, contrasena, prompt_mfa=pedir_codigo)
    cliente.login(str(tokens))
    fichero = tokens / "garmin_tokens.json"
    if fichero.exists():
        fichero.chmod(0o600)
    return str(getattr(cliente, "full_name", "") or getattr(cliente, "display_name", "") or correo)
