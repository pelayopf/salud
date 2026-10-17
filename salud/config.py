"""Carga de configuración desde variables de entorno."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Ruta explícita al .env del proyecto: load_dotenv() sin argumento busca a
# partir del directorio de trabajo, lo que falla si arranca desde systemd.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

MOTORES_IA = ("agy", "claude", "vertex")
MOTORES_LECTURA = ("claude", "vertex")


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
    lectura_motor: Literal["claude", "vertex"] = "claude"
    lectura_modelo: str = "sonnet"
    # Repo de fitness-tracker, para usar su MCP en solo lectura (o None).
    fitness_tracker: Path | None = None
    uv: str = "uv"
    # Funciones de IA bajo petición (salud/ia.py): "agy" (Gemini, por defecto), "claude" o "vertex".
    ia_motor: str = "agy"
    agy: str = "agy"
    # Sesión de agy (Google): el HOME aislado de cada tarea solo enlaza este fichero.
    agy_token: Path = Path("~/.gemini/antigravity-cli/antigravity-oauth-token").expanduser()
    ia_modelo_agy: str = "gemini-3.8-flash-low"
    ia_modelo_claude: str = "haiku"
    # Medido el 2026-10-15: en agy, Flash High razona ~21.000 tokens incluso para
    # una estimación sencilla (2-5 min, una sola petición, sin reintentos); Low no
    # razona (0 tokens, 5-15 s) y en datos estructurados acierta igual. No usar
    # High para estas tareas.
    # Consejo (planificar el día; pocas veces al día): Argon Medium desde agy
    # (cupo propio de Argon, semanal); si se agota, Flash Medium (~1 min, bien).
    # Low da consejos algo más vagos.
    ia_modelo_consejo: str = "gemini-4-argon-medium"
    ia_modelo_consejo_respaldo: str = "gemini-3.8-flash-medium"
    # "Añadir con IA" (varias veces al día): Flash Low, que con texto y con foto
    # acertó en 8-15 s; si se agota el cupo de Gemini, Argon Low (otro cupo).
    ia_modelo_comida: str = "gemini-3.8-flash-low"
    ia_modelo_comida_respaldo: str = "gemini-4-argon-low"
    # Motor "vertex" (Gemini por Vertex AI con clave de API de cuenta de servicio).
    vertex_api_key: str | None = field(default=None, repr=False)
    vertex_proyecto: str | None = None
    vertex_region: str = "global"
    vertex_modelo: str = "gemini-3.8-flash"
    # Consejo: Gemini 4 Argon (probado 2026-10-15, ~40 s); a veces da 429 por
    # saturación, y entonces va Gemini 3.1 Pro (~35 s) antes de caer a agy.
    vertex_modelo_consejo: str = "gemini-4-argon"
    vertex_modelo_consejo_respaldo: str = "gemini-3.1-pro-preview"
    # Control de gasto de IA (Vertex): estimación de coste frente a créditos
    ia_usd_eur: float = 0.92
    ia_credito_mes_eur: float = 8.80
    # Topes de gasto estimado en Vertex: al llegar, la IA pasa a agy hasta el día
    # (o mes) siguiente. El presupuesto de Google solo avisa; esto corta.
    ia_tope_dia_eur: float = 1.0
    ia_tope_mes_eur: float = 15.0
    ia_credito_prueba_eur: float = 264.0
    ia_prueba_desde: date = date(2026, 10, 15)
    ia_prueba_hasta: date = date(2027, 1, 14)
    # Dirección secreta iCal del calendario con los deportes; es un secreto.
    gcal_ics: str | None = None
    # Web pública (Cloudflare Tunnel): correos de Google con acceso.
    cuentas_google: frozenset[str] = frozenset()
    url_publica: str = ""
    google_oauth: Path | None = None  # JSON con client_id y client_secret
    # Token de la API de IA de IdiHealth (salud/idihealth.py); sin él no hay API.
    idihealth_token: str | None = field(default=None, repr=False)


def _ruta(valor: str) -> Path:
    return Path(valor).expanduser()


def _euros(variable: str, defecto: float) -> float:
    """Importe en euros de una variable de entorno; el defecto si falta o no es un número ≥ 0."""
    try:
        valor = float(os.environ.get(variable, "").strip().replace(",", "."))
    except ValueError:
        return defecto
    return valor if valor >= 0 else defecto


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
    lectura_motor = os.environ.get("SALUD_LECTURA_MOTOR", "").strip() or "claude"
    if lectura_motor not in MOTORES_LECTURA:
        raise ConfigError(
            f"SALUD_LECTURA_MOTOR debe ser {' o '.join(MOTORES_LECTURA)}: {lectura_motor}"
        )
    vertex_api_key = os.environ.get("SALUD_VERTEX_API_KEY", "").strip() or None
    vertex_proyecto = os.environ.get("SALUD_VERTEX_PROYECTO", "").strip() or None
    vertex_region = os.environ.get("SALUD_VERTEX_REGION", "").strip() or "global"
    vertex_modelo = os.environ.get("SALUD_VERTEX_MODELO", "").strip() or "gemini-3.8-flash"
    vertex_modelo_consejo = (
        os.environ.get("SALUD_VERTEX_MODELO_CONSEJO", "").strip() or "gemini-4-argon"
    )
    vertex_modelo_consejo_respaldo = (
        os.environ.get("SALUD_VERTEX_MODELO_CONSEJO_RESPALDO", "").strip()
        or "gemini-3.1-pro-preview"
    )
    if ia_motor == "vertex" and (not vertex_api_key or not vertex_proyecto):
        raise ConfigError(
            "SALUD_IA_MOTOR=vertex requiere SALUD_VERTEX_API_KEY y SALUD_VERTEX_PROYECTO"
        )
    if lectura_motor == "vertex" and (not vertex_api_key or not vertex_proyecto):
        raise ConfigError(
            "SALUD_LECTURA_MOTOR=vertex requiere SALUD_VERTEX_API_KEY y SALUD_VERTEX_PROYECTO"
        )
    try:
        ia_usd_eur = float(os.environ.get("SALUD_IA_USD_EUR", "0.92").strip() or 0.92)
    except ValueError:
        ia_usd_eur = 0.92
    try:
        ia_credito_mes_eur = float(
            os.environ.get("SALUD_IA_CREDITO_MES_EUR", "8.80").strip() or 8.80
        )
    except ValueError:
        ia_credito_mes_eur = 8.80
    try:
        ia_credito_prueba_eur = float(
            os.environ.get("SALUD_IA_CREDITO_PRUEBA_EUR", "264.0").strip() or 264.0
        )
    except ValueError:
        ia_credito_prueba_eur = 264.0
    desde_str = os.environ.get("SALUD_IA_PRUEBA_DESDE", "").strip()
    try:
        ia_prueba_desde = date.fromisoformat(desde_str) if desde_str else date(2026, 10, 15)
    except ValueError:
        ia_prueba_desde = date(2026, 10, 15)
    hasta_str = os.environ.get("SALUD_IA_PRUEBA_HASTA", "").strip()
    try:
        ia_prueba_hasta = date.fromisoformat(hasta_str) if hasta_str else date(2027, 1, 14)
    except ValueError:
        ia_prueba_hasta = date(2027, 1, 14)
    return Settings(
        idihealth_token=os.environ.get("SALUD_IDIHEALTH_TOKEN", "").strip() or None,
        db_path=_ruta(os.environ.get("SALUD_DB_PATH", "~/.local/share/salud/salud.db")),
        usuarios=frozenset(u.strip().lower() for u in usuarios.split(",") if u.strip()),
        garmin_tokens=_ruta(os.environ.get("SALUD_GARMIN_TOKENS", "~/.local/share/salud/garmin")),
        perfil_path=_ruta(os.environ.get("SALUD_PERFIL_PATH", "~/.config/salud/perfil.toml")),
        claude=os.environ.get("SALUD_CLAUDE", "").strip() or shutil.which("claude") or "claude",
        lectura_motor="vertex" if lectura_motor == "vertex" else "claude",
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
        or "gemini-4-argon-medium",
        ia_modelo_consejo_respaldo=os.environ.get("SALUD_IA_MODELO_CONSEJO_RESPALDO", "").strip()
        or "gemini-3.8-flash-medium",
        ia_modelo_comida=os.environ.get("SALUD_IA_MODELO_COMIDA", "").strip()
        or "gemini-3.8-flash-low",
        ia_modelo_comida_respaldo=os.environ.get("SALUD_IA_MODELO_COMIDA_RESPALDO", "").strip()
        or "gemini-4-argon-low",
        vertex_api_key=vertex_api_key,
        vertex_proyecto=vertex_proyecto,
        vertex_region=vertex_region,
        vertex_modelo=vertex_modelo,
        vertex_modelo_consejo=vertex_modelo_consejo,
        vertex_modelo_consejo_respaldo=vertex_modelo_consejo_respaldo,
        ia_usd_eur=ia_usd_eur,
        ia_credito_mes_eur=ia_credito_mes_eur,
        ia_tope_dia_eur=_euros("SALUD_IA_TOPE_DIA_EUR", 1.0),
        ia_tope_mes_eur=_euros("SALUD_IA_TOPE_MES_EUR", 15.0),
        ia_credito_prueba_eur=ia_credito_prueba_eur,
        ia_prueba_desde=ia_prueba_desde,
        ia_prueba_hasta=ia_prueba_hasta,
        gcal_ics=os.environ.get("SALUD_GCAL_ICS", "").strip() or None,
        cuentas_google=frozenset(c.strip().lower() for c in cuentas.split(",") if c.strip()),
        url_publica=os.environ.get("SALUD_URL_PUBLICA", "").strip().rstrip("/"),
        google_oauth=_ruta(
            os.environ.get("SALUD_GOOGLE_OAUTH", "~/.config/salud/google-oauth.json")
        ),
    )
