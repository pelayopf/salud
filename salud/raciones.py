"""Manejo y limpieza de nombres de raciones de alimentos."""

from __future__ import annotations

import re


def limpiar_racion_nombre(texto: str | None) -> str | None:
    """Deja el nombre de la ración corto y sin cantidad.

    Ejemplo: "1 plátano mediano (sin piel)" → "plátano mediano".
    """
    if texto is None:
        return None
    limpio = re.sub(r"\([^)]*\)", "", texto)
    limpio = re.sub(r"\(.*$", "", limpio)
    limpio = re.sub(r"^\s*(?:\d+(?:[.,]\d+)?|una|uno|un)\s+", "", limpio, flags=re.IGNORECASE)
    limpio = re.sub(r"\s+", " ", limpio).strip(" ,.")
    if limpio:
        limpio = limpio[0].lower() + limpio[1:]
    if len(limpio) > 30:
        limpio = limpio[:30].rstrip()
    return limpio or None
