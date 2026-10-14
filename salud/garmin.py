"""Conexión con Garmin Connect (librería no oficial `garminconnect`).

Solo se guardan los tokens de la sesión, nunca la contraseña. La sesión se
crea una vez, desde Conexiones en la web o con `salud garmin-entrar` (las
dos piden el código de verificación si la cuenta lo tiene activado), y la
librería la renueva sola.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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

    if not fichero_tokens(tokens).is_file():
        raise GarminNoConectado("No hay sesión de Garmin: entra desde Conexiones")
    cliente = Garmin()
    try:
        cliente.login(str(tokens))
    except Exception as exc:  # la librería lanza varios tipos según el fallo
        raise GarminNoConectado(f"La sesión de Garmin no vale ({exc}): entra desde Conexiones")
    return cliente


def fichero_tokens(tokens: Path) -> Path:
    """El fichero de la sesión dentro de `tokens` (o `tokens` si ya es el fichero)."""
    return tokens if tokens.suffix == ".json" else tokens / "garmin_tokens.json"


# --- Entrar en dos pasos -------------------------------------------------------
#
# Con `return_on_mfa` la librería no pide el código por consola: devuelve
# "needs_mfa" y deja la sesión a medias en el propio cliente, que se completa
# con `resume_login`. Así sirve igual para la terminal y para la web. En ese
# modo no guarda los tokens ni carga el perfil: lo hace `_guardar`.


def cliente_nuevo(correo: str, contrasena: str) -> Any:
    """Un `garminconnect.Garmin` listo para entrar en dos pasos."""
    from garminconnect import Garmin

    return Garmin(correo, contrasena, return_on_mfa=True)


@dataclass
class Pendiente:
    """Entrada a falta del código de verificación (solo en memoria)."""

    cliente: Any


def empezar_entrada(
    tokens: Path,
    correo: str,
    contrasena: str,
    crear: Callable[[str, str], Any] = cliente_nuevo,
) -> Pendiente | None:
    """Primer paso: correo y contraseña.

    Si Garmin no pide código, guarda la sesión y devuelve None; si lo pide,
    devuelve la entrada pendiente para `terminar_entrada`. La contraseña no
    se queda en el cliente en ningún caso.
    """
    cliente = crear(correo, contrasena)
    try:
        estado, _ = cliente.login()
    finally:
        cliente.password = None
    if estado == "needs_mfa":
        return Pendiente(cliente)
    _guardar(cliente, tokens)
    return None


def terminar_entrada(tokens: Path, pendiente: Pendiente, codigo: str) -> None:
    """Segundo paso: el código de verificación. Si falla, se puede reintentar."""
    pendiente.cliente.resume_login({}, codigo)
    _guardar(pendiente.cliente, tokens)


def _guardar(cliente: Any, tokens: Path) -> None:
    """Escribe los tokens (directorio 700, fichero 600: lo asegura la librería)."""
    tokens.mkdir(parents=True, exist_ok=True)
    tokens.chmod(0o700)
    cliente.client.dump(str(tokens))
