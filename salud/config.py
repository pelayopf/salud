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


def _ruta(valor: str) -> Path:
    return Path(valor).expanduser()


def load_settings() -> Settings:
    """Lee la configuración del .env del proyecto y del entorno."""
    load_dotenv(ENV_PATH)
    usuarios = os.environ.get("SALUD_USUARIOS", "")
    fitness = os.environ.get("SALUD_FITNESS_TRACKER", "").strip()
    if fitness and not _ruta(fitness).is_dir():
        raise ConfigError(f"SALUD_FITNESS_TRACKER no es un directorio: {fitness}")
    return Settings(
        db_path=_ruta(os.environ.get("SALUD_DB_PATH", "~/.local/share/salud/salud.db")),
        usuarios=frozenset(u.strip().lower() for u in usuarios.split(",") if u.strip()),
        garmin_tokens=_ruta(os.environ.get("SALUD_GARMIN_TOKENS", "~/.local/share/salud/garmin")),
        perfil_path=_ruta(os.environ.get("SALUD_PERFIL_PATH", "~/.config/salud/perfil.toml")),
        claude=os.environ.get("SALUD_CLAUDE", "").strip() or shutil.which("claude") or "claude",
        lectura_modelo=os.environ.get("SALUD_LECTURA_MODELO", "sonnet").strip() or "sonnet",
        fitness_tracker=_ruta(fitness) if fitness else None,
        uv=shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser()),
    )
