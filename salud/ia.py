"""Funciones de IA bajo petición: la IA propone y Pelayo confirma.

Cada función es una `Tarea` (instrucciones, esquema JSON de la respuesta y
adjuntos) que ejecuta un motor:

- `agy` (Antigravity CLI, Gemini; por defecto, para no gastar cupo de
  Claude). Solo acepta texto por la entrada, así que lee los adjuntos de
  disco: corre en una carpeta temporal con un HOME propio sin reglas, MCP ni
  skills, y con todas las órdenes denegadas. No puede leer fuera de su
  carpeta ni abrir URLs.
- `claude` (`claude -p`): los adjuntos van dentro del mensaje y corre sin
  herramientas.

Los adjuntos solo existen en esa carpeta temporal, que se borra al terminar.
Nunca se guardan en la base de datos. Una tarea a la vez.

La primera tarea es `leer_etiqueta`: la tabla nutricional de una foto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .config import Settings

MAX_BYTES_IMAGEN = 8 * 1024 * 1024
TIEMPO_MAXIMO_S = 90

Unidad = Literal["g", "ml"]


class ErrorIA(Exception):
    """La IA no ha podido hacer la tarea. El mensaje se enseña tal cual en la web."""


class IAOcupada(ErrorIA):
    """Ya hay otra tarea de IA en marcha."""


@dataclass(frozen=True)
class Adjunto:
    """Un fichero para la IA (en memoria; solo toca disco mientras corre agy)."""

    tipo: str  # tipo MIME: image/jpeg, image/png, image/webp
    datos: bytes


@dataclass(frozen=True)
class Tarea:
    """Lo que se pide a la IA: instrucciones, esquema de la respuesta y adjuntos."""

    nombre: str  # para el log, p. ej. "etiqueta"
    instrucciones: str
    esquema: dict[str, Any]
    adjuntos: tuple[Adjunto, ...] = ()


@dataclass(frozen=True)
class Etiqueta:
    """Valores por 100 g o 100 ml leídos de una etiqueta, para revisarlos antes de guardar."""

    nombre: str | None
    marca: str | None
    unidad: Unidad | None
    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None
    por_racion: bool  # la etiqueta solo traía la ración y se ha pasado a 100
    cuadra: bool  # 4·proteínas + 4·hidratos + 9·grasas ≈ kcal (True si no se puede comprobar)
    avisos: tuple[str, ...]  # notas para enseñar junto al formulario


def tipo_imagen(datos: bytes) -> str | None:
    """Tipo MIME según los primeros bytes (JPEG, PNG o WebP); None si no es ninguno."""
    if datos.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(datos) >= 12 and datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "image/webp"
    return None


def leer_etiqueta(settings: Settings, imagen: bytes) -> Etiqueta:
    """Lee la tabla nutricional de una foto.

    Raises:
        IAOcupada: ya hay otra tarea en marcha.
        ErrorIA: la foto no es una imagen válida, la IA falla, tarda más de
            `TIEMPO_MAXIMO_S` o no se lee ningún valor.
    """
    raise NotImplementedError
