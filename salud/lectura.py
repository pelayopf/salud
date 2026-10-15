"""Lectura diaria con Claude Code, sin la API: con la suscripción de la sesión
iniciada en calamar (`claude -p`).

Se le da un resumen de los últimos 14 días sacado de la base de datos y el
perfil (`perfil.toml`); si está configurado, puede consultar además la
nutrición de FatSecret y el catálogo de MyWhoosh con el MCP de
fitness-tracker en solo lectura. No tiene ninguna otra herramienta: ni
comandos, ni ficheros, ni web. Devuelve JSON con un esquema fijo, que se
guarda en la tabla `lecturas` y enseña la página Hoy.

Claude se lanza en un directorio temporal fuera de la carpeta personal y sin
cargar la configuración de nadie (`--setting-sources ""`), para que no lea
ningún CLAUDE.md ni ejecute hooks.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from . import calendario, db, ia, tratamiento
from .config import Settings
from .metricas import CATALOGO, nombre_tipo, texto

log = logging.getLogger(__name__)

DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
DIAS_CONTEXTO = 14
DIAS_MEDIA = 28
TIEMPO_MAXIMO_S = 600

# Lo que se pasa de cada día.
CLAVES_DIA = (
    "sueno_h",
    "sueno_puntuacion",
    "sueno_profundo_h",
    "sueno_rem_h",
    "sueno_despierto_h",
    "sueno_inicio",
    "sueno_fin",
    "vfc_noche",
    "fc_reposo",
    "bb_despertar",
    "bb_max",
    "bb_min",
    "preparacion",
    "preparacion_motivo",
    "recuperacion_h",
    "estado_entreno",
    "carga_aguda",
    "carga_cronica",
    "ratio_carga",
    "estres",
    "pasos",
    "min_intensidad",
    "kcal",
    "peso",
    "grasa",
)
# Medias de las últimas semanas, para comparar el día con lo normal en él.
CLAVES_MEDIA = ("fc_reposo", "vfc_noche", "sueno_h", "bb_despertar", "preparacion", "pasos")
# Lo que cambia poco: su último valor, con su fecha.
CLAVES_ULTIMO = ("peso", "grasa", "vo2max", "vo2max_ciclismo", "ftp", "fc_umbral_ciclismo")

# Herramientas del MCP de fitness-tracker que puede usar (todas de consulta).
SERVIDOR_FITNESS = "fitness"
HERRAMIENTAS_FITNESS = ("nutricion", "objetivos_nutricion", "recomendar_entreno")

ESTADOS = ("verde", "ambar", "rojo")
INTENSIDADES = ("descanso", "suave", "moderada", "alta")

ESQUEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "estado": {
            "type": "string",
            "enum": list(ESTADOS),
            "description": "verde: puede apretar; ambar: con cabeza; rojo: descansar o recuperar",
        },
        "titular": {"type": "string", "description": "Cómo está hoy, en 12 palabras como mucho"},
        "analisis": {
            "type": "string",
            "description": "2 o 3 frases con las cifras que lo justifican",
        },
        "entreno": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "titulo": {"type": "string"},
                "detalle": {
                    "type": "string",
                    "description": (
                        "Qué hacer en concreto: bloques, zonas o vatios, o por qué descansar"
                    ),
                },
                "duracion_min": {"type": ["integer", "null"]},
                "intensidad": {"type": "string", "enum": list(INTENSIDADES)},
            },
            "required": ["titulo", "detalle", "duracion_min", "intensidad"],
        },
        "comida": {
            "type": "string",
            "description": "Consejo de alimentación para hoy según el entreno y los objetivos",
        },
        "alertas": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Solo lo que haya que vigilar; vacío si no hay nada",
        },
    },
    "required": ["estado", "titular", "analisis", "entreno", "comida", "alertas"],
}

SISTEMA = """\
Eres el entrenador personal y nutricionista deportivo de la persona dueña de estos datos. \
Cada mañana le preparas una lectura breve de cómo está y qué le conviene hacer hoy.

Reglas:
- Usa solo los datos que te doy y lo que devuelvan tus herramientas. No inventes cifras. \
Si falta algo importante (por ejemplo, el reloj aún no ha mandado el sueño de esta noche), dilo.
- Da cifras concretas y compáralas con su propia normalidad: las medias de las últimas semanas \
y la franja equilibrada de su VFC.
- Ten en cuenta la carga de los últimos días (actividades, carga aguda y crónica) y lo que \
tenga previsto según su perfil.
- Respeta su plan de deporte (`plan_deporte`, sacado de su calendario y su \
semana tipo): el entreno de hoy debe encajar con lo que ya tiene. Si hoy tiene \
un partido o una sesión intensa planificada, ese es el entreno: no propongas \
otro aparte; como mucho, una activación suave o movilidad, y di cómo llegar \
bien. Si mañana tiene algo intenso, que hoy no le reste. Si lo planificado ya \
está hecho, dilo y propon recuperación.
- El entreno, concreto: tipo, duración e intensidad (zonas de FC, o vatios a partir del FTP). \
Si toca descansar, dilo y explica por qué.
- La comida, según el entreno de hoy, lo que comió ayer si lo puedes consultar y sus objetivos.
- Alertas solo si hay algo que vigilar (FC en reposo muy por encima de lo normal, VFC baja \
varios días, sueño corto acumulado, cambios bruscos de peso). No diagnostiques: si algo puede \
ser un problema de salud, recomienda consultarlo con un médico.
- Breve, que se lee en el móvil: el análisis, el detalle del entreno y la comida, tres frases \
como mucho cada uno; como mucho tres alertas, de una frase cada una.
- Castellano de España, directo y con frases cortas.
- Discreción: en lo que escribes no nombres el medicamento, el tratamiento, las \
inyecciones ni las dosis, aunque estén en los datos. Si influye (por ejemplo, en \
el apetito o la saciedad), dilo de forma neutra: «con el poco apetito de estos \
días», «si te llenas enseguida». Puede que otras personas vean la pantalla.
"""


class LecturaError(Exception):
    """Claude no devolvió una lectura válida."""


def _fecha_larga(d: date) -> str:
    return f"{DIAS_SEMANA[d.weekday()]} {d.day} de {MESES[d.month - 1]} de {d.year}"


def _formato(clave: str, v: db.Valor) -> Any:
    """El valor tal como se le pasa a Claude: número, o número con su calificativo."""
    definicion = CATALOGO.get(clave)
    calificativo = texto(clave, v.texto) if v.texto else ""
    if clave in ("sueno_inicio", "sueno_fin"):
        return v.texto
    if v.valor is None:
        return calificativo or None
    numero = round(v.valor, definicion.decimales if definicion else 1)
    if definicion and definicion.decimales == 0:
        numero = int(numero)
    return f"{numero} ({calificativo})" if calificativo else numero


def contexto(con: sqlite3.Connection, fecha: date) -> dict[str, Any]:
    """Lo que necesita Claude para la lectura de `fecha`, sacado de la base de datos."""
    desde = fecha - timedelta(days=DIAS_CONTEXTO - 1)
    hasta = fecha.isoformat()
    por_clave = db.series(con, CLAVES_DIA, desde.isoformat(), hasta)
    dias: dict[str, dict[str, Any]] = {}
    d = desde
    while d <= fecha:
        dias[d.isoformat()] = {"fecha": d.isoformat(), "dia": DIAS_SEMANA[d.weekday()]}
        d += timedelta(days=1)
    for clave, valores in por_clave.items():
        for v in valores:
            valor = _formato(clave, v)
            if valor is not None:
                dias[v.fecha][clave] = valor

    inicio_media = (fecha - timedelta(days=DIAS_MEDIA)).isoformat()
    ayer = (fecha - timedelta(days=1)).isoformat()
    medias = {}
    for clave, valores in db.series(con, CLAVES_MEDIA, inicio_media, ayer).items():
        numeros = [v.valor for v in valores if v.valor is not None]
        if numeros:
            medias[clave] = round(mean(numeros), 1)

    ultimos = {}
    for clave in CLAVES_ULTIMO:
        v = db.ultimo(con, clave, hasta)
        if v is not None and v.valor is not None:
            ultimos[clave] = {"valor": _formato(clave, v), "fecha": v.fecha}
    hace_un_mes = db.ultimo(con, "peso", (fecha - timedelta(days=30)).isoformat())
    if hace_un_mes is not None:
        ultimos["peso_hace_30_dias"] = {"valor": hace_un_mes.valor, "fecha": hace_un_mes.fecha}
    base = db.ultimo(con, "vfc_base_baja", hasta), db.ultimo(con, "vfc_base_alta", hasta)
    if base[0] and base[1]:
        ultimos["vfc_franja_equilibrada"] = f"{base[0].valor:.0f}-{base[1].valor:.0f} ms"

    actividades = [
        {
            "fecha": a["fecha"],
            "hora": a["inicio"][11:16],
            "tipo": nombre_tipo(a["tipo"]),
            "nombre": a["nombre"],
            "duracion_min": round(a["duracion_s"] / 60) if a["duracion_s"] else None,
            "distancia_km": round(a["distancia_m"] / 1000, 1) if a["distancia_m"] else None,
            "fc_media": a["fc_media"],
            "fc_max": a["fc_max"],
            "potencia_media": a["potencia_media"],
            "potencia_normalizada": a["potencia_normalizada"],
            "tss": round(a["tss"]) if a["tss"] else None,
            "carga": round(a["carga"]) if a["carga"] else None,
            "efecto_aerobico": a["efecto_aerobico"],
            "efecto_anaerobico": a["efecto_anaerobico"],
        }
        for a in db.actividades(con, desde.isoformat(), hasta)
    ]
    sync = db.ultima_sincronizacion(con, "garmin")
    anterior = db.lectura(con, ayer)
    items_plan = calendario.plan(con, fecha, fecha + timedelta(days=2), fecha)
    actividades_plan = db.actividades(
        con, fecha.isoformat(), (fecha + timedelta(days=2)).isoformat()
    )
    plan_deporte = []
    for p in items_plan:
        d_p = date.fromisoformat(p.fecha)
        item = {
            "fecha": p.fecha,
            "dia": DIAS_SEMANA[d_p.weekday()],
            "hora": p.inicio,
            "titulo": p.titulo,
            "deporte": nombre_tipo(p.tipo) if p.tipo else None,
            "duracion_min": p.duracion_min,
            "intensidad": p.intensidad,
            "origen": (
                "calendario" if p.origen in (calendario.GOOGLE, "calendario") else "semana tipo"
            ),
            "hecho": calendario.hecho(p, actividades_plan),
        }
        plan_deporte.append({k: v for k, v in item.items() if v is not None})

    return {
        "hoy": _fecha_larga(fecha),
        "ultima_sincronizacion_garmin": (
            {"hecha_utc": sync["hecha"], "estado": sync["estado"]} if sync else None
        ),
        "dias": list(dias.values()),
        f"medias_{DIAS_MEDIA}_dias": medias,
        "ultimos_valores": ultimos,
        "actividades": [{k: v for k, v in a.items() if v is not None} for a in actividades],
        "plan_deporte": plan_deporte,
        "lectura_de_ayer": (
            {
                "titular": anterior["contenido"]["titular"],
                "entreno": anterior["contenido"]["entreno"]["titulo"],
            }
            if anterior and anterior["contenido"]
            else None
        ),
        "tratamiento": tratamiento.resumen(con, fecha),
    }


def perfil(ruta: Path) -> str:
    """El perfil y los objetivos (TOML libre), o un aviso si no hay."""
    try:
        return ruta.read_text().strip()
    except FileNotFoundError:
        return "(No hay perfil: no conoces sus objetivos ni su semana tipo; no los supongas.)"


def mensaje(ctx: dict[str, Any], texto_perfil: str, con_fitness: bool) -> str:
    """El mensaje con el que se pide la lectura."""
    herramientas = (
        "Puedes consultar su nutrición en FatSecret (nutricion, objetivos_nutricion) y pedir "
        "entrenos del catálogo de MyWhoosh según su estado (recomendar_entreno)."
        if con_fitness
        else "No tienes herramientas: trabaja solo con estos datos."
    )
    return (
        f"Hoy es {ctx['hoy']}. Prepara la lectura de hoy.\n\n{herramientas}\n\n"
        f"## Perfil y objetivos\n\n{texto_perfil}\n\n"
        "## Datos (JSON)\n\nLas horas están en hora local. `dias` va del más antiguo a hoy; "
        "el sueño de cada día es el de la noche que termina ese día.\n\n"
        f"```json\n{json.dumps(ctx, ensure_ascii=False, indent=1)}\n```\n"
    )


def configuracion_mcp(settings: Settings) -> dict[str, Any] | None:
    """El MCP de fitness-tracker en solo lectura, o None si no está configurado."""
    if settings.fitness_tracker is None:
        return None
    return {
        "mcpServers": {
            SERVIDOR_FITNESS: {
                "type": "stdio",
                "command": settings.uv,
                "args": ["run", "--directory", str(settings.fitness_tracker), "fitness-mcp"],
                "env": {"FITNESS_READONLY": "1", "FITNESS_NONINTERACTIVE": "1"},
            }
        }
    }


def comando(settings: Settings, mcp: Path | None) -> list[str]:
    """La orden de Claude Code: sin herramientas propias y sin configuración de nadie."""
    orden = [
        settings.claude,
        "-p",
        "--setting-sources",
        "",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(ESQUEMA),
        "--system-prompt",
        SISTEMA,
        "--model",
        settings.lectura_modelo,
    ]
    if mcp is not None:
        permitidas = [f"mcp__{SERVIDOR_FITNESS}__{h}" for h in HERRAMIENTAS_FITNESS]
        orden += ["--mcp-config", str(mcp), "--allowedTools", ",".join(permitidas)]
    return orden


Ejecutor = Callable[[list[str], str, Path], str]
EjecutorVertex = Callable[[ia.Tarea, Settings], ia.RespuestaIA]


def ejecutar(orden: list[str], entrada: str, directorio: Path) -> str:
    """Lanza Claude Code y devuelve lo que escribe (JSON)."""
    try:
        proceso = subprocess.run(
            orden,
            input=entrada,
            capture_output=True,
            text=True,
            cwd=directorio,
            timeout=TIEMPO_MAXIMO_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise LecturaError(f"Claude tardó más de {TIEMPO_MAXIMO_S} s") from exc
    except OSError as exc:
        raise LecturaError(f"No se pudo lanzar Claude Code: {exc}") from exc
    if not proceso.stdout.strip():
        raise LecturaError(f"Claude Code no respondió: {proceso.stderr.strip()[-500:]}")
    return proceso.stdout


def validar(contenido: Any) -> dict[str, Any]:
    """Comprueba que la lectura traiga los campos requeridos y un estado válido."""
    if not isinstance(contenido, dict):
        raise LecturaError("La respuesta no trae la lectura")
    faltan = [c for c in ESQUEMA["required"] if c not in contenido]
    if faltan:
        raise LecturaError(f"A la lectura le falta: {', '.join(faltan)}")
    if contenido.get("estado") not in ESTADOS:
        raise LecturaError(f"Estado desconocido: {contenido.get('estado')!r}")
    return contenido


def interpretar(salida: str) -> dict[str, Any]:
    """Saca la lectura de la respuesta de `claude -p --output-format json`."""
    try:
        respuesta = json.loads(salida)
    except json.JSONDecodeError as exc:
        raise LecturaError(f"Respuesta que no es JSON: {salida[:300]}") from exc
    if respuesta.get("is_error"):
        raise LecturaError(f"Claude Code dio un error: {respuesta.get('result')}")
    contenido = respuesta.get("structured_output")
    if not isinstance(contenido, dict):
        try:
            contenido = json.loads(respuesta.get("result") or "")
        except json.JSONDecodeError as exc:
            raise LecturaError("La respuesta no trae la lectura") from exc
    return validar(contenido)


def generar(
    con: sqlite3.Connection,
    settings: Settings,
    fecha: date,
    ejecutor: Ejecutor = ejecutar,
    ejecutor_vertex: EjecutorVertex = ia.ejecutar_vertex,
) -> dict[str, Any]:
    """Pide la lectura de `fecha` y la guarda (también si falla, con el error)."""
    ctx = contexto(con, fecha)
    txt_perfil = perfil(settings.perfil_path)
    inicio = time.monotonic()

    if settings.lectura_motor == "vertex":
        tarea = ia.Tarea(
            nombre="lectura",
            instrucciones=SISTEMA + "\n\n" + mensaje(ctx, txt_perfil, con_fitness=False),
            esquema=ESQUEMA,
            modelo_vertex=settings.vertex_modelo_consejo,
            modelo_vertex_respaldo=settings.vertex_modelo_consejo_respaldo,
            razonamiento="high",
            tiempo_maximo_s=TIEMPO_MAXIMO_S,
        )
        try:
            resp = ejecutor_vertex(tarea, settings)
            contenido = validar(resp.datos)
        except (ia.ErrorIA, LecturaError) as exc:
            # Vertex sin respuesta, con el tope alcanzado o con una lectura incompleta:
            # se repite con Claude, que es lo que se hacía siempre.
            log.warning("Fallo en lectura por Vertex (%s); se intenta con Claude", exc)
        else:
            db.guardar_lectura(
                con,
                fecha.isoformat(),
                contenido=contenido,
                modelo=f"vertex:{resp.modelo}",
                segundos=round(time.monotonic() - inicio, 1),
            )
            return contenido

    mcp = configuracion_mcp(settings)
    entrada = mensaje(ctx, txt_perfil, mcp is not None)
    # Fuera de la carpeta personal: así no hay ningún CLAUDE.md por encima.
    with tempfile.TemporaryDirectory(prefix="salud-lectura-", dir="/tmp") as tmp:
        directorio = Path(tmp)
        ruta_mcp = None
        if mcp is not None:
            ruta_mcp = directorio / "mcp.json"
            ruta_mcp.write_text(json.dumps(mcp))
        try:
            contenido = interpretar(ejecutor(comando(settings, ruta_mcp), entrada, directorio))
        except LecturaError as exc:
            # Si ya había una buena de ese día, se queda; si no, se apunta el error.
            previa = db.lectura(con, fecha.isoformat())
            if previa is None or previa["estado"] != "ok":
                db.guardar_lectura(
                    con,
                    fecha.isoformat(),
                    error=str(exc),
                    modelo=settings.lectura_modelo,
                    segundos=round(time.monotonic() - inicio, 1),
                )
            raise
    db.guardar_lectura(
        con,
        fecha.isoformat(),
        contenido=contenido,
        modelo=settings.lectura_modelo,
        segundos=round(time.monotonic() - inicio, 1),
    )
    return contenido
