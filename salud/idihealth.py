"""API de IA para las pruebas de IdiHealth (la app Android de IDITAC).

Solo para Pelayo: va en la web de la tailnet (nunca en la pública), exige el
token de `SALUD_IDIHEALTH_TOKEN` y sin él ni existe. Es un reenviador: lo que
manda la app (métricas del día, comida, una foto) se usa para la llamada a la
IA y no se guarda en ningún sitio; solo queda el registro de uso y coste de
cada llamada, como el resto de la IA de esta app.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
from dataclasses import asdict
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import ia
from .config import Settings

log = logging.getLogger(__name__)

PREFIJO = "/api/idihealth"
MAX_CUERPO = 64_000  # bytes de JSON sin foto
MAX_CUERPO_FOTO = 12_000_000  # la foto va en base64 (ia.MAX_BYTES_IMAGEN la limita después)

ESTILO = (
    "Escribe en castellano de España, de tú, con un tono neutro y claro, sin exclamaciones, "
    "sin markdown y sin nombrar marcas ni tiendas. No diagnostiques ni hables de "
    "enfermedades, medicamentos o tratamientos: no es consejo médico. Usa solo los datos "
    "que te dan; si falta algo, no lo supongas."
)

INSTRUCCIONES_LECTURA = (
    "Eres el asistente de IdiHealth, una app de salud y deporte. Con los datos de hoy de "
    "la persona (en JSON: recuperación y sus factores, energía, sueño, tu forma y sus "
    "tendencias, entrenos recientes, lo que tiene planificado hoy, lesiones activas, "
    "objetivo y comida), escribe su lectura del día: un título de pocas palabras, un "
    "resumen de 2 o 3 frases de cómo está, un consejo para el entreno de hoy (si tiene una "
    "lesión activa, tenla en cuenta y no propongas cargar esa zona) y otro para la comida. "
    "Si algo merece atención (sueño muy corto varios días, esfuerzo muy por encima de su "
    "forma), ponlo en «aviso»; si no, déjalo vacío. "
)

ESQUEMA_LECTURA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "resumen": {"type": "string"},
        "entreno": {"type": "string"},
        "comida": {"type": "string"},
        "aviso": {"type": "string"},
    },
    "required": ["titulo", "resumen", "entreno", "comida"],
}

INSTRUCCIONES_QUE_COMO = (
    "Eres el asistente de comida de IdiHealth. Con lo que le queda a la persona hoy (kcal y "
    "macros), lo que ya ha comido, la hora, el momento del día y el deporte previsto (en "
    "JSON), propón 3 opciones para su próxima comida, de cocina casera española, fáciles y "
    "que encajen en lo que le queda. Para cada una: nombre del plato con cantidades "
    "aproximadas, kcal y proteínas estimadas, y una frase de por qué encaja. "
)

ESQUEMA_QUE_COMO: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sugerencias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "kcal": {"type": "number"},
                    "proteinas": {"type": "number"},
                    "por_que": {"type": "string"},
                },
                "required": ["nombre", "kcal", "proteinas", "por_que"],
            },
        }
    },
    "required": ["sugerencias"],
}


def token_valido(request: Request, settings: Settings) -> bool:
    """El token de la cabecera Authorization coincide con el configurado (sin filtrar tiempos)."""
    esperado = settings.idihealth_token
    if not esperado:
        return False
    cabecera = request.headers.get("authorization", "")
    if not cabecera.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(cabecera[7:].strip().encode(), esperado.encode())


def tarea_lectura(datos: dict[str, Any], settings: Settings) -> ia.Tarea:
    """La lectura del día a partir de las métricas que manda la app."""
    return ia.Tarea(
        nombre="idihealth-lectura",
        instrucciones=INSTRUCCIONES_LECTURA
        + ESTILO
        + "\n\nDatos de hoy:\n"
        + json.dumps(datos, ensure_ascii=False),
        esquema=ESQUEMA_LECTURA,
        modelo_agy=settings.ia_modelo_comida,
        modelo_vertex=settings.vertex_modelo,
        razonamiento="medium",
    )


def tarea_que_como(datos: dict[str, Any], settings: Settings) -> ia.Tarea:
    """Qué comer ahora, con lo que le queda del día."""
    return ia.Tarea(
        nombre="idihealth-que-como",
        instrucciones=INSTRUCCIONES_QUE_COMO
        + ESTILO
        + "\n\nDatos:\n"
        + json.dumps(datos, ensure_ascii=False),
        esquema=ESQUEMA_QUE_COMO,
        modelo_agy=settings.ia_modelo_comida,
        modelo_vertex=settings.vertex_modelo,
        razonamiento="low",
    )


def _error(texto: str, estado: int) -> JSONResponse:
    return JSONResponse({"error": texto}, status_code=estado)


def _error_ia(exc: ia.ErrorIA) -> JSONResponse:
    if isinstance(exc, ia.IAOcupada):
        return _error("La IA está ocupada con otra petición; prueba en un momento", 409)
    if isinstance(exc, ia.TopeGasto):
        return _error("Se ha llegado al tope de gasto de IA de hoy", 503)
    return _error(str(exc) or "La IA no ha podido responder", 503)


async def _cuerpo(request: Request, limite: int) -> dict[str, Any] | JSONResponse:
    """El JSON de la petición, o la respuesta de error si es grande o no es un objeto."""
    crudo = await request.body()
    if len(crudo) > limite:
        return _error("La petición es demasiado grande", 413)
    try:
        cuerpo = json.loads(crudo or b"{}")
    except json.JSONDecodeError:
        return _error("La petición no es JSON", 400)
    if not isinstance(cuerpo, dict):
        return _error("La petición no es un objeto JSON", 400)
    return cuerpo


def rutas(
    settings: Settings,
    ejecutar: Callable[[ia.Tarea, Settings], ia.RespuestaIA] = ia.ejecutar,
    analizar: Callable[[str, bytes | None], list[ia.AlimentoIA]] | None = None,
) -> list[Route]:
    """Las rutas de la API, o ninguna si no hay token configurado."""
    if not settings.idihealth_token:
        return []
    analizar_fn = analizar or (lambda t, img: ia.analizar_comida(settings, t, img))

    async def _con_datos(request: Request, crear: Callable[[dict[str, Any], Settings], ia.Tarea]):
        if not token_valido(request, settings):
            return _error("No autorizado", 401)
        cuerpo = await _cuerpo(request, MAX_CUERPO)
        if isinstance(cuerpo, JSONResponse):
            return cuerpo
        datos = cuerpo.get("datos")
        if not isinstance(datos, dict):
            return _error("Faltan los datos", 400)
        try:
            resp = await run_in_threadpool(ejecutar, crear(datos, settings), settings)
        except ia.ErrorIA as exc:
            return _error_ia(exc)
        return JSONResponse({"respuesta": resp.datos, "modelo": resp.modelo})

    async def lectura(request: Request):
        return await _con_datos(request, tarea_lectura)

    async def que_como(request: Request):
        return await _con_datos(request, tarea_que_como)

    async def comida(request: Request):
        if not token_valido(request, settings):
            return _error("No autorizado", 401)
        cuerpo = await _cuerpo(request, MAX_CUERPO_FOTO)
        if isinstance(cuerpo, JSONResponse):
            return cuerpo
        texto = cuerpo.get("texto") or ""
        if not isinstance(texto, str):
            return _error("El texto no es válido", 400)
        imagen: bytes | None = None
        if cuerpo.get("imagen"):
            try:
                imagen = base64.b64decode(cuerpo["imagen"], validate=True)
            except (binascii.Error, TypeError, ValueError):
                return _error("La foto no es válida", 400)
        try:
            alimentos = await run_in_threadpool(analizar_fn, texto, imagen)
        except ia.ErrorIA as exc:
            return _error_ia(exc)
        return JSONResponse({"alimentos": [asdict(a) for a in alimentos]})

    return [
        Route(f"{PREFIJO}/lectura", lectura, methods=["POST"]),
        Route(f"{PREFIJO}/que-como", que_como, methods=["POST"]),
        Route(f"{PREFIJO}/comida", comida, methods=["POST"]),
    ]
