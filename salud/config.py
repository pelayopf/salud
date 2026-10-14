"""Carga de configuración desde variables de entorno."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Ruta explícita al .env del proyecto: load_dotenv() sin argumento busca a
# partir del directorio de trabajo, lo que falla si arranca desde systemd.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

MOTORES_IA = ("agy", "claude")


class ConfigError(Exception):
    """Falta o es inválida una variable de configuración."""


@dataclass(frozen=True)
class Settings:
    """Configuración de la aplicación."""

    db_path: Path
    usuarios: frozenset[str] = frozenset()
    # Sesión de Garmin: solo los tokens, nunca la contraseña.
    garmin_tokens: Path = Path("~/.local/share/salud/garmin").expanduser()
    # Perfil y objetivos para la lectura diaria: datos personales, fuera del repo.
    perfil_path: Path = Path("~/.config/salud/perfil.toml").expanduser()
    # Lectura diaria con Claude Code (la suscripción de la sesión iniciada).
    claude: str = "claude"
    lectura_modelo: str = "sonnet"
    # Repo de fitness-tracker, para usar su MCP en solo lectura (o None).
    fitness_tracker: Path | None = None
    uv: str = "uv"
    # Funciones de IA bajo petición (salud/ia.py): "agy" (Gemini, por defecto) o "claude".
    ia_motor: str = "agy"
    agy: str = "agy"
    # Sesión de agy (Google): el HOME aislado de cada tarea solo enlaza este fichero.
    agy_token: Path = Path("~/.gemini/antigravity-cli/antigravity-oauth-token").expanduser()
    ia_modelo_agy: str = "gemini-3.8-flash-low"
    ia_modelo_claude: str = "haiku"
    # Prima la calidad (Pelayo, 2026-10-14): Sonnet desde agy (~25 s, gasta
    # ~2 % del cupo semanal de Claude/GPT en agy); si ese cupo se agota, Flash
    # High (Gemini, de 35 s a más de 2 min). Flash Low da consejos vagos.
    ia_modelo_consejo: str = "claude-sonnet-5-5-medium"
    ia_modelo_consejo_respaldo: str = "gemini-3.8-flash-high"
    # "Añadir con IA" (probado 2026-10-14 con texto y foto de un plato): Sonnet
    # es el más fino y tarda ~10 s (~1,4 % del cupo semanal Claude/GPT); Flash
    # Low, de respaldo, acierta casi igual en ~10 s. Medium y High tardan 2-5 min
    # con foto sin ser mejores.
    ia_modelo_comida: str = "claude-sonnet-5-5-medium"
    ia_modelo_comida_respaldo: str = "gemini-3.8-flash-low"
    # Dirección secreta iCal del calendario con los deportes; es un secreto.
    gcal_ics: str | None = None
    # Web pública (Cloudflare Tunnel): correos de Google con acceso.
    cuentas_google: frozenset[str] = frozenset()
    url_publica: str = ""
    google_oauth: Path | None = None  # JSON con client_id y client_secret


def _ruta(valor: str) -> Path:
    return Path(valor).expanduser()


def load_settings() -> Settings:
    """Lee la configuración del .env del proyecto y del entorno."""
    load_dotenv(ENV_PATH)
    usuarios = os.environ.get("SALUD_USUARIOS", "")
    cuentas = os.environ.get("SALUD_CUENTAS_GOOGLE", "")
    fitness = os.environ.get("SALUD_FITNESS_TRACKER", "").strip()
    if fitness and not _ruta(fitness).is_dir():
        raise ConfigError(f"SALUD_FITNESS_TRACKER no es un directorio: {fitness}")
    ia_motor = os.environ.get("SALUD_IA_MOTOR", "").strip() or "agy"
    if ia_motor not in MOTORES_IA:
        raise ConfigError(f"SALUD_IA_MOTOR debe ser {' o '.join(MOTORES_IA)}: {ia_motor}")
    return Settings(
        db_path=_ruta(os.environ.get("SALUD_DB_PATH", "~/.local/share/salud/salud.db")),
        usuarios=frozenset(u.strip().lower() for u in usuarios.split(",") if u.strip()),
        garmin_tokens=_ruta(os.environ.get("SALUD_GARMIN_TOKENS", "~/.local/share/salud/garmin")),
        perfil_path=_ruta(os.environ.get("SALUD_PERFIL_PATH", "~/.config/salud/perfil.toml")),
        claude=os.environ.get("SALUD_CLAUDE", "").strip() or shutil.which("claude") or "claude",
        lectura_modelo=os.environ.get("SALUD_LECTURA_MODELO", "sonnet").strip() or "sonnet",
        fitness_tracker=_ruta(fitness) if fitness else None,
        uv=shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser()),
        ia_motor=ia_motor,
        agy=os.environ.get("SALUD_AGY", "").strip()
        or shutil.which("agy")
        or str(Path("~/.local/bin/agy").expanduser()),
        agy_token=_ruta(
            os.environ.get("SALUD_AGY_TOKEN", "~/.gemini/antigravity-cli/antigravity-oauth-token")
        ),
        ia_modelo_agy=os.environ.get("SALUD_IA_MODELO_AGY", "").strip() or "gemini-3.8-flash-low",
        ia_modelo_claude=os.environ.get("SALUD_IA_MODELO_CLAUDE", "").strip() or "haiku",
        ia_modelo_consejo=os.environ.get("SALUD_IA_MODELO_CONSEJO", "").strip()
        or "claude-sonnet-5-5-medium",
        ia_modelo_consejo_respaldo=os.environ.get("SALUD_IA_MODELO_CONSEJO_RESPALDO", "").strip()
        or "gemini-3.8-flash-high",
        ia_modelo_comida=os.environ.get("SALUD_IA_MODELO_COMIDA", "").strip()
        or "claude-sonnet-5-5-medium",
        ia_modelo_comida_respaldo=os.environ.get("SALUD_IA_MODELO_COMIDA_RESPALDO", "").strip()
        or "gemini-3.8-flash-low",
        gcal_ics=os.environ.get("SALUD_GCAL_ICS", "").strip() or None,
        cuentas_google=frozenset(c.strip().lower() for c in cuentas.split(",") if c.strip()),
        url_publica=os.environ.get("SALUD_URL_PUBLICA", "").strip().rstrip("/"),
        google_oauth=_ruta(
            os.environ.get("SALUD_GOOGLE_OAUTH", "~/.config/salud/google-oauth.json")
        ),
    )
