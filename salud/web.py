"""Web de la app: Hoy, Tendencias, Actividades, Lecturas y Conexiones."""

from __future__ import annotations

import calendar
import hashlib
import logging
import math
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import (
    acceso,
    alimentos,
    calendario,
    ciqual,
    consejo,
    db,
    garmin,
    graficas,
    ia,
    lectura,
    metricas,
    nutricion,
    raciones,
    sync,
    tratamiento,
)
from .acceso import AccesoMiddleware
from .config import Settings
from .garmin import FuenteGarmin, GarminNoConectado
from .graficas import cifra
from .metricas import CATALOGO, GARMIN, nombre_tipo, texto

BASE = Path(__file__).resolve().parent
PLANTILLAS_DIR = BASE / "plantillas"
ESTATICOS = BASE / "estaticos"
ZONA = ZoneInfo("Europe/Madrid")

DIAS_SPARK = 14
DIAS_MEDIA = 28
PERIODOS = (30, 90, 180, 365)
DIAS_SYNC_WEB = 2
DIAS_SEMANA = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
DIAS_PASADOS_PLAN = 6
DIAS_FUTUROS_PLAN = 7
FILAS_VACIAS_SEMANA = 2

# Entrar en Garmin desde Conexiones.
CADUCIDAD_CODIGO_S = 10 * 60
MAX_FALLOS = 5
BLOQUEO_S = 15 * 60

log = logging.getLogger(__name__)


def _version_estaticos() -> str:
    """Huella de CSS y JS, para que el móvil no use copias viejas."""
    h = hashlib.sha256()
    for nombre in ("estilos.css", "app.js"):
        h.update((ESTATICOS / nombre).read_bytes())
    return h.hexdigest()[:10]


PLANTILLAS = Environment(
    loader=FileSystemLoader(PLANTILLAS_DIR), autoescape=select_autoescape(["html"])
)
PLANTILLAS.globals["version"] = _version_estaticos()
PLANTILLAS.globals["acceso"] = acceso.actual
PLANTILLAS.filters["cifra"] = cifra


def gramos(valor: float | None) -> str:
    """Gramos con un decimal solo si lo tienen: 16,2 · 3 (en comida, 1,6 g no es 2 g)."""
    texto = cifra(valor, 1)
    return texto.removesuffix(",0")


PLANTILLAS.filters["gramos"] = gramos
PLANTILLAS.filters["texto"] = lambda codigo, clave="": texto(clave, codigo)
PLANTILLAS.filters["tipo"] = nombre_tipo


def horas_y_minutos(horas: float | None) -> str:
    """7,25 -> "7 h 15 min"."""
    if horas is None:
        return "—"
    minutos = round(horas * 60)
    h, m = divmod(minutos, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def fecha_larga(fecha: str) -> str:
    """ "2026-10-07" -> "martes 7 de octubre"."""
    d = date.fromisoformat(fecha)
    return f"{lectura.DIAS_SEMANA[d.weekday()]} {d.day} de {lectura.MESES[d.month - 1]}"


def hace(momento_utc: str | None) -> str:
    """Cuánto hace de una marca de tiempo UTC ("hace 5 min", "ayer a las 21:04")."""
    if not momento_utc:
        return "nunca"
    m = datetime.fromisoformat(momento_utc).astimezone(ZONA)
    ahora = datetime.now(ZONA)
    minutos = int((ahora - m).total_seconds() // 60)
    if minutos < 1:
        return "ahora mismo"
    if minutos < 60:
        return f"hace {minutos} min"
    if m.date() == ahora.date():
        return f"hoy a las {m:%H:%M}"
    if m.date() == ahora.date() - timedelta(days=1):
        return f"ayer a las {m:%H:%M}"
    return f"el {m.day}/{m.month} a las {m:%H:%M}"


hora_local = db.hora_local


def dia_corto(fecha: str | date) -> str:
    """ "2026-10-16" -> "viernes 16 oct"."""
    d = date.fromisoformat(fecha) if isinstance(fecha, str) else fecha
    return f"{lectura.DIAS_SEMANA[d.weekday()]} {d.day} {graficas.MESES_CORTOS[d.month - 1]}"


def _tratamiento(con: sqlite3.Connection, dia: date) -> dict[str, Any]:
    """Pauta, última dosis, próxima, zona sugerida, dosis hechas y días del ciclo."""
    pauta = db.pauta(con)
    recientes = db.dosis(con, limite=1)
    ultima = recientes[0] if recientes else None
    dosis_hechas = db.contar_dosis(con, farmaco=pauta.farmaco, dosis_mg=pauta.dosis_mg)
    dias = tratamiento.dias_ciclo(pauta, ultima, dia)
    return {
        "pauta": pauta,
        "ultima": ultima,
        "prox": tratamiento.proxima(pauta, ultima, dia),
        "zona_sugerida": tratamiento.zona_sugerida(ultima.zona if ultima else None),
        "dosis_hechas": dosis_hechas,
        "dias_ciclo": dias,
    }


PLANTILLAS.filters["horas"] = horas_y_minutos
PLANTILLAS.filters["fecha_larga"] = fecha_larga
PLANTILLAS.filters["fecha_corta"] = lambda f: graficas.fecha_corta(f)
PLANTILLAS.filters["dia_corto"] = dia_corto
PLANTILLAS.filters["hace"] = hace
PLANTILLAS.filters["hora_local"] = hora_local


def _render(nombre: str, **contexto: Any) -> HTMLResponse:
    return HTMLResponse(PLANTILLAS.get_template(nombre).render(**contexto))


def _volver(mensaje: str, destino: str = "/") -> RedirectResponse:
    base, _, fragmento = destino.partition("#")
    separador = "&" if "?" in base else "?"
    url = f"{base}{separador}{urlencode({'mensaje': mensaje})}"
    if fragmento:
        url = f"{url}#{fragmento}"
    return RedirectResponse(url, 303)


async def _formulario(request: Request) -> dict[str, str]:
    """Campos de un formulario normal (sin ficheros), sin depender de python-multipart."""
    cuerpo = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(cuerpo, keep_blank_values=True).items()}


@dataclass
class _EntradaGarmin:
    """Entrada en Garmin desde la web. Solo en memoria: nada de esto va a disco."""

    pendiente: garmin.Pendiente | None = None
    id_pendiente: str = ""
    caduca: float = 0.0
    fallos: int = 0
    bloqueado_hasta: float = 0.0


# --- Tarjetas de Hoy -----------------------------------------------------------

ICONO_SUENO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    ' aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
)
ICONO_VFC = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    ' aria-hidden="true"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8'
    ' 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/></svg>'
)
ICONO_BB = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    ' aria-hidden="true"><rect x="2" y="7" width="18" height="10" rx="2"/>'
    '<path d="M22 11v2"/><path d="M6 10v4M10 10v4"/></svg>'
)
ICONO_PESO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    ' aria-hidden="true"><path d="M5 20h14"/><path d="M7 20V9a5 5 0 0 1 10 0v11"/></svg>'
)
ICONO_FC = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    ' aria-hidden="true"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>'
)


@dataclass(frozen=True)
class Tarjeta:
    """Una cifra de Hoy: valor, comparación con lo normal y mini-gráfica o icono."""

    titulo: str
    valor: str
    unidad: str = ""
    detalle: str = ""
    comparacion: str = ""
    clase: str = "neutro"  # bien / mal / neutro: sentido del cambio por si subir es bueno
    spark: Markup = Markup("")
    fecha: str = ""  # si el valor no es del día que se mira
    icono: str = ""
    color_icono: str = ""
    grafico: Markup = Markup("")


def _tiempo_hasta(inicio: str, ahora_dt: datetime) -> str:
    """Devuelve 'en X h' o 'en X min' si es hoy en el futuro."""
    try:
        partes = [int(x) for x in inicio.split(":")[:2]]
        inicio_dt = ahora_dt.replace(hour=partes[0], minute=partes[1], second=0, microsecond=0)
        diff_s = (inicio_dt - ahora_dt).total_seconds()
        if diff_s <= 0:
            return ""
        horas = diff_s / 3600
        if horas >= 1:
            return f"en {round(horas)} h"
        minutos = round(diff_s / 60)
        return f"en {minutos} min" if minutos > 0 else "ahora"
    except Exception:
        return ""


def _resumen_comida_dia(
    con: sqlite3.Connection, fecha: date, hoy_dia: date
) -> tuple[db.TotalesDia, nutricion.ObjetivoDia | None]:
    """Totales y objetivo de nutrición de un día."""
    totales = db.totales_dia(con, fecha.isoformat())
    objetivo = nutricion.objetivo_dia(con, fecha, hoy_dia)
    return totales, objetivo


def _clase(clave: str, diferencia: float, decimales: int) -> str:
    if abs(diferencia) < 0.5 * 10**-decimales:
        return "neutro"
    mejor = CATALOGO[clave].mejor
    if mejor is None:
        return "neutro"
    return "bien" if (diferencia > 0) == (mejor == "alto") else "mal"


def _comparacion(
    clave: str, valor: float, referencia: float | None, cuando: str
) -> tuple[str, str]:
    """("+3 lpm frente a la media de 28 días", "mal")."""
    if referencia is None:
        return "", "neutro"
    d = CATALOGO[clave]
    diferencia = valor - referencia
    signo = "+" if diferencia > 0 else "−" if diferencia < 0 else "±"
    unidad = f" {d.unidad}" if d.unidad else ""
    return (
        f"{signo}{cifra(abs(diferencia), d.decimales)}{unidad} {cuando}",
        _clase(clave, diferencia, d.decimales),
    )


def _tarjetas(con: sqlite3.Connection, fecha: date) -> list[Tarjeta]:
    hoy = fecha.isoformat()
    d = db.dia(con, hoy)
    desde_spark = (fecha - timedelta(days=DIAS_SPARK - 1)).isoformat()
    desde_media = (fecha - timedelta(days=DIAS_MEDIA)).isoformat()
    ayer = (fecha - timedelta(days=1)).isoformat()
    claves = ("sueno_h", "vfc_noche", "fc_reposo", "bb_despertar")
    sparks = db.series(con, claves, desde_spark, hoy)
    medias_series = db.series(con, claves, desde_media, ayer)

    def spark(clave: str) -> Markup:
        por_fecha = {v.fecha: v.valor for v in sparks[clave]}
        dias_ = [(fecha - timedelta(days=i)).isoformat() for i in range(DIAS_SPARK - 1, -1, -1)]
        return graficas.sparkline([por_fecha.get(x) for x in dias_])

    def media(clave: str) -> float | None:
        numeros = [v.valor for v in medias_series[clave] if v.valor is not None]
        return mean(numeros) if len(numeros) >= 7 else None

    def normal(clave: str) -> tuple[str, str]:
        v = d.get(clave)
        if v is None or v.valor is None:
            return "", "neutro"
        return _comparacion(clave, v.valor, media(clave), f"frente a tu media de {DIAS_MEDIA} días")

    def valor(clave: str) -> float | None:
        v = d.get(clave)
        return v.valor if v else None

    tarjetas: list[Tarjeta] = []

    # 1. Sueño (violeta)
    sueno = valor("sueno_h")
    puntuacion = d.get("sueno_puntuacion")
    profundo = valor("sueno_profundo_h")
    rem = valor("sueno_rem_h")
    ligero = valor("sueno_ligero_h")
    despierto = valor("sueno_despierto_h")
    partes_sueno = []
    if puntuacion and puntuacion.valor is not None:
        partes_sueno.append(f"Puntuación {cifra(puntuacion.valor)}")
    if profundo is not None:
        partes_sueno.append(f"{round(profundo * 60)} min profundo")
    elif puntuacion and puntuacion.texto:
        partes_sueno.append(texto("sueno_puntuacion", puntuacion.texto))
    detalle_sueno = " · ".join(partes_sueno)
    comp, clase = normal("sueno_h")
    graf_sueno = graficas.fases_sueno(profundo, rem, ligero, despierto)
    tarjetas.append(
        Tarjeta(
            titulo="Sueño",
            valor=horas_y_minutos(sueno),
            unidad="",
            detalle=detalle_sueno,
            comparacion=comp,
            clase=clase,
            spark=spark("sueno_h"),
            icono=ICONO_SUENO,
            color_icono="violeta",
            grafico=graf_sueno,
        )
    )

    # 2. VFC noche (rojo)
    vfc_val = valor("vfc_noche")
    lo, hi = valor("vfc_base_baja"), valor("vfc_base_alta")
    if lo is not None and hi is not None and vfc_val is not None:
        rango_str = f"({cifra(lo)}–{cifra(hi)})"
        if vfc_val < lo:
            detalle_vfc = f"Por debajo de tu rango {rango_str}"
            clase_vfc = "mal"
        elif vfc_val > hi:
            detalle_vfc = f"Por encima de tu rango {rango_str}"
            clase_vfc = "mal"
        else:
            detalle_vfc = f"Dentro de tu rango {rango_str}"
            clase_vfc = "bien"
    else:
        vfc_obj = d.get("vfc_noche")
        partes_vfc = (
            [texto("vfc_noche", vfc_obj.texto).capitalize()] if vfc_obj and vfc_obj.texto else []
        )
        if lo is not None and hi is not None:
            partes_vfc.append(f"tu franja equilibrada: {cifra(lo)}–{cifra(hi)} ms")
        detalle_vfc = " · ".join(partes_vfc)
        _, clase_vfc = normal("vfc_noche")

    graf_vfc = graficas.rango(vfc_val, lo, hi)
    tarjetas.append(
        Tarjeta(
            titulo="VFC noche",
            valor=cifra(vfc_val),
            unidad="ms",
            detalle=detalle_vfc,
            comparacion="",
            clase=clase_vfc,
            spark=spark("vfc_noche"),
            icono=ICONO_VFC,
            color_icono="rojo",
            grafico=graf_vfc,
        )
    )

    # 3. Body Battery (verde)
    partes_bb = []
    if valor("bb_min") is not None:
        partes_bb.append(f"Ahora {cifra(valor('bb_min'))}")
    if valor("recuperacion_h") is not None:
        partes_bb.append(f"recuperación {cifra(valor('recuperacion_h'))} h")
    elif not partes_bb and valor("bb_max") is not None:
        partes_bb.append(f"Máxima {cifra(valor('bb_max'))}")
    detalle_bb = " · ".join(partes_bb)
    comp, clase = normal("bb_despertar")
    tarjetas.append(
        Tarjeta(
            titulo="Body Battery",
            valor=cifra(valor("bb_despertar")),
            unidad="al despertar",
            detalle=detalle_bb,
            comparacion=comp,
            clase=clase,
            spark=spark("bb_despertar"),
            icono=ICONO_BB,
            color_icono="verde",
            grafico=Markup(""),
        )
    )

    # 4. Peso (marca)
    peso = db.ultimo(con, "peso", hoy)
    if peso is not None and peso.valor is not None:
        antes = db.ultimo(
            con, "peso", (date.fromisoformat(peso.fecha) - timedelta(days=30)).isoformat()
        )
        comp, clase = ("", "neutro")
        if antes is not None and antes.valor is not None:
            comp, clase = _comparacion("peso", peso.valor, antes.valor, "en 30 días")
        serie_peso = db.serie(con, "peso", (fecha - timedelta(days=60)).isoformat(), hoy)
        spk_peso = graficas.sparkline([v.valor for v in serie_peso])
        tarjetas.append(
            Tarjeta(
                titulo="Peso",
                valor=cifra(peso.valor, 1),
                unidad="kg",
                detalle="",
                comparacion=comp,
                clase=clase,
                spark=spk_peso,
                fecha=peso.fecha if peso.fecha != hoy else "",
                icono=ICONO_PESO,
                color_icono="marca",
                grafico=spk_peso,
            )
        )

    # 5. FC en reposo (rojo)
    comp, clase = normal("fc_reposo")
    spk_fc = spark("fc_reposo")
    tarjetas.append(
        Tarjeta(
            titulo="FC en reposo",
            valor=cifra(valor("fc_reposo")),
            unidad="lpm",
            detalle="",
            comparacion=comp,
            clase=clase,
            spark=spk_fc,
            icono=ICONO_FC,
            color_icono="rojo",
            grafico=spk_fc,
        )
    )

    return tarjetas


def _rendimiento(con: sqlite3.Connection, fecha: date) -> list[dict[str, str]]:
    """VO2max, FTP y estado de entrenamiento: valores que cambian poco, con su fecha."""
    hoy = fecha.isoformat()
    filas = []
    general = db.ultimo(con, "vo2max", hoy)
    for clave, decimales in (("vo2max", 1), ("vo2max_ciclismo", 1), ("ftp", 0)):
        v = db.ultimo(con, clave, hoy)
        if v is not None and v.valor is not None:
            if clave == "vo2max_ciclismo" and general and general.valor == v.valor:
                continue  # el mismo que el general: no se repite
            d = CATALOGO[clave]
            filas.append(
                {
                    "titulo": d.nombre,
                    "valor": f"{cifra(v.valor, decimales)}{' ' + d.unidad if d.unidad else ''}",
                    "fecha": v.fecha,
                }
            )
    estado = db.ultimo(con, "estado_entreno", hoy)
    if estado is not None and estado.texto:
        filas.append(
            {
                "titulo": "Estado de entrenamiento",
                "valor": texto("estado_entreno", estado.texto).capitalize(),
                "fecha": estado.fecha,
            }
        )
    ratio = db.ultimo(con, "ratio_carga", hoy)
    if ratio is not None and ratio.valor is not None:
        filas.append(
            {
                "titulo": "Carga aguda / crónica",
                "valor": cifra(ratio.valor, 1),
                "fecha": ratio.fecha,
            }
        )
    return filas


def _carga_semanal(con: sqlite3.Connection, desde: date, hasta: date) -> list[tuple[str, float]]:
    """Carga de entrenamiento (Garmin) sumada por semana, de lunes a domingo."""
    lunes = desde - timedelta(days=desde.weekday())
    semanas: dict[date, float] = defaultdict(float)
    s = lunes
    while s <= hasta:
        semanas[s] = 0.0
        s += timedelta(days=7)
    for a in db.actividades(con, lunes.isoformat(), hasta.isoformat()):
        d = date.fromisoformat(a["fecha"])
        semanas[d - timedelta(days=d.weekday())] += a["carga"] or 0
    return [(f"{s.day}/{s.month}", round(v)) for s, v in sorted(semanas.items())]


def _graficas(
    con: sqlite3.Connection,
    claves: Sequence[str],
    desde: date,
    hasta: date,
    *,
    referencia: float | None = None,
    marcas: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Construye las gráficas de líneas para las métricas dadas en el periodo."""
    dias = (hasta - desde).days + 1
    fechas = [(desde + timedelta(days=i)).isoformat() for i in range(dias)]
    claves_consulta = list(claves)
    if "vfc_noche" in claves:
        if "vfc_base_baja" not in claves_consulta:
            claves_consulta.append("vfc_base_baja")
        if "vfc_base_alta" not in claves_consulta:
            claves_consulta.append("vfc_base_alta")

    datos = db.series(con, tuple(claves_consulta), desde.isoformat(), hasta.isoformat())
    por_fecha = {c: {v.fecha: v.valor for v in valores} for c, valores in datos.items()}

    def puntos(clave: str) -> list[tuple[str, float | None]]:
        valores_clave = por_fecha.get(clave, {})
        return [(f, valores_clave.get(f)) for f in fechas]

    franja_vfc = None
    if "vfc_noche" in claves:
        bajas = por_fecha.get("vfc_base_baja", {})
        altas = por_fecha.get("vfc_base_alta", {})
        franja_vfc = [(f, bajas.get(f), altas.get(f)) for f in fechas]

    ajustes: dict[str, tuple[str, bool, float, str]] = {
        "peso": ("Peso", True, 4, ""),
        "fc_reposo": ("FC en reposo", False, 10, ""),
        "vfc_noche": (
            "VFC nocturna",
            False,
            20,
            "La franja gris es tu VFC equilibrada según Garmin.",
        ),
        "sueno_h": ("Sueño", False, 4, ""),
        "preparacion": ("Preparación para entrenar", False, 0, ""),
        "bb_despertar": ("Body Battery al despertar", False, 0, ""),
        "vo2max": ("VO2max", True, 4, ""),
    }

    resultado = []
    for clave in claves:
        if clave not in ajustes:
            continue
        titulo, unir, rango, nota = ajustes[clave]
        d = CATALOGO[clave]
        g = graficas.linea(
            puntos(clave),
            decimales=d.decimales,
            unidad=d.unidad,
            titulo=titulo,
            unir_huecos=unir,
            rango_minimo=rango,
            banda=franja_vfc if clave == "vfc_noche" else None,
            referencia=referencia if clave == "peso" else None,
            marcas=marcas if clave == "peso" else (),
        )
        resultado.append(
            {
                "clave": clave,
                "titulo": titulo,
                "unidad": d.unidad,
                "grafica": g,
                "nota": nota,
            }
        )
    return resultado


def _actividades(filas: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            **dict(a),
            "nombre_tipo": nombre_tipo(a["tipo"]),
            "hora": a["inicio"][11:16],
            "duracion": horas_y_minutos(a["duracion_s"] / 3600) if a["duracion_s"] else "",
            "km": cifra(a["distancia_m"] / 1000, 1) if a["distancia_m"] else "",
        }
        for a in filas
    ]


def momento_por_hora(hora: int) -> str:
    """Devuelve el momento según la hora (0-23).

    desayuno si hora < 10, media mañana si < 13, comida si < 16, merienda si < 20, cena el resto.
    """
    if hora < 10:
        return "desayuno"
    if hora < 13:
        return "media mañana"
    if hora < 16:
        return "comida"
    if hora < 20:
        return "merienda"
    return "cena"


def momento_valido(valor: str | None) -> str | None:
    """Devuelve el valor si está en MOMENTOS, si no None."""
    if valor in db.MOMENTOS:
        return valor
    return None


@dataclass(frozen=True)
class DatosProducto:
    """Datos validados del formulario de un producto propio."""

    codigo: str | None
    nombre: str
    marca: str | None
    unidad: str
    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None
    origen: str
    racion_g: float | None = None
    racion_nombre: str | None = None


def datos_producto(datos: dict[str, str]) -> DatosProducto:
    """Valida los datos de un producto desde el formulario web."""
    codigo_raw = alimentos.normalizar_codigo(datos.get("codigo", ""))
    if codigo_raw:
        if not alimentos.codigo_valido(codigo_raw):
            raise ValueError("Código de barras no válido")
        codigo = codigo_raw
    else:
        codigo = None

    nombre = datos.get("nombre", "").strip()
    if not nombre:
        raise ValueError("Falta el nombre")
    if len(nombre) > 120:
        nombre = nombre[:120]

    marca_raw = datos.get("marca", "").strip()
    if marca_raw:
        marca = marca_raw[:120] if len(marca_raw) > 120 else marca_raw
    else:
        marca = None

    unidad = datos.get("unidad", "").strip()
    if unidad not in ("g", "ml"):
        raise ValueError("Unidad no válida")

    def _parsear_nutriente(campo: str, max_valor: float) -> float | None:
        v = datos.get(campo, "").strip()
        if not v:
            return None
        v = v.replace(",", ".")
        try:
            val = float(v)
            if not math.isfinite(val):
                raise ValueError(f"El valor de {campo} no es un número")
        except ValueError:
            raise ValueError(f"El valor de {campo} no es un número") from None

        if not (0 <= val <= max_valor):
            raise ValueError(f"El valor de {campo} está fuera de rango")
        return val

    kcal = _parsear_nutriente("kcal", 950.0)
    proteinas = _parsear_nutriente("proteinas", 100.0)
    hidratos = _parsear_nutriente("hidratos", 100.0)
    grasas = _parsear_nutriente("grasas", 100.0)

    racion_g_raw = datos.get("racion_g", "").strip()
    racion_nombre_raw = datos.get("racion_nombre", "").strip()
    if racion_nombre_raw and not racion_g_raw:
        raise ValueError("Falta el peso de la ración")

    if racion_g_raw:
        v_rac = racion_g_raw.replace(",", ".")
        try:
            val_rac = float(v_rac)
            if not math.isfinite(val_rac):
                raise ValueError("El peso de la ración no es un número")
        except ValueError:
            raise ValueError("El peso de la ración no es un número") from None

        if not (1 <= val_rac <= 2000):
            raise ValueError("El peso de la ración debe estar entre 1 y 2000")
        racion_g = val_rac
        racion_nombre = raciones.limpiar_racion_nombre(racion_nombre_raw) or "ración"
    else:
        racion_g = None
        racion_nombre = None

    if datos.get("ia") == "1":
        origen = "etiqueta-ia"
    elif datos.get("origen") == "ciqual":
        origen = "ciqual"
    else:
        origen = "manual"

    return DatosProducto(
        codigo=codigo,
        nombre=nombre,
        marca=marca,
        unidad=unidad,
        kcal=kcal,
        proteinas=proteinas,
        hidratos=hidratos,
        grasas=grasas,
        origen=origen,
        racion_g=racion_g,
        racion_nombre=racion_nombre,
    )


@dataclass(frozen=True)
class DiaPlan:
    """Un día del panel de planificación con su plan y actividades hechas."""

    fecha: str
    titulo: str  # "Hoy", "Mañana", "Ayer" o fecha_corta
    es_hoy: bool
    futuro: bool
    plan: list[dict[str, Any]]
    hechas: list[dict[str, Any]]


def dias_plan(
    planificado: list[db.Planificado],
    actividades: list[sqlite3.Row],
    desde: date,
    hasta: date,
    hoy: date,
) -> list[DiaPlan]:
    """Construye la lista de días con su plan y actividades hechas."""
    actividades_por_dia: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for a in actividades:
        f = a["fecha"] if "fecha" in a.keys() else a["inicio"][:10]
        actividades_por_dia[f].append(a)

    plan_por_dia: dict[str, list[db.Planificado]] = defaultdict(list)
    for p in planificado:
        plan_por_dia[p.fecha].append(p)

    resultado: list[DiaPlan] = []
    d = desde
    while d <= hasta:
        f_str = d.isoformat()
        if d == hoy:
            titulo = "Hoy"
        elif d == hoy + timedelta(days=1):
            titulo = "Mañana"
        elif d == hoy - timedelta(days=1):
            titulo = "Ayer"
        else:
            titulo = graficas.fecha_corta(f_str)

        acts_dia = actividades_por_dia.get(f_str, [])
        hechas = _actividades(acts_dia)

        plan_dia: list[dict[str, Any]] = []
        for p in plan_por_dia.get(f_str, []):
            plan_dia.append(
                {
                    "hora": p.inicio or "",
                    "nombre": nombre_tipo(p.tipo) if p.tipo else p.titulo,
                    "titulo": p.titulo,
                    "duracion": f"{p.duracion_min} min" if p.duracion_min else "",
                    "intensidad": p.intensidad,
                    "origen": p.origen,
                    "tipo": p.tipo,
                    "hecho": calendario.hecho(p, acts_dia),
                }
            )

        resultado.append(
            DiaPlan(
                fecha=f_str,
                titulo=titulo,
                es_hoy=d == hoy,
                futuro=d > hoy,
                plan=plan_dia,
                hechas=hechas,
            )
        )
        d += timedelta(days=1)

    return resultado


def entradas_semana(datos: dict[str, str]) -> list[db.EntradaSemana]:
    """Valida y extrae las entradas de la semana tipo del formulario web."""
    filas_raw = datos.get("filas", "").strip()
    try:
        num_filas = int(filas_raw)
    except ValueError:
        raise ValueError("Formulario no válido") from None

    if not (0 <= num_filas <= 50):
        raise ValueError("Formulario no válido")

    entradas: list[db.EntradaSemana] = []
    for i in range(num_filas):
        if datos.get(f"quitar-{i}") == "on":
            continue
        tipo = datos.get(f"tipo-{i}", "").strip()
        if not tipo:
            continue

        dia_raw = datos.get(f"dia-{i}", "").strip()
        try:
            dia = int(dia_raw)
            if not (0 <= dia <= 6):
                raise ValueError
        except ValueError:
            raise ValueError(f"Fila {i + 1}: día no válido") from None

        inicio_raw = datos.get(f"inicio-{i}", "").strip()
        if inicio_raw:
            partes = inicio_raw.split(":")
            if len(partes) != 2:
                raise ValueError(f"Fila {i + 1}: hora no válida")
            try:
                h, m = int(partes[0]), int(partes[1])
                if not (
                    0 <= h <= 23 and 0 <= m <= 59 and len(partes[0]) == 2 and len(partes[1]) == 2
                ):
                    raise ValueError
            except ValueError:
                raise ValueError(f"Fila {i + 1}: hora no válida") from None
            inicio: str | None = f"{h:02d}:{m:02d}"
        else:
            inicio = None

        duracion_raw = datos.get(f"duracion-{i}", "").strip()
        if duracion_raw:
            try:
                duracion: int | None = int(duracion_raw)
                if not (5 <= duracion <= 600):
                    raise ValueError
            except ValueError:
                raise ValueError(f"Fila {i + 1}: duración no válida") from None
        else:
            duracion = None

        if tipo not in metricas.TIPOS_ACTIVIDAD:
            raise ValueError(f"Fila {i + 1}: tipo no válido")

        intensidad = datos.get(f"intensidad-{i}", "").strip()
        if intensidad not in lectura.INTENSIDADES:
            raise ValueError(f"Fila {i + 1}: intensidad no válida")

        entradas.append(
            db.EntradaSemana(
                dia=dia,
                inicio=inicio,
                duracion_min=duracion,
                tipo=tipo,
                intensidad=intensidad,
            )
        )
    return entradas


# --- Aplicación ----------------------------------------------------------------


async def _leer_foto(request: Request) -> bytes | Response:
    """La foto del cuerpo de la petición, o la respuesta de error (tamaño o tipo)."""
    demasiado = JSONResponse({"error": "La foto es demasiado grande"}, 413)
    try:
        if int(request.headers.get("content-length") or 0) > ia.MAX_BYTES_IMAGEN:
            return demasiado
    except ValueError:
        pass
    partes: list[bytes] = []
    tam = 0
    async for trozo in request.stream():
        tam += len(trozo)
        if tam > ia.MAX_BYTES_IMAGEN:
            return demasiado
        partes.append(trozo)
    cuerpo = b"".join(partes)
    if ia.tipo_imagen(cuerpo) is None:
        return JSONResponse({"error": "El fichero no es una imagen JPEG, PNG o WebP"}, 415)
    return cuerpo


@dataclass(frozen=True)
class PeticionIA:
    """Lo que se pide analizar: fecha y momento del diario y el texto (puede ir vacío con foto)."""

    fecha: str
    momento: str
    texto: str


def peticion_ia(datos: dict[str, str], *, hay_foto: bool, hoy_: date) -> PeticionIA:
    """Valida la petición de "Añadir con IA". ValueError con el mensaje para el usuario."""
    texto = datos.get("texto", "").strip()
    if len(texto) > ia.MAX_TEXTO_COMIDA:
        raise ValueError("El texto es demasiado largo")
    if len(texto) < 2 and not hay_foto:
        raise ValueError("Escribe qué has comido o añade una foto")
    fecha_raw = datos.get("fecha", "").strip()
    try:
        fecha = date.fromisoformat(fecha_raw) if fecha_raw else hoy_
    except ValueError:
        raise ValueError("Fecha no válida") from None
    if fecha > hoy_:
        raise ValueError("La fecha no puede ser posterior a hoy")
    momento = momento_valido(datos.get("momento"))
    if momento is None:
        raise ValueError("Momento no válido")
    return PeticionIA(fecha.isoformat(), momento, texto)


@dataclass(frozen=True)
class AlimentoFormulario:
    """Un alimento revisado en el formulario de "Añadir con IA"."""

    nombre: str
    cantidad: float
    unidad: str
    momento: str
    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None
    producto: bool


def alimentos_desde_formulario(datos: dict[str, str], n: int) -> list[AlimentoFormulario]:
    """Los alimentos marcados de las `n` tarjetas (`a-<i>-…`). ValueError si alguno está mal."""
    elegidos: list[AlimentoFormulario] = []
    for i in range(n):

        def campo(nombre: str, i: int = i) -> str:
            return datos.get(f"a-{i}-{nombre}", "").strip()

        if f"a-{i}-incluir" not in datos:
            continue
        nombre = campo("nombre")
        if not nombre or len(nombre) > 120:
            raise ValueError("Cada alimento necesita un nombre (como mucho 120 caracteres)")

        def numero(clave: str, nombre: str = nombre) -> float | None:
            valor = campo(clave).replace(",", ".")
            if not valor:
                return None
            try:
                v = float(valor)
            except ValueError:
                raise ValueError(f"Valor no válido en {nombre}") from None
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"Valor no válido en {nombre}")
            return v

        cantidad = numero("cantidad")
        if cantidad is None or not 0 < cantidad <= 5000:
            raise ValueError(f"Cantidad no válida en {nombre}")
        unidad = campo("unidad")
        if unidad not in ("g", "ml"):
            raise ValueError(f"Unidad no válida en {nombre}")
        momento = momento_valido(campo("momento"))
        if momento is None:
            raise ValueError(f"Momento no válido en {nombre}")
        elegidos.append(
            AlimentoFormulario(
                nombre=nombre,
                cantidad=cantidad,
                unidad=unidad,
                momento=momento,
                kcal=numero("kcal"),
                proteinas=numero("proteinas"),
                hidratos=numero("hidratos"),
                grasas=numero("grasas"),
                producto=f"a-{i}-producto" in datos,
            )
        )
    if not elegidos:
        raise ValueError("No has marcado ningún alimento")
    return elegidos


def por_100(total: float | None, cantidad: float) -> float | None:
    """Valor por 100 g/ml a partir del total de una cantidad, con un decimal."""
    return None if total is None else round(total * 100 / cantidad, 1)


def crear_app(
    settings: Settings,
    abrir_db: Callable[[], sqlite3.Connection],
    conectar_garmin: Callable[[], FuenteGarmin],
    lector: Callable[[sqlite3.Connection, date], dict[str, Any]] | None = None,
    *,
    hoy: Callable[[], date] = lambda: datetime.now(ZONA).date(),
    crear_cliente_garmin: Callable[[str, str], Any] = garmin.cliente_nuevo,
    reloj: Callable[[], float] = time.monotonic,
    cliente_off: httpx.Client | None = None,
    lector_etiqueta: Callable[[bytes], ia.Etiqueta] | None = None,
    cliente_calendario: httpx.Client | None = None,
    pedir_consejo: Callable[[sqlite3.Connection, str], dict[str, Any]] | None = None,
    analizar: Callable[[str, bytes | None], list[ia.AlimentoIA]] | None = None,
    publica: bool = False,
    crear_login: Callable[[str, str], acceso.Login] | None = None,
) -> Starlette:
    """Construye la aplicación web."""
    leer = lector_etiqueta or (lambda imagen: ia.leer_etiqueta(settings, imagen))
    analizar_fn = analizar or (lambda t, img: ia.analizar_comida(settings, t, img))
    generando = threading.Lock()
    aconsejando = threading.Lock()
    error_consejo: dict[str, str | None] = {"texto": None}  # último fallo, para enseñarlo una vez
    entrando = threading.Lock()
    entrada = _EntradaGarmin()

    def _leer(con: sqlite3.Connection, fecha: date) -> dict[str, Any]:
        if lector is not None:
            return lector(con, fecha)
        return lectura.generar(con, settings, fecha)

    def _fecha(request: Request) -> date:
        try:
            pedida = date.fromisoformat(request.query_params.get("fecha", ""))
        except ValueError:
            return hoy()
        return min(pedida, hoy())

    def ver_hoy(request: Request) -> Response:
        fecha = _fecha(request)
        con = abrir_db()
        try:
            es_hoy = fecha == hoy()
            d = db.dia(con, fecha.isoformat())
            prep = d.get("preparacion")
            prep_valor = prep.valor if prep else None
            anillo_prep = graficas.anillo(
                prep_valor / 100 if prep_valor is not None else None,
                clase="preparacion",
                tamano=76,
                grosor=8,
                texto=str(round(prep_valor)) if prep_valor is not None else "—",
                etiqueta=(
                    f"Preparación {round(prep_valor)} de 100"
                    if prep_valor is not None
                    else "Sin preparación"
                ),
            )

            plan_hoy: list[dict[str, Any]] = []
            dosis_hoy: dict[str, str] | None = None
            comida_hoy: dict[str, Any] | None = None

            if es_hoy:
                items_plan = calendario.plan(con, fecha, fecha, fecha)
                acts_hoy = db.actividades(con, fecha.isoformat(), fecha.isoformat())
                ahora_dt = datetime.now(ZONA)
                for p in items_plan:
                    esta_hecho = calendario.hecho(p, acts_hoy)
                    deporte = metricas.nombre_tipo(p.tipo) if p.tipo else p.titulo
                    hora = p.inicio[:5] if p.inicio else ""
                    tit = f"{deporte} · {hora}" if hora else deporte
                    detalles = []
                    if p.duracion_min:
                        detalles.append(f"{p.duracion_min} min")
                    if p.intensidad:
                        detalles.append(f"intensidad {p.intensidad}")
                    k = nutricion.kcal_tipica(con, p, fecha)
                    if k > 0:
                        detalles.append(f"~{cifra(k)} kcal")

                    if esta_hecho:
                        pastilla_txt = "hecho"
                        pastilla_cls = "bien"
                    else:
                        t_hasta = _tiempo_hasta(p.inicio, ahora_dt) if p.inicio else ""
                        pastilla_txt = t_hasta or ""
                        pastilla_cls = "cuidado" if pastilla_txt else ""

                    plan_hoy.append(
                        {
                            "titulo": tit,
                            "detalle": " · ".join(detalles),
                            "pastilla_texto": pastilla_txt,
                            "pastilla_clase": pastilla_cls,
                        }
                    )

                trat = _tratamiento(con, fecha)
                if trat is not None and (trat.get("pauta") or trat.get("ultima")):
                    p_prox = trat.get("prox")
                    pauta = trat.get("pauta")
                    if p_prox and pauta:
                        farmaco = pauta.farmaco
                        dosis_mg = gramos(pauta.dosis_mg)
                        if p_prox.estado == "hoy":
                            t_dosis = f"{farmaco} {dosis_mg} mg · hoy"
                            d_dosis = f"Toca hoy · {trat['zona_sugerida']}"
                        elif p_prox.estado == "esperar":
                            t_dosis = f"{farmaco} {dosis_mg} mg · {dia_corto(p_prox.prevista)}"
                            d_dosis = (
                                f"Puedes adelantarla desde el {dia_corto(p_prox.adelantar_desde)}"
                                f" · {trat['zona_sugerida']}"
                            )
                        elif p_prox.estado == "puede_adelantar":
                            t_dosis = f"{farmaco} {dosis_mg} mg · {dia_corto(p_prox.prevista)}"
                            d_dosis = f"Si quieres adelantarla, ya puedes · {trat['zona_sugerida']}"
                        elif p_prox.estado == "retrasada":
                            t_dosis = f"{farmaco} {dosis_mg} mg · {dia_corto(p_prox.prevista)}"
                            d_dosis = (
                                f"Tocaba el {dia_corto(p_prox.prevista)}. Aún puedes ponértela"
                                f" hasta el {dia_corto(p_prox.limite)} · {trat['zona_sugerida']}"
                            )
                        elif p_prox.estado == "saltar":
                            t_dosis = f"{farmaco} {dosis_mg} mg · {dia_corto(p_prox.prevista)}"
                            d_dosis = (
                                f"Han pasado más de {pauta.margen_olvido_dias} días: sáltala y"
                                f" sigue el {dia_corto(p_prox.siguiente_habitual)}"
                            )
                        else:
                            t_dosis = f"{farmaco} {dosis_mg} mg"
                            d_dosis = f"Zona sugerida: {trat['zona_sugerida']}"
                        dosis_hoy = {"titulo": t_dosis, "detalle": d_dosis}
                    elif pauta:
                        t_dosis = f"{pauta.farmaco} {gramos(pauta.dosis_mg)} mg"
                        d_dosis = f"Aún no hay dosis registradas · {trat['zona_sugerida']}"
                        dosis_hoy = {"titulo": t_dosis, "detalle": d_dosis}

                totales_c, obj_c = _resumen_comida_dia(con, fecha, fecha)
                f_prot = (
                    totales_c.total.proteinas / obj_c.total.proteinas
                    if obj_c and obj_c.total.proteinas > 0
                    else None
                )
                f_kcal = (
                    totales_c.total.kcal / obj_c.total.kcal
                    if obj_c and obj_c.total.kcal > 0
                    else None
                )
                etiqueta = (
                    f"Proteína {gramos(totales_c.total.proteinas)} de"
                    f" {gramos(obj_c.total.proteinas)} gramos"
                    if obj_c
                    else "Comida de hoy"
                )
                graf = graficas.anillos(
                    f_prot,
                    f_kcal,
                    clase_ext="proteina",
                    clase_int="kcal",
                    tamano=64,
                    etiqueta=etiqueta,
                )
                if obj_c:
                    cifra_prot = (
                        f"{cifra(totales_c.total.proteinas)} / {cifra(obj_c.total.proteinas)}"
                    )
                    txt_kcal = f"{cifra(totales_c.total.kcal)} / {cifra(obj_c.total.kcal)} kcal"
                else:
                    cifra_prot = cifra(totales_c.total.proteinas)
                    txt_kcal = f"{cifra(totales_c.total.kcal)} kcal"
                comida_hoy = {
                    "grafico": graf,
                    "cifra_proteina": cifra_prot,
                    "texto_kcal": txt_kcal,
                }

            return _render(
                "hoy.html",
                seccion="hoy",
                fecha=fecha.isoformat(),
                es_hoy=es_hoy,
                anterior=(fecha - timedelta(days=1)).isoformat(),
                siguiente=(fecha + timedelta(days=1)).isoformat() if fecha < hoy() else None,
                lectura=db.lectura(con, fecha.isoformat()),
                generando=generando.locked(),
                anillo_prep=anillo_prep,
                tarjetas=_tarjetas(con, fecha),
                plan_hoy=plan_hoy,
                dosis_hoy=dosis_hoy,
                comida_hoy=comida_hoy,
                sync=db.ultima_sincronizacion(con, GARMIN),
                mensaje=request.query_params.get("mensaje"),
            )
        finally:
            con.close()

    def ver_salud(request: Request) -> Response:
        try:
            dias = int(request.query_params.get("dias", "90"))
        except ValueError:
            dias = 90
        dias = dias if dias in PERIODOS else 90
        fin = hoy()
        inicio = fin - timedelta(days=dias - 1)
        con = abrir_db()
        try:
            trat = _tratamiento(con, fin)
            if trat is not None and trat["ultima"] is None and not db.hay_pauta(con):
                trat = None
            ajustes = db.ajustes_comida(con)
            peso_objetivo_kg = ajustes.peso_objetivo_kg

            lista_dosis = db.dosis(con)
            marcas_dosis = [
                d.fecha for d in lista_dosis if inicio.isoformat() <= d.fecha <= fin.isoformat()
            ]

            graficas_salud = _graficas(
                con,
                ("peso", "fc_reposo", "vfc_noche", "sueno_h", "bb_despertar"),
                inicio,
                fin,
                referencia=peso_objetivo_kg,
                marcas=marcas_dosis,
            )
            ult_grasa = db.ultimo(con, "grasa", fin.isoformat())
            ult_musculo = db.ultimo(con, "musculo", fin.isoformat())
            ult_peso = db.ultimo(con, "peso", fin.isoformat())

            # Diferencia con el primer peso del periodo elegido
            serie_peso = db.serie(con, "peso", inicio.isoformat(), fin.isoformat())
            pesos_con_valor = [(v.fecha, v.valor) for v in serie_peso if v.valor is not None]
            peso_diff: str | None = None
            peso_diff_baja: bool = False
            if len(pesos_con_valor) >= 2:
                f_primero, v_primero = pesos_con_valor[0]
                _, v_ultimo = pesos_con_valor[-1]
                diff = round(v_ultimo - v_primero, 1)
                f_corta = graficas.fecha_corta(f_primero)
                if diff < 0:
                    peso_diff = f"−{cifra(abs(diff), 1)} desde el {f_corta}"
                    peso_diff_baja = True
                elif diff > 0:
                    peso_diff = f"+{cifra(diff, 1)} desde el {f_corta}"
                else:
                    peso_diff = f"0,0 desde el {f_corta}"

            # Mosaicos: FC en reposo y VFC semanal
            ult_fc = db.ultimo(con, "fc_reposo", fin.isoformat())
            ult_vfc = db.ultimo(con, "vfc_semana", fin.isoformat())
            if ult_vfc is None or ult_vfc.valor is None:
                ult_vfc = db.ultimo(con, "vfc_noche", fin.isoformat())

            f_spark_ini = (fin - timedelta(days=29)).isoformat()
            sparks_data = db.series(
                con, ("fc_reposo", "vfc_semana", "vfc_noche"), f_spark_ini, fin.isoformat()
            )
            dias_spark = [(fin - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]

            map_fc = {v.fecha: v.valor for v in sparks_data.get("fc_reposo", [])}
            spark_fc = graficas.sparkline([map_fc.get(d) for d in dias_spark], ancho=120, alto=28)

            map_vfc_sem = {v.fecha: v.valor for v in sparks_data.get("vfc_semana", [])}
            if any(v is not None for v in map_vfc_sem.values()):
                spark_vfc = graficas.sparkline(
                    [map_vfc_sem.get(d) for d in dias_spark], ancho=120, alto=28
                )
            else:
                map_vfc_noc = {v.fecha: v.valor for v in sparks_data.get("vfc_noche", [])}
                spark_vfc = graficas.sparkline(
                    [map_vfc_noc.get(d) for d in dias_spark], ancho=120, alto=28
                )
        finally:
            con.close()

        peso_grafica = next((g for g in graficas_salud if g["clave"] == "peso"), None)
        resto_graficas = [g for g in graficas_salud if g["clave"] != "peso"]

        grasa_str = (
            f"{cifra(ult_grasa.valor, 1)} %" if ult_grasa and ult_grasa.valor is not None else None
        )
        musculo_str = (
            f"{cifra(ult_musculo.valor, 1)} kg"
            if ult_musculo and ult_musculo.valor is not None
            else None
        )

        return _render(
            "salud.html",
            seccion="salud",
            dias=dias,
            periodos=PERIODOS,
            tratamiento=trat,
            peso=peso_grafica,
            ultimo_peso=ult_peso.valor if ult_peso and ult_peso.valor is not None else None,
            peso_objetivo=peso_objetivo_kg,
            peso_diff=peso_diff,
            peso_diff_baja=peso_diff_baja,
            grasa=grasa_str,
            musculo=musculo_str,
            ult_fc=ult_fc,
            spark_fc=spark_fc,
            ult_vfc=ult_vfc,
            spark_vfc=spark_vfc,
            graficas=resto_graficas,
        )

    def redirigir_tendencias(request: Request) -> Response:
        qs = request.url.query
        destino = f"/salud?{qs}" if qs else "/salud"
        return RedirectResponse(destino, status_code=301)

    def ver_actividades(request: Request) -> Response:
        try:
            dias = int(request.query_params.get("dias", "30"))
        except ValueError:
            dias = 30
        dias = dias if dias in PERIODOS else 30
        fin = hoy()
        inicio = fin - timedelta(days=dias - 1)
        desde_plan = fin - timedelta(days=DIAS_PASADOS_PLAN)
        hasta_plan = fin + timedelta(days=DIAS_FUTUROS_PLAN)
        semana_7dias = (fin - timedelta(days=6)).isoformat()
        con = abrir_db()
        try:
            filas = db.actividades(con, inicio.isoformat(), fin.isoformat())
            filas_7dias = db.actividades(con, semana_7dias, fin.isoformat())
            plan_items = calendario.plan(con, desde_plan, hasta_plan, fin)
            acts_plan = db.actividades(con, desde_plan.isoformat(), hasta_plan.isoformat())
            sync_cal = db.ultima_sincronizacion(con, calendario.GOOGLE)
            graficas_act = _graficas(con, ("preparacion", "vo2max"), inicio, fin)
            carga = _carga_semanal(con, inicio, fin)
            rendimiento = _rendimiento(con, fin)
        finally:
            con.close()
        g_carga = graficas.columnas(carga, titulo="Carga de entrenamiento por semana")
        graficas_act.append(
            {
                "clave": "carga_semanal",
                "titulo": "Carga de entrenamiento por semana",
                "unidad": "",
                "grafica": g_carga,
                "nota": "Suma de la carga de Garmin de cada semana, de lunes a domingo.",
            }
        )
        dias_p = dias_plan(plan_items, acts_plan, desde_plan, hasta_plan, fin)
        return _render(
            "actividades.html",
            seccion="actividad",
            dias=dias,
            periodos=PERIODOS,
            rendimiento=rendimiento,
            actividades_7dias=_actividades(filas_7dias),
            actividades=_actividades(filas),
            dias_plan=dias_p,
            sync_calendario=sync_cal,
            tipos_actividad=metricas.TIPOS_ACTIVIDAD,
            graficas=graficas_act,
            mensaje=request.query_params.get("mensaje"),
        )

    async def sincronizar_calendario_web(_request: Request) -> Response:
        if settings.gcal_ics is None:
            return _volver("Falta SALUD_GCAL_ICS en el .env", "/actividades")

        url = settings.gcal_ics

        def _sync() -> int:
            con = abrir_db()
            try:
                res = calendario.sincronizar_calendario(con, url, hoy(), cliente=cliente_calendario)
                return res.deportes
            finally:
                con.close()

        try:
            n = await run_in_threadpool(_sync)
            return _volver(f"Calendario al día: {n} deportes", "/actividades")
        except calendario.ErrorCalendario as exc:
            return _volver(str(exc), "/actividades")

    async def calendario_titulo_web(request: Request) -> Response:
        datos = await _formulario(request)
        titulo = datos.get("titulo", "").strip()
        if not titulo:
            return _volver("El título no puede estar vacío", "/actividades")

        deporte = datos.get("deporte", "").strip()
        if deporte == "0":

            def _desmarcar() -> None:
                con = abrir_db()
                try:
                    db.marcar_titulo(con, titulo=titulo, deporte=False, tipo=None)
                    db.borrar_planificado_titulo(con, titulo)
                finally:
                    con.close()

            await run_in_threadpool(_desmarcar)
            return _volver(f"«{titulo}» ya no cuenta como deporte", "/actividades")

        if deporte == "1":
            tipo = datos.get("tipo", "").strip()
            if tipo not in metricas.TIPOS_ACTIVIDAD:
                return _volver("Tipo de deporte no válido", "/actividades")

            def _marcar() -> None:
                con = abrir_db()
                try:
                    db.marcar_titulo(con, titulo=titulo, deporte=True, tipo=tipo)
                    db.actualizar_tipo_titulo(
                        con,
                        titulo=titulo,
                        tipo=tipo,
                        intensidad=calendario.intensidad(tipo),
                    )
                finally:
                    con.close()

            await run_in_threadpool(_marcar)
            return _volver(f"«{titulo}» es {nombre_tipo(tipo)}", "/actividades")

        return _volver("Opción no válida", "/actividades")

    def ver_semana_tipo(request: Request) -> Response:
        proponer = request.query_params.get("proponer") == "1"
        aviso: str | None = None
        mensaje = request.query_params.get("mensaje")

        con = abrir_db()
        try:
            if proponer:
                filas_semana = calendario.proponer_semana_tipo(con, hoy())
                if filas_semana:
                    aviso = (
                        "Propuesta a partir de tus actividades de las últimas 8 semanas:"
                        " revísala y guarda."
                    )
                else:
                    aviso = "No hay suficientes actividades repetidas para proponer una semana tipo"
            else:
                filas_semana = db.semana_tipo(con)
        finally:
            con.close()

        total_filas = len(filas_semana) + FILAS_VACIAS_SEMANA
        filas_contexto = []
        for i in range(total_filas):
            if i < len(filas_semana):
                e = filas_semana[i]
                filas_contexto.append(
                    {
                        "indice": i,
                        "dia": e.dia,
                        "inicio": e.inicio or "",
                        "tipo": e.tipo,
                        "duracion": str(e.duracion_min) if e.duracion_min is not None else "",
                        "intensidad": e.intensidad,
                    }
                )
            else:
                filas_contexto.append(
                    {
                        "indice": i,
                        "dia": 0,
                        "inicio": "",
                        "tipo": "",
                        "duracion": "",
                        "intensidad": "moderada",
                    }
                )

        return _render(
            "semana_tipo.html",
            seccion="actividades",
            filas=filas_contexto,
            total_filas=total_filas,
            dias_opciones=list(enumerate(DIAS_SEMANA)),
            tipos_actividad=metricas.TIPOS_ACTIVIDAD,
            intensidades=[i for i in lectura.INTENSIDADES if i != "descanso"],
            aviso=aviso,
            mensaje=mensaje,
        )

    async def guardar_semana_tipo_web(request: Request) -> Response:
        datos = await _formulario(request)
        try:
            entradas = entradas_semana(datos)
        except ValueError as exc:
            return _volver(str(exc), "/actividades/semana-tipo")

        def _guardar() -> None:
            con = abrir_db()
            try:
                db.guardar_semana_tipo(con, entradas)
            finally:
                con.close()

        await run_in_threadpool(_guardar)
        return _volver("Semana tipo guardada", "/actividades/semana-tipo")

    def ver_lecturas(_request: Request) -> Response:
        con = abrir_db()
        try:
            return _render("lecturas.html", seccion="hoy", lecturas=db.lecturas(con))
        finally:
            con.close()

    def sincronizar(_request: Request) -> Response:
        try:
            garmin = conectar_garmin()
        except GarminNoConectado as exc:
            return _volver(str(exc))
        fin = hoy()
        con = abrir_db()
        try:
            r = sync.sincronizar(con, garmin, fin - timedelta(days=DIAS_SYNC_WEB - 1), fin)
        finally:
            con.close()
        if r.estado == "ok":
            return _volver(f"Garmin al día: {r.metricas} datos y {r.actividades} actividades")
        return _volver(f"Sincronización {r.estado}: {len(r.errores)} peticiones fallaron")

    def pedir_lectura(_request: Request) -> Response:
        if not generando.acquire(blocking=False):
            return _volver("Ya se está preparando la lectura")
        fecha = hoy()

        def trabajo() -> None:
            con = abrir_db()
            try:
                _leer(con, fecha)
            except lectura.LecturaError:
                pass  # queda guardada con su error
            finally:
                con.close()
                generando.release()

        threading.Thread(target=trabajo, daemon=True).start()
        return _volver("Preparando la lectura: tarda un minuto o dos")

    # --- Conexiones ----------------------------------------------------------

    def _conexiones(
        request: Request,
        *,
        error: str | None = None,
        id_codigo: str | None = None,
    ) -> HTMLResponse:
        fichero = garmin.fichero_tokens(settings.garmin_tokens)
        desde = (
            datetime.fromtimestamp(fichero.stat().st_mtime, UTC).isoformat(timespec="seconds")
            if fichero.is_file()
            else None
        )
        restante = entrada.bloqueado_hasta - reloj()
        hoy_d = hoy()
        inicio_mes = hoy_d.replace(day=1).isoformat()
        fin_mes = hoy_d.isoformat()
        con = abrir_db()
        try:
            sync_garmin = db.ultima_sincronizacion(con, GARMIN)
            filas_mes = db.uso_ia_resumen(con, inicio_mes, fin_mes)
            gasto_ia_mes_usd = sum(float(r["coste_usd"]) for r in filas_mes)
            gasto_ia_mes_eur = gasto_ia_mes_usd * settings.ia_usd_eur
        finally:
            con.close()
        return _render(
            "conexiones.html",
            seccion="conexiones",
            sesion_desde=desde,
            sync=sync_garmin,
            gasto_ia_mes_eur=gasto_ia_mes_eur,
            bloqueado_min=int(restante // 60) + 1 if restante > 0 else 0,
            id_codigo=id_codigo,
            error=error,
            mensaje=request.query_params.get("mensaje"),
        )

    def _bloqueado() -> str | None:
        restante = entrada.bloqueado_hasta - reloj()
        if restante <= 0:
            return None
        return f"Demasiados intentos fallidos: espera {int(restante // 60) + 1} minutos"

    def _fallo(exc: Exception) -> None:
        # Solo el tipo: el mensaje de la librería no hace falta en el log.
        log.warning("Garmin ha rechazado la entrada desde la web (%s)", type(exc).__name__)
        entrada.fallos += 1
        if entrada.fallos >= MAX_FALLOS:
            entrada.bloqueado_hasta = reloj() + BLOQUEO_S
            entrada.fallos = 0
            entrada.pendiente = None

    def _hecho() -> Response:
        entrada.pendiente = None
        entrada.fallos = 0
        return _volver("Sesión de Garmin guardada", "/conexiones")

    def ver_conexiones(request: Request) -> Response:
        return _conexiones(request)

    def ver_ia_uso(_request: Request) -> Response:
        hoy_d = hoy()
        dias_en_mes = calendar.monthrange(hoy_d.year, hoy_d.month)[1]
        inicio_mes = hoy_d.replace(day=1).isoformat()
        fin_mes = hoy_d.replace(day=dias_en_mes).isoformat()

        dias_30 = [hoy_d - timedelta(days=i) for i in reversed(range(30))]
        desde_30d = dias_30[0].isoformat()
        hasta_30d = dias_30[-1].isoformat()

        desde_prueba = settings.ia_prueba_desde.isoformat()
        hasta_prueba = settings.ia_prueba_hasta.isoformat()

        con = abrir_db()
        try:
            filas_mes = db.uso_ia_resumen(con, inicio_mes, fin_mes)
            filas_30d = db.uso_ia_resumen(con, desde_30d, hasta_30d)
            filas_prueba = db.uso_ia_resumen(con, desde_prueba, hasta_prueba)
        finally:
            con.close()

        # 1. Gasto mes en EUR
        gasto_mes_usd = sum(float(r["coste_usd"]) for r in filas_mes)
        gasto_mes_eur = gasto_mes_usd * settings.ia_usd_eur
        gasto_hoy_eur = settings.ia_usd_eur * sum(
            float(r["coste_usd"]) for r in filas_mes if r["fecha"] == hoy_d.isoformat()
        )
        credito_mes_eur = settings.ia_credito_mes_eur
        pct_mes = (
            min(100.0, round((gasto_mes_eur / credito_mes_eur) * 100, 1))
            if credito_mes_eur > 0
            else 0.0
        )
        pasado_mes = gasto_mes_eur > credito_mes_eur

        # 2. Prueba gratuita
        gasto_prueba_usd = sum(float(r["coste_usd"]) for r in filas_prueba)
        gasto_prueba_eur = gasto_prueba_usd * settings.ia_usd_eur
        prueba_restante_eur = max(0.0, settings.ia_credito_prueba_eur - gasto_prueba_eur)
        idx_mes_prueba = settings.ia_prueba_hasta.month - 1
        fecha_hasta_prueba_str = (
            f"{settings.ia_prueba_hasta.day} {graficas.MESES_CORTOS[idx_mes_prueba]}"
        )

        # 3. Proyección fin de mes
        dias_transcurridos = hoy_d.day
        gasto_medio_diario = gasto_mes_eur / dias_transcurridos if dias_transcurridos > 0 else 0.0
        proyeccion_mes_eur = gasto_medio_diario * dias_en_mes

        # 4. Aviso si supera el 80% del crédito mensual y la prueba ya caducó
        prueba_caducada = hoy_d > settings.ia_prueba_hasta
        mostrar_aviso = (proyeccion_mes_eur > 0.8 * credito_mes_eur) and prueba_caducada

        # 5. Gráfica columnas 30 días
        gasto_por_dia: dict[str, float] = defaultdict(float)
        for r in filas_30d:
            gasto_por_dia[r["fecha"]] += float(r["coste_usd"]) * settings.ia_usd_eur
        # En céntimos: en euros, el eje y las etiquetas salían todos a 0.
        barras = [
            (graficas.fecha_corta(d.isoformat()), round(gasto_por_dia[d.isoformat()] * 100, 1))
            for d in dias_30
        ]
        grafica_30d = graficas.columnas(
            barras,
            unidad="cént.",
            titulo="Gasto diario últimos 30 días, en céntimos",
            etiquetas_cada=7,
        )

        # 6. Tabla por modelo este mes
        modelos_mes: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "motor": "",
                "modelo": "",
                "llamadas": 0,
                "entrada": 0,
                "salida": 0,
                "razonamiento": 0,
                "coste_usd": 0.0,
                "fallos": 0,
            }
        )
        for r in filas_mes:
            m = r["modelo"]
            entry = modelos_mes[m]
            entry["motor"] = r["motor"]
            entry["modelo"] = m
            entry["llamadas"] += int(r["llamadas"])
            entry["entrada"] += int(r["entrada"])
            entry["salida"] += int(r["salida"])
            entry["razonamiento"] += int(r["razonamiento"])
            entry["coste_usd"] += float(r["coste_usd"])
            entry["fallos"] += int(r["fallos"])

        tabla_modelos = []
        for entry in sorted(modelos_mes.values(), key=lambda x: str(x["modelo"])):
            entry["coste_eur"] = entry["coste_usd"] * settings.ia_usd_eur
            tabla_modelos.append(entry)

        return _render(
            "ia_uso.html",
            seccion="conexiones",
            gasto_mes_eur=gasto_mes_eur,
            credito_mes_eur=credito_mes_eur,
            pct_mes=pct_mes,
            pasado_mes=pasado_mes,
            prueba_restante_eur=prueba_restante_eur,
            fecha_hasta_prueba_str=fecha_hasta_prueba_str,
            proyeccion_mes_eur=proyeccion_mes_eur,
            mostrar_aviso=mostrar_aviso,
            prueba_caducada=prueba_caducada,
            grafica_30d=grafica_30d,
            tabla_modelos=tabla_modelos,
            gasto_hoy_eur=gasto_hoy_eur,
            tope_dia_eur=settings.ia_tope_dia_eur,
            tope_mes_eur=settings.ia_tope_mes_eur,
            tope_alcanzado=(
                gasto_hoy_eur >= settings.ia_tope_dia_eur
                or gasto_mes_eur >= settings.ia_tope_mes_eur
            ),
        )

    async def garmin_entrar(request: Request) -> Response:
        datos = await _formulario(request)
        correo = datos.get("correo", "").strip()
        contrasena = datos.get("contrasena", "")
        if not correo or not contrasena:
            return _conexiones(request, error="Escribe el correo y la contraseña de Garmin")
        if bloqueo := _bloqueado():
            return _conexiones(request, error=bloqueo)
        if not entrando.acquire(blocking=False):
            return _conexiones(request, error="Ya hay una entrada en Garmin en curso")
        try:
            pendiente = await run_in_threadpool(
                garmin.empezar_entrada,
                settings.garmin_tokens,
                correo,
                contrasena,
                crear_cliente_garmin,
            )
        except Exception as exc:  # la librería lanza varios tipos según el fallo
            _fallo(exc)
            return _conexiones(
                request,
                error=_bloqueado()
                or "Garmin no ha aceptado el correo o la contraseña (o no responde ahora)",
            )
        finally:
            entrando.release()
        if pendiente is None:
            return _hecho()
        entrada.pendiente = pendiente
        entrada.id_pendiente = secrets.token_urlsafe(16)
        entrada.caduca = reloj() + CADUCIDAD_CODIGO_S
        return _conexiones(request, id_codigo=entrada.id_pendiente)

    async def garmin_codigo(request: Request) -> Response:
        datos = await _formulario(request)
        id_ = datos.get("id", "")
        codigo = datos.get("codigo", "").strip()
        if bloqueo := _bloqueado():
            return _conexiones(request, error=bloqueo)
        pendiente = entrada.pendiente
        if (
            pendiente is None
            or not secrets.compare_digest(id_, entrada.id_pendiente)
            or reloj() > entrada.caduca
        ):
            entrada.pendiente = None
            return _conexiones(request, error="El código ha caducado: vuelve a empezar")
        if not codigo:
            return _conexiones(request, error="Escribe el código", id_codigo=id_)
        if not entrando.acquire(blocking=False):
            return _conexiones(request, error="Ya hay una entrada en Garmin en curso")
        try:
            await run_in_threadpool(
                garmin.terminar_entrada, settings.garmin_tokens, pendiente, codigo
            )
        except Exception as exc:  # la librería lanza varios tipos según el fallo
            _fallo(exc)
            if bloqueo := _bloqueado():
                return _conexiones(request, error=bloqueo)
            return _conexiones(request, error="Garmin no ha aceptado el código", id_codigo=id_)
        finally:
            entrando.release()
        return _hecho()

    async def garmin_probar(_request: Request) -> Response:
        try:
            await run_in_threadpool(conectar_garmin)
        except GarminNoConectado as exc:
            return _volver(str(exc), "/conexiones")
        return _volver("La sesión de Garmin funciona", "/conexiones")

    def manifiesto(_request: Request) -> Response:
        return FileResponse(
            ESTATICOS / "manifest.webmanifest", media_type="application/manifest+json"
        )

    def service_worker(_request: Request) -> Response:
        # Desde la raíz para que controle toda la web; sin caché HTTP para
        # que el móvil note enseguida una versión nueva.
        return FileResponse(
            ESTATICOS / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"}
        )

    # --- Comida --------------------------------------------------------------

    def _pagina_comida(
        request: Request,
        fecha: date,
        *,
        error: str | None = None,
        codigo_buscado: str = "",
        momento_elegido: str | None = None,
    ) -> HTMLResponse:
        con = abrir_db()
        try:
            comidas_dia = db.comidas(con, fecha.isoformat())
            totales, objetivo = _resumen_comida_dia(con, fecha, hoy())
        finally:
            con.close()
        comidas_por_momento: dict[str, list[db.Comida]] = {m: [] for m in db.MOMENTOS}
        for c in comidas_dia:
            comidas_por_momento.setdefault(c.momento, []).append(c)
        if momento_elegido is None:
            momento_elegido = momento_valido(request.query_params.get("momento"))
        momento_defecto = momento_elegido or momento_por_hora(datetime.now(ZONA).hour)

        def _progreso(llevas: float, meta: int) -> dict[str, Any]:
            pct = min(100, round(100 * llevas / meta)) if meta > 0 else 0
            return {"pct": pct, "pasado": llevas > meta}

        prog_dia: dict[str, dict[str, Any]] | None = None
        prog_momento: dict[str, dict[str, Any]] | None = None
        if objetivo is not None:
            prog_dia = {
                "kcal": _progreso(totales.total.kcal, objetivo.total.kcal),
                "proteinas": _progreso(totales.total.proteinas, objetivo.total.proteinas),
                "hidratos": _progreso(totales.total.hidratos, objetivo.total.hidratos),
                "grasas": _progreso(totales.total.grasas, objetivo.total.grasas),
            }
            prog_momento = {}
            for m, obj_m in objetivo.por_momento.items():
                tot_m = totales.por_momento.get(m)
                llevas_m = tot_m.kcal if tot_m else 0.0
                prog_momento[m] = _progreso(llevas_m, obj_m.kcal)

        minimo = objetivo.kcal_minimo if objetivo is not None else None
        pct_minimo = 0
        bajo_minimo = False
        if objetivo is not None and minimo is not None:
            if objetivo.total.kcal > 0:
                pct_minimo = min(100, round(100 * minimo / objetivo.total.kcal))
            es_tarde = fecha == hoy() and datetime.now(ZONA).hour >= 21
            bajo_minimo = (
                totales.total.entradas > 0
                and (fecha < hoy() or es_tarde)
                and totales.total.kcal < minimo
            )

        f_prot = (
            totales.total.proteinas / objetivo.total.proteinas
            if objetivo and objetivo.total.proteinas > 0
            else None
        )
        f_kcal = (
            totales.total.kcal / objetivo.total.kcal
            if objetivo and objetivo.total.kcal > 0
            else None
        )
        if objetivo:
            etiqueta = (
                f"Proteína {gramos(totales.total.proteinas)} de"
                f" {gramos(objetivo.total.proteinas)} gramos;"
                f" calorías {cifra(totales.total.kcal)} de"
                f" {cifra(objetivo.total.kcal)}"
            )
            texto_anillo = f"{gramos(totales.total.proteinas)} g"
            subtexto_anillo = f"de {gramos(objetivo.total.proteinas)} g"
        else:
            etiqueta = (
                f"Proteína {gramos(totales.total.proteinas)} gramos;"
                f" calorías {cifra(totales.total.kcal)}"
            )
            texto_anillo = f"{gramos(totales.total.proteinas)} g"
            subtexto_anillo = ""

        anillos_resumen = graficas.anillos(
            f_prot,
            f_kcal,
            clase_ext="proteina",
            clase_int="kcal",
            tamano=132,
            texto=texto_anillo,
            subtexto=subtexto_anillo,
            etiqueta=etiqueta,
        )
        te_faltan_prot = (
            max(0.0, objetivo.total.proteinas - totales.total.proteinas) if objetivo else 0.0
        )
        prog_hidratos = (
            min(100, round(100 * totales.total.hidratos / objetivo.total.hidratos))
            if objetivo and objetivo.total.hidratos > 0
            else 0
        )
        prog_grasas = (
            min(100, round(100 * totales.total.grasas / objetivo.total.grasas))
            if objetivo and objetivo.total.grasas > 0
            else 0
        )

        return _render(
            "comida.html",
            pestana="diario",
            seccion="comida",
            fecha=fecha.isoformat(),
            es_hoy=fecha == hoy(),
            anterior=(fecha - timedelta(days=1)).isoformat(),
            siguiente=(fecha + timedelta(days=1)).isoformat() if fecha < hoy() else None,
            comidas_por_momento=comidas_por_momento,
            totales=totales,
            objetivo=objetivo,
            prog_dia=prog_dia,
            prog_momento=prog_momento,
            pct_minimo=pct_minimo,
            bajo_minimo=bajo_minimo,
            anillos_resumen=anillos_resumen,
            te_faltan_prot=te_faltan_prot,
            prog_hidratos=prog_hidratos,
            prog_grasas=prog_grasas,
            momentos=db.MOMENTOS,
            momento_defecto=momento_defecto,
            momento_elegido=momento_elegido,
            mensaje=request.query_params.get("mensaje"),
            error=error or request.query_params.get("error"),
            codigo_buscado=codigo_buscado,
        )

    def _pagina_anadir(
        fecha: str,
        momento: str,
        q: str = "",
        *,
        resultados: dict[str, Any] | None = None,
        mensaje: str | None = None,
        error: str | None = None,
    ) -> Response:
        """Pestaña Añadir: con `resultados` de una búsqueda, o lo reciente y lo tuyo."""
        recientes: list[sqlite3.Row] = []
        propios: list[sqlite3.Row] = []
        if resultados is None:
            con = abrir_db()
            try:
                recientes = db.productos_recientes(con)
                propios = db.productos_propios(con)
            finally:
                con.close()
        vacios: dict[str, Any] = {
            "productos_locales": [],
            "comidas_anteriores": [],
            "genericos": [],
            "off_productos": [],
            "off_error": None,
        }
        return _render(
            "comida_buscar.html",
            pestana="anadir",
            seccion="comida",
            fecha=fecha,
            momento_elegido=momento,
            momentos_chips=[m for m in db.MOMENTOS if m != "otro"],
            q=q,
            recientes=recientes,
            productos_propios=propios,
            ciqual_fuente=ciqual.FUENTE,
            mensaje=mensaje,
            error=error,
            **(vacios | (resultados or {})),
        )

    def ver_comida(request: Request) -> Response:
        momento_elegido = momento_valido(request.query_params.get("momento"))
        return _pagina_comida(request, _fecha(request), momento_elegido=momento_elegido)

    async def buscar_comida(request: Request) -> Response:
        fecha = _fecha(request)
        fecha_str = fecha.isoformat()
        q = request.query_params.get("q", "").strip()
        momento_elegido = momento_valido(request.query_params.get("momento"))
        momento_elegido = momento_elegido or momento_por_hora(datetime.now(ZONA).hour)
        mensaje = request.query_params.get("mensaje")
        error = request.query_params.get("error")

        if not q:
            return await run_in_threadpool(
                _pagina_anadir, fecha_str, momento_elegido, mensaje=mensaje, error=error
            )

        cod_norm = alimentos.normalizar_codigo(q)
        if cod_norm.isdigit() and len(cod_norm) in alimentos.LONGITUDES_VALIDAS:
            params = {"codigo": cod_norm, "fecha": fecha_str}
            if momento_elegido:
                params["momento"] = momento_elegido
            return RedirectResponse(f"/comida/producto?{urlencode(params)}", status_code=303)

        def _buscar_local() -> tuple[list[dict[str, Any]], list[db.Comida]]:
            con = abrir_db()
            try:
                propios = db.buscar_productos(con, q)
                anteriores = db.comidas_anteriores(con, q, hasta=fecha_str)
                return propios, anteriores
            finally:
                con.close()

        productos_locales, anteriores = await run_in_threadpool(_buscar_local)
        genericos = await run_in_threadpool(ciqual.buscar, q)

        off_error: str | None = None
        off_productos: list[alimentos.ResultadoBusqueda] = []
        try:
            off_productos = await run_in_threadpool(alimentos.buscar_off, q, cliente_off)
        except (alimentos.OpenFoodFactsNoDisponible, httpx.HTTPError):
            off_error = "Open Food Facts no responde ahora; prueba en un minuto"

        return _pagina_anadir(
            fecha_str,
            momento_elegido,
            q,
            resultados={
                "productos_locales": productos_locales,
                "comidas_anteriores": anteriores,
                "genericos": genericos,
                "off_productos": off_productos,
                "off_error": off_error,
            },
            mensaje=mensaje,
            error=error,
        )

    async def repetir_comida(request: Request) -> Response:
        datos = await _formulario(request)
        id_raw = datos.get("id", "").strip()
        fecha_raw = datos.get("fecha", "").strip()
        momento_raw = datos.get("momento", "").strip()

        try:
            id_ = int(id_raw)
        except ValueError:
            return _volver("Comida no encontrada", "/comida")

        try:
            fecha_dt = date.fromisoformat(fecha_raw) if fecha_raw else hoy()
        except ValueError:
            return _volver("Fecha no válida", "/comida")

        if fecha_dt > hoy():
            return _volver("La fecha no puede ser posterior a hoy", "/comida")
        fecha_str = fecha_dt.isoformat()

        momento = momento_valido(momento_raw)
        if momento is None:
            return _volver("Momento no válido", f"/comida?fecha={fecha_str}")

        def _repetir() -> str:
            con = abrir_db()
            try:
                c = db.leer_comida(con, id_)
                if c is None:
                    raise ValueError("Comida no encontrada")
                db.anadir_comida(
                    con,
                    fecha=fecha_str,
                    momento=momento,
                    nombre=c.nombre,
                    cantidad=c.cantidad,
                    unidad=c.unidad,
                    kcal=c.kcal,
                    proteinas=c.proteinas,
                    hidratos=c.hidratos,
                    grasas=c.grasas,
                    codigo=c.codigo,
                )
                return c.nombre
            finally:
                con.close()

        try:
            nombre = await run_in_threadpool(_repetir)
            return _volver(f"Añadido {nombre}", f"/comida?fecha={fecha_str}")
        except ValueError as exc:
            return _volver(str(exc), f"/comida?fecha={fecha_str}")

    def ver_comida_ia(request: Request) -> Response:
        momento = momento_valido(request.query_params.get("momento"))
        return _render(
            "comida_ia.html",
            fecha=_fecha(request).isoformat(),
            momento_elegido=momento or momento_por_hora(datetime.now(ZONA).hour),
            momentos_chips=db.MOMENTOS,
            texto=request.query_params.get("texto", "").strip(),
            pestana="anadir",
            seccion="comida",
            mensaje=request.query_params.get("mensaje"),
            error=request.query_params.get("error"),
        )

    def _analizar_en_segundo_plano(peticion: PeticionIA, foto: bytes | None) -> int:
        """Crea el borrador y lanza el análisis en un hilo; devuelve el id del borrador."""
        con = abrir_db()
        try:
            id_ = db.crear_borrador_comida(
                con,
                fecha=peticion.fecha,
                momento=peticion.momento,
                texto=peticion.texto,
                con_foto=foto is not None,
            )
        finally:
            con.close()

        def _trabajo() -> None:
            con_hilo = abrir_db()
            try:
                alimentos = analizar_fn(peticion.texto, foto)
                db.completar_borrador_comida(con_hilo, id_, [asdict(a) for a in alimentos])
            except (ia.ErrorIA, ia.IAOcupada) as exc:
                db.fallar_borrador_comida(con_hilo, id_, str(exc))
            except Exception:
                log.exception("Error al analizar la comida con IA")
                db.fallar_borrador_comida(con_hilo, id_, "No se pudo analizar la comida")
            finally:
                con_hilo.close()

        threading.Thread(target=_trabajo, daemon=True).start()
        return id_

    async def crear_comida_ia_web(request: Request) -> Response:
        # Con foto, app.js manda la imagen como cuerpo y el resto en la query string
        # (sin python-multipart) y espera JSON; sin foto, es un formulario normal.
        if request.headers.get("content-type", "").lower().startswith("image/"):
            foto = await _leer_foto(request)
            if isinstance(foto, Response):
                return foto
            try:
                peticion = peticion_ia(dict(request.query_params), hay_foto=True, hoy_=hoy())
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, 400)
            id_ = await run_in_threadpool(_analizar_en_segundo_plano, peticion, foto)
            return JSONResponse({"url": f"/comida/ia/{id_}"})

        datos = await _formulario(request)
        try:
            peticion = peticion_ia(datos, hay_foto=False, hoy_=hoy())
        except ValueError as exc:
            volver = {k: datos.get(k, "") for k in ("fecha", "momento", "texto")}
            return _volver(str(exc), f"/comida/ia?{urlencode(volver)}")
        id_ = await run_in_threadpool(_analizar_en_segundo_plano, peticion, None)
        return RedirectResponse(f"/comida/ia/{id_}", 303)

    def _borrador(request: Request) -> db.BorradorComida | None:
        con = abrir_db()
        try:
            return db.leer_borrador_comida(con, int(request.path_params["id"]))
        finally:
            con.close()

    def estado_comida_ia(request: Request) -> Response:
        borrador = _borrador(request)
        if borrador is None:
            return JSONResponse({"error": "Borrador no encontrado"}, 404)
        return JSONResponse({"estado": borrador.estado})

    def ver_comida_ia_borrador(request: Request) -> Response:
        borrador = _borrador(request)
        if borrador is None:
            return HTMLResponse("Borrador no encontrado", 404)

        def total(clave: str) -> float:
            return sum(float(a[clave]) for a in borrador.alimentos if a.get(clave) is not None)

        return _render(
            "comida_ia_revisar.html",
            borrador=borrador,
            fecha=borrador.fecha,
            momento_elegido=borrador.momento,
            momentos=db.MOMENTOS,
            momentos_chips=db.MOMENTOS,
            texto=borrador.texto,
            total_kcal=total("kcal"),
            total_proteinas=total("proteinas"),
            total_hidratos=total("hidratos"),
            total_grasas=total("grasas"),
            pestana="anadir",
            seccion="comida",
            mensaje=request.query_params.get("mensaje"),
            error=request.query_params.get("error"),
        )

    def descartar_comida_ia_web(request: Request) -> Response:
        borrador = _borrador(request)
        fecha = borrador.fecha if borrador else hoy().isoformat()
        if borrador is not None:
            con = abrir_db()
            try:
                db.borrar_borrador_comida(con, borrador.id)
            finally:
                con.close()
        return RedirectResponse(f"/comida/ia?fecha={fecha}", 303)

    async def anadir_comida_ia_web(request: Request) -> Response:
        datos = await _formulario(request)
        borrador = await run_in_threadpool(_borrador, request)
        if borrador is None:
            return _volver("Borrador no encontrado", "/comida/ia")
        try:
            elegidos = alimentos_desde_formulario(datos, len(borrador.alimentos))
        except ValueError as exc:
            return _volver(str(exc), f"/comida/ia/{borrador.id}")

        def _guardar() -> None:
            con = abrir_db()
            try:
                for a in elegidos:
                    db.anadir_comida(
                        con,
                        fecha=borrador.fecha,
                        momento=a.momento,
                        nombre=a.nombre,
                        cantidad=a.cantidad,
                        unidad=a.unidad,
                        kcal=a.kcal,
                        proteinas=a.proteinas,
                        hidratos=a.hidratos,
                        grasas=a.grasas,
                    )
                    if a.producto:
                        db.guardar_producto_propio(
                            con,
                            id_=None,
                            codigo=None,
                            nombre=a.nombre,
                            marca=None,
                            unidad=a.unidad,
                            kcal=por_100(a.kcal, a.cantidad),
                            proteinas=por_100(a.proteinas, a.cantidad),
                            hidratos=por_100(a.hidratos, a.cantidad),
                            grasas=por_100(a.grasas, a.cantidad),
                            origen="comida-ia",
                            racion_g=a.cantidad,
                            racion_nombre="ración",
                        )
                db.borrar_borrador_comida(con, borrador.id)
            finally:
                con.close()

        await run_in_threadpool(_guardar)
        mensaje = (
            f"Añadido {elegidos[0].nombre}"
            if len(elegidos) == 1
            else f"Añadidos {len(elegidos)} alimentos"
        )
        return _volver(mensaje, f"/comida?fecha={borrador.fecha}")

    def ver_comida_objetivos(request: Request) -> Response:
        con = abrir_db()
        try:
            ajustes = db.ajustes_comida(con)
            objetivo_hoy = nutricion.objetivo_dia(con, hoy(), hoy(), ajustes=ajustes)
        finally:
            con.close()
        momentos_reparto = [
            (i, m, ajustes.reparto.get(m, db.REPARTO_DEFECTO.get(m, 0)))
            for i, m in enumerate(db.REPARTO_DEFECTO)
        ]
        return _render(
            "comida_objetivos.html",
            seccion="comida",
            ajustes=ajustes,
            objetivo=objetivo_hoy,
            momentos_reparto=momentos_reparto,
            mensaje=request.query_params.get("mensaje"),
            error=request.query_params.get("error"),
        )

    async def guardar_comida_objetivos(request: Request) -> Response:
        datos = await _formulario(request)
        try:
            ajustes = nutricion.ajustes_desde_formulario(datos)
        except ValueError as exc:
            return _volver(str(exc), "/comida/objetivos")

        def _guardar() -> None:
            con = abrir_db()
            try:
                db.guardar_ajustes_comida(con, ajustes)
            finally:
                con.close()

        await run_in_threadpool(_guardar)
        return _volver("Objetivos guardados", "/comida/objetivos")

    def ver_comida_nuevo(request: Request) -> Response:
        fecha = _fecha(request)
        codigo = request.query_params.get("codigo", "").strip()
        generico_codigo = request.query_params.get("generico", "").strip()
        momento_defecto = momento_valido(request.query_params.get("momento")) or momento_por_hora(
            datetime.now(ZONA).hour
        )
        aviso = request.query_params.get("aviso")
        mensaje = request.query_params.get("mensaje")
        error = request.query_params.get("error")

        p = None
        origen = None
        if generico_codigo:
            g = ciqual.por_codigo(generico_codigo)
            if g is not None:
                p = alimentos.producto_desde_generico(g)
                origen = "ciqual"

        return _render(
            "comida_nuevo.html",
            seccion="comida",
            fecha=fecha.isoformat(),
            codigo=codigo,
            p=p,
            origen=origen,
            aviso=aviso,
            mensaje=mensaje,
            error=error,
            momentos=db.MOMENTOS,
            momento_defecto=momento_defecto,
        )

    def ver_producto(request: Request) -> Response:
        fecha = _fecha(request)
        codigo = request.query_params.get("codigo", "").strip()
        momento_elegido = momento_valido(request.query_params.get("momento"))
        momento_defecto = momento_elegido or momento_por_hora(datetime.now(ZONA).hour)
        if not codigo:
            return _pagina_anadir(
                fecha.isoformat(), momento_defecto, "", error="Escribe un código de barras"
            )
        con = abrir_db()
        try:
            try:
                res = alimentos.obtener_producto(con, codigo, cliente_off)
            except alimentos.ErrorAlimentos as exc:
                if isinstance(exc, alimentos.CodigoInvalido):
                    return _pagina_anadir(
                        fecha.isoformat(),
                        momento_defecto,
                        codigo,
                        error=f"Código no válido: {codigo}",
                    )
                elif isinstance(exc, alimentos.ProductoNoEncontrado):
                    return _render(
                        "comida_nuevo.html",
                        seccion="comida",
                        fecha=fecha.isoformat(),
                        codigo=codigo,
                        aviso=(
                            "Open Food Facts no tiene este producto:"
                            " créalo con los datos de la etiqueta"
                        ),
                        mensaje=None,
                        error=None,
                        momentos=db.MOMENTOS,
                        momento_defecto=momento_defecto,
                    )
                else:
                    error_msg = "Open Food Facts no responde en este momento"
                return _pagina_anadir(fecha.isoformat(), momento_defecto, codigo, error=error_msg)
        finally:
            con.close()
        return _render(
            "comida_producto.html",
            seccion="comida",
            producto=res.producto,
            desde_cache=res.desde_cache,
            fecha=fecha.isoformat(),
            momentos=db.MOMENTOS,
            momento_defecto=momento_defecto,
            mensaje=request.query_params.get("mensaje"),
        )

    async def guardar_productos(request: Request) -> Response:
        datos = await _formulario(request)
        fecha_raw = datos.get("fecha", "").strip()
        try:
            fecha_dt = date.fromisoformat(fecha_raw) if fecha_raw else hoy()
        except ValueError:
            fecha_dt = hoy()
        fecha_str = fecha_dt.isoformat()

        id_raw = datos.get("id", "").strip()
        id_ = int(id_raw) if id_raw.isdigit() else None
        codigo_form = datos.get("codigo", "").strip()

        momento_form = momento_valido(datos.get("momento"))
        momento_qs = f"&{urlencode({'momento': momento_form})}" if momento_form else ""

        qs_prod = urlencode({"codigo": codigo_form, "fecha": fecha_str})
        destino_error = (
            f"/comida/propio/{id_}?fecha={fecha_str}{momento_qs}"
            if id_ is not None
            else (
                f"/comida/producto?{qs_prod}{momento_qs}"
                if codigo_form
                else f"/comida/nuevo?fecha={fecha_str}{momento_qs}"
            )
        )

        try:
            dp = datos_producto(datos)
        except ValueError as exc:
            return _volver(str(exc), destino_error)

        def _guardar() -> int:
            con = abrir_db()
            try:
                return db.guardar_producto_propio(
                    con,
                    id_=id_,
                    codigo=dp.codigo,
                    nombre=dp.nombre,
                    marca=dp.marca,
                    unidad=dp.unidad,
                    kcal=dp.kcal,
                    proteinas=dp.proteinas,
                    hidratos=dp.hidratos,
                    grasas=dp.grasas,
                    origen=dp.origen,
                    racion_g=dp.racion_g,
                    racion_nombre=dp.racion_nombre,
                )
            finally:
                con.close()

        try:
            guardado_id = await run_in_threadpool(_guardar)
        except ValueError as exc:
            return _volver(str(exc), destino_error)

        if dp.codigo:
            dest_prod = f"/comida/producto?{urlencode({'codigo': dp.codigo, 'fecha': fecha_str})}"
            return _volver("Producto guardado", f"{dest_prod}{momento_qs}")
        return _volver(
            "Producto guardado",
            f"/comida/propio/{guardado_id}?fecha={fecha_str}{momento_qs}",
        )

    def ver_comida_propio(request: Request) -> Response:
        id_ = int(request.path_params["id"])
        fecha = _fecha(request)
        momento_defecto = momento_valido(request.query_params.get("momento")) or momento_por_hora(
            datetime.now(ZONA).hour
        )
        con = abrir_db()
        try:
            fila = db.leer_producto_propio(con, id_)
        finally:
            con.close()

        if fila is None:
            return Response("No encontrado", 404)

        prod = alimentos.producto_desde_propio(fila)
        return _render(
            "comida_producto.html",
            seccion="comida",
            producto=prod,
            desde_cache=False,
            fecha=fecha.isoformat(),
            momentos=db.MOMENTOS,
            momento_defecto=momento_defecto,
            mensaje=request.query_params.get("mensaje"),
        )

    def ver_generico(request: Request) -> Response:
        codigo = request.path_params["codigo"]
        g = ciqual.por_codigo(codigo)
        if g is None:
            return Response("No encontrado", 404)
        fecha = _fecha(request)
        momento_defecto = momento_valido(request.query_params.get("momento")) or momento_por_hora(
            datetime.now(ZONA).hour
        )
        prod = alimentos.producto_desde_generico(g)
        return _render(
            "comida_producto.html",
            seccion="comida",
            producto=prod,
            generico=codigo,
            ciqual=ciqual,
            desde_cache=False,
            fecha=fecha.isoformat(),
            momentos=db.MOMENTOS,
            momento_defecto=momento_defecto,
            mensaje=request.query_params.get("mensaje"),
        )

    async def leer_etiqueta_foto(request: Request) -> Response:
        cuerpo = await _leer_foto(request)
        if isinstance(cuerpo, Response):
            return cuerpo

        try:
            etiqueta = await run_in_threadpool(leer, cuerpo)
        except ia.IAOcupada as exc:
            return JSONResponse({"error": str(exc)}, 429)
        except ia.ErrorIA as exc:
            return JSONResponse({"error": str(exc)}, 502)

        datos_resp = asdict(etiqueta)
        datos_resp["avisos"] = list(etiqueta.avisos)
        return JSONResponse(datos_resp, 200)

    def _guardar_comida(datos: dict[str, str], fecha: str, cantidad: float) -> str:
        propio_raw = datos.get("propio", "").strip()
        codigo = datos.get("codigo", "").strip()
        generico_raw = datos.get("generico", "").strip()
        momento = momento_valido(datos.get("momento"))
        if momento is None:
            raise ValueError("Momento no válido")
        con = abrir_db()
        try:
            if propio_raw:
                try:
                    id_propio = int(propio_raw)
                except ValueError:
                    raise ValueError("Producto no encontrado") from None
                fila = db.leer_producto_propio(con, id_propio)
                if fila is None:
                    raise ValueError("Producto no encontrado")
                prod = alimentos.producto_desde_propio(fila)
                nutrientes = alimentos.calcular_nutrientes(prod, cantidad)
                nombre_base = prod.nombre or f"Producto {prod.codigo or id_propio}"
                nombre = alimentos.nombre_con_raciones(
                    nombre_base, cantidad, prod.racion_g, prod.racion_nombre
                )
                db.anadir_comida(
                    con,
                    fecha=fecha,
                    momento=momento,
                    nombre=nombre,
                    cantidad=cantidad,
                    unidad=prod.unidad,
                    kcal=nutrientes.kcal,
                    proteinas=nutrientes.proteinas,
                    hidratos=nutrientes.hidratos,
                    grasas=nutrientes.grasas,
                    codigo=prod.codigo or None,
                )
                return nombre
            elif codigo:
                res = alimentos.obtener_producto(con, codigo, cliente_off)
                prod = res.producto
                nutrientes = alimentos.calcular_nutrientes(prod, cantidad)
                nombre_base = prod.nombre or f"Producto {codigo}"
                nombre = alimentos.nombre_con_raciones(
                    nombre_base, cantidad, prod.racion_g, prod.racion_nombre
                )
                db.anadir_comida(
                    con,
                    fecha=fecha,
                    momento=momento,
                    nombre=nombre,
                    cantidad=cantidad,
                    unidad=prod.unidad,
                    kcal=nutrientes.kcal,
                    proteinas=nutrientes.proteinas,
                    hidratos=nutrientes.hidratos,
                    grasas=nutrientes.grasas,
                    codigo=codigo,
                )
                return nombre
            elif generico_raw:
                g = ciqual.por_codigo(generico_raw)
                if g is None:
                    raise ValueError("Alimento no encontrado")
                prod = alimentos.producto_desde_generico(g)
                nutrientes = alimentos.calcular_nutrientes(prod, cantidad)
                nombre_base = prod.nombre or f"Alimento {generico_raw}"
                nombre = alimentos.nombre_con_raciones(
                    nombre_base, cantidad, prod.racion_g, prod.racion_nombre
                )
                db.anadir_comida(
                    con,
                    fecha=fecha,
                    momento=momento,
                    nombre=nombre,
                    cantidad=cantidad,
                    unidad=prod.unidad,
                    kcal=nutrientes.kcal,
                    proteinas=nutrientes.proteinas,
                    hidratos=nutrientes.hidratos,
                    grasas=nutrientes.grasas,
                    codigo=None,
                )
                return nombre
            else:
                nombre = datos.get("nombre", "").strip()
                unidad = datos.get("unidad", "").strip()

                def _parse_nutriente(campo: str) -> float | None:
                    v = datos.get(campo, "").strip().replace(",", ".")
                    if not v:
                        return None
                    val = float(v)
                    if not math.isfinite(val):
                        raise ValueError(f"El valor de {campo} debe ser un número finito")
                    return val

                kcal = _parse_nutriente("kcal")
                proteinas = _parse_nutriente("proteinas")
                hidratos = _parse_nutriente("hidratos")
                grasas = _parse_nutriente("grasas")

                db.anadir_comida(
                    con,
                    fecha=fecha,
                    momento=momento,
                    nombre=nombre,
                    cantidad=cantidad,
                    unidad=unidad,
                    kcal=kcal,
                    proteinas=proteinas,
                    hidratos=hidratos,
                    grasas=grasas,
                )
                return nombre
        finally:
            con.close()

    async def anadir(request: Request) -> Response:
        datos = await _formulario(request)
        fecha_raw = datos.get("fecha", "").strip()
        try:
            fecha_dt = date.fromisoformat(fecha_raw) if fecha_raw else hoy()
        except ValueError:
            return _volver("Fecha no válida", "/comida")
        if fecha_dt > hoy():
            return _volver("La fecha no puede ser posterior a hoy", "/comida")
        fecha_str = fecha_dt.isoformat()

        if momento_valido(datos.get("momento")) is None:
            return _volver("Momento no válido", f"/comida?fecha={fecha_str}")

        propio_raw = datos.get("propio", "").strip()
        codigo = datos.get("codigo", "").strip()
        generico_raw = datos.get("generico", "").strip()
        try:
            cantidad_str = (
                datos.get("cantidad", "100" if (codigo or propio_raw or generico_raw) else "")
                .strip()
                .replace(",", ".")
            )
            if not cantidad_str:
                return _volver("La cantidad debe ser un número", f"/comida?fecha={fecha_str}")
            cantidad = float(cantidad_str)
            if not math.isfinite(cantidad):
                return _volver(
                    "La cantidad debe ser un número finito", f"/comida?fecha={fecha_str}"
                )
        except ValueError:
            return _volver("La cantidad debe ser un número", f"/comida?fecha={fecha_str}")

        try:
            nombre = await run_in_threadpool(_guardar_comida, datos, fecha_str, cantidad)
            return _volver(f"Añadido {nombre}", f"/comida?fecha={fecha_str}")
        except (ValueError, alimentos.ErrorAlimentos) as exc:
            return _volver(str(exc), f"/comida?fecha={fecha_str}")

    async def borrar(request: Request) -> Response:
        id_ = int(request.path_params["id"])
        fecha = request.query_params.get("fecha", "").strip()
        if not fecha:
            datos = await _formulario(request)
            fecha = datos.get("fecha", "").strip() or hoy().isoformat()
        try:
            date.fromisoformat(fecha)
        except ValueError:
            fecha = hoy().isoformat()

        def _hacer_borrar() -> None:
            con = abrir_db()
            try:
                db.borrar_comida(con, id_)
            finally:
                con.close()

        await run_in_threadpool(_hacer_borrar)
        return _volver("Entrada borrada", f"/comida?fecha={fecha}")

    def ver_comida_consejo(request: Request) -> Response:
        fecha = _fecha(request)
        momento_elegido = momento_valido(request.query_params.get("momento"))
        con = abrir_db()
        try:
            consejo_guardado = db.ultimo_consejo(con, fecha.isoformat()) if fecha == hoy() else None
            manana = fecha + timedelta(days=1)
            items_plan = calendario.plan(con, fecha, manana, fecha)
            acts = db.actividades(con, fecha.isoformat(), manana.isoformat())
            ahora_hora = datetime.now(ZONA).strftime("%H:%M")
            proximo_titulo: str | None = None
            proximo_detalles: list[str] = []
            for p in items_plan:
                if calendario.hecho(p, acts):
                    continue
                if p.fecha == fecha.isoformat():
                    if p.inicio and len(p.inicio) >= 5 and p.inicio[:5] <= ahora_hora:
                        continue
                deporte = metricas.nombre_tipo(p.tipo) if p.tipo else p.titulo
                dia_str = "hoy" if p.fecha == fecha.isoformat() else "mañana"
                hora_str = f" · {p.inicio[:5]}" if p.inicio else ""
                proximo_titulo = f"{deporte} {dia_str}{hora_str}"
                if p.duracion_min:
                    proximo_detalles.append(f"{p.duracion_min} min")
                if p.intensidad:
                    proximo_detalles.append(p.intensidad)
                break

            resumen_trat = tratamiento.resumen(con, hoy())
            if resumen_trat:
                farmaco = resumen_trat["farmaco"]
                dias = resumen_trat["dias_desde_ultima"]
                proximo_detalles.append(f"{farmaco}: día {dias} tras el pinchazo")

            proximo_info = None
            if proximo_titulo:
                proximo_info = {
                    "titulo": proximo_titulo,
                    "detalle": " · ".join(proximo_detalles),
                }
            elif proximo_detalles:
                proximo_info = {
                    "titulo": "Sin ejercicio planificado",
                    "detalle": " · ".join(proximo_detalles),
                }
        finally:
            con.close()

        err_consejo = error_consejo["texto"]
        error_consejo["texto"] = None

        return _render(
            "comida_consejo.html",
            pestana="consejo",
            seccion="comida",
            fecha=fecha.isoformat(),
            momento_elegido=momento_elegido,
            proximo=proximo_info,
            consejo=consejo_guardado,
            aconsejando=aconsejando.locked(),
            error_consejo=err_consejo,
            mensaje=request.query_params.get("mensaje"),
            error=request.query_params.get("error"),
        )

    async def pedir_consejo_web(request: Request) -> Response:
        datos = await _formulario(request)
        nota = datos.get("nota", "")
        if not aconsejando.acquire(blocking=False):
            return _volver("Ya estoy preparando el consejo", "/comida/consejo")
        error_consejo["texto"] = None

        def _trabajo() -> None:
            con = abrir_db()
            try:
                if pedir_consejo is not None:
                    pedir_consejo(con, nota)
                else:
                    consejo.pedir_consejo(con, settings, datetime.now(ZONA), nota)
            except ia.ErrorIA as exc:
                error_consejo["texto"] = str(exc)
            except Exception:
                log.exception("Error al preparar el consejo")
                error_consejo["texto"] = "No se pudo preparar el consejo"
            finally:
                con.close()
                aconsejando.release()

        threading.Thread(target=_trabajo, daemon=True).start()
        return _volver("Preparando el consejo", "/comida/consejo")

    def estado_consejo(_request: Request) -> Response:
        return JSONResponse({"preparando": aconsejando.locked()})

    # --- Tratamiento ---------------------------------------------------------

    def ver_tratamiento(request: Request) -> Response:
        con = abrir_db()
        try:
            trat = _tratamiento(con, hoy())
            lista_dosis = db.dosis(con)

            historial: list[dict[str, Any]] = []
            for i, d in enumerate(lista_dosis):
                dias_ant: int | None = None
                if i + 1 < len(lista_dosis):
                    d_ant = lista_dosis[i + 1]
                    dias_ant = (date.fromisoformat(d.fecha) - date.fromisoformat(d_ant.fecha)).days

                v_peso = db.ultimo(con, "peso", d.fecha)
                peso_info: str | None = None
                if v_peso is not None and v_peso.valor is not None:
                    p_txt = f"{cifra(v_peso.valor, 1)} kg"
                    if v_peso.fecha != d.fecha:
                        p_txt = f"{p_txt} ({graficas.fecha_corta(v_peso.fecha)})"
                    peso_info = p_txt

                historial.append(
                    {
                        "dosis": d,
                        "dias_desde_anterior": dias_ant,
                        "peso": peso_info,
                    }
                )
        finally:
            con.close()

        return _render(
            "tratamiento.html",
            seccion="salud",
            t=trat,
            con_enlace=False,
            pauta=trat["pauta"],
            zonas=tratamiento.ZONAS,
            historial=historial,
            hoy_str=hoy().isoformat(),
            mensaje=request.query_params.get("mensaje"),
            error=request.query_params.get("error"),
        )

    async def guardar_tratamiento_dosis(request: Request) -> Response:
        datos = await _formulario(request)

        def _guardar() -> None:
            c = abrir_db()
            try:
                args = tratamiento.dosis_desde_formulario(datos, db.pauta(c), hoy())
                db.anadir_dosis(c, **args)
            finally:
                c.close()

        try:
            await run_in_threadpool(_guardar)
        except ValueError as exc:
            return _volver(str(exc), "/tratamiento")
        return _volver("Dosis registrada", "/tratamiento")

    async def borrar_tratamiento_dosis(request: Request) -> Response:
        id_str = request.path_params.get("id", "")
        try:
            id_ = int(id_str)
        except ValueError:
            return _volver("Dosis no válida", "/tratamiento")

        def _borrar() -> None:
            c = abrir_db()
            try:
                db.borrar_dosis(c, id_)
            finally:
                c.close()

        await run_in_threadpool(_borrar)
        return _volver("Dosis borrada", "/tratamiento")

    async def guardar_tratamiento_pauta(request: Request) -> Response:
        datos = await _formulario(request)
        try:
            p = tratamiento.pauta_desde_formulario(datos)
        except ValueError as exc:
            return _volver(str(exc), "/tratamiento")

        def _guardar() -> None:
            c = abrir_db()
            try:
                db.guardar_pauta(c, p)
            finally:
                c.close()

        await run_in_threadpool(_guardar)
        return _volver("Pauta guardada", "/tratamiento")

    # --- Entrar en la web pública (ver acceso.py) ---------------------------

    redirect_login = f"{settings.url_publica}{acceso.RUTA_CALLBACK}"
    # Solo la entrada pública usa Google; sin credenciales no arranca.
    client_id, client_secret = (
        acceso.leer_google_oauth(settings.google_oauth) if publica else ("", "")
    )
    login_google = (crear_login or acceso.GoogleLogin)(client_id, client_secret)

    def ver_entrar(request: Request) -> Response:
        return _render("entrar.html", mensaje=request.query_params.get("mensaje"))

    def entrar_google(_request: Request) -> Response:
        state = secrets.token_urlsafe(24)
        con = abrir_db()
        try:
            db.guardar_autorizacion(con, state)
        finally:
            con.close()
        respuesta = RedirectResponse(
            acceso.url_login(client_id, redirect_login, state), status_code=303
        )
        respuesta.set_cookie(
            acceso.COOKIE_STATE,
            state,
            max_age=600,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return respuesta

    def vuelta_google(request: Request) -> Response:
        params = request.query_params
        if "error" in params or not params.get("code") or not params.get("state"):
            return _volver("Google no confirmó quién eres", "/entrar")
        state_cookie = request.cookies.get(acceso.COOKIE_STATE)
        if not state_cookie or state_cookie != params.get("state"):
            return _volver("Esa entrada ya no vale: vuelve a intentarlo", "/entrar")
        con = abrir_db()
        try:
            if not db.consumir_autorizacion(con, params["state"]):
                return _volver("Esa entrada ya no vale: vuelve a intentarlo", "/entrar")
            try:
                correo_login = login_google.correo(params["code"], redirect_login)
            except acceso.LoginError as exc:
                return PlainTextResponse(f"No se pudo entrar con Google: {exc}", status_code=400)
            if correo_login not in settings.cuentas_google:
                return _volver("Esa cuenta no tiene acceso", "/entrar")
            token = db.crear_sesion_web(con, correo_login, db.DIAS_SESION)
        finally:
            con.close()
        respuesta = RedirectResponse("/", status_code=303)
        respuesta.set_cookie(
            acceso.COOKIE_SESION,
            token,
            # La caducidad la decide la base (se renueva con el uso); la cookie, el máximo
            # que admiten los navegadores.
            max_age=400 * 86400,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        respuesta.delete_cookie(
            acceso.COOKIE_STATE, path="/", secure=True, httponly=True, samesite="lax"
        )
        return respuesta

    def salir(request: Request) -> Response:
        token = request.cookies.get(acceso.COOKIE_SESION)
        if token:
            con = abrir_db()
            try:
                db.cerrar_sesion_web(con, token)
            finally:
                con.close()
        respuesta = _volver("Has salido", "/entrar")
        respuesta.delete_cookie(
            acceso.COOKIE_SESION, path="/", secure=True, httponly=True, samesite="lax"
        )
        return respuesta

    return Starlette(
        routes=[
            Route("/", ver_hoy),
            Route("/salud", ver_salud),
            Route("/tendencias", redirigir_tendencias),
            Route("/actividades", ver_actividades),
            Route("/actividades/semana-tipo", ver_semana_tipo),
            Route("/actividades/semana-tipo", guardar_semana_tipo_web, methods=["POST"]),
            Route("/calendario/sincronizar", sincronizar_calendario_web, methods=["POST"]),
            Route("/calendario/titulo", calendario_titulo_web, methods=["POST"]),
            Route("/lecturas", ver_lecturas),
            Route("/tratamiento", ver_tratamiento),
            Route("/tratamiento/dosis", guardar_tratamiento_dosis, methods=["POST"]),
            Route("/tratamiento/dosis/{id:int}/borrar", borrar_tratamiento_dosis, methods=["POST"]),
            Route("/tratamiento/pauta", guardar_tratamiento_pauta, methods=["POST"]),
            Route("/comida", ver_comida),
            Route("/comida/ia", ver_comida_ia),
            Route("/comida/ia", crear_comida_ia_web, methods=["POST"]),
            Route("/comida/ia/{id:int}", ver_comida_ia_borrador),
            Route("/comida/ia/{id:int}/estado", estado_comida_ia),
            Route("/comida/ia/{id:int}/descartar", descartar_comida_ia_web, methods=["POST"]),
            Route("/comida/ia/{id:int}/anadir", anadir_comida_ia_web, methods=["POST"]),
            Route("/comida/buscar", buscar_comida),
            Route("/comida/repetir", repetir_comida, methods=["POST"]),
            Route("/comida/consejo", ver_comida_consejo),
            Route("/comida/consejo", pedir_consejo_web, methods=["POST"]),
            Route("/comida/consejo/estado", estado_consejo),
            Route("/comida/objetivos", ver_comida_objetivos),
            Route("/comida/objetivos", guardar_comida_objetivos, methods=["POST"]),
            Route("/comida/nuevo", ver_comida_nuevo),
            Route("/comida/productos", guardar_productos, methods=["POST"]),
            Route("/comida/producto", ver_producto),
            Route("/comida/propio/{id:int}", ver_comida_propio),
            Route("/comida/generico/{codigo}", ver_generico),
            Route("/comida/etiqueta", leer_etiqueta_foto, methods=["POST"]),
            Route("/comida/anadir", anadir, methods=["POST"]),
            Route("/comida/{id:int}/borrar", borrar, methods=["POST"]),
            Route("/sincronizar", sincronizar, methods=["POST"]),
            Route("/lectura", pedir_lectura, methods=["POST"]),
            Route("/conexiones", ver_conexiones),
            Route("/ia/uso", ver_ia_uso),
            Route("/conexiones/garmin/entrar", garmin_entrar, methods=["POST"]),
            Route("/conexiones/garmin/codigo", garmin_codigo, methods=["POST"]),
            Route("/conexiones/garmin/probar", garmin_probar, methods=["POST"]),
            Route("/manifest.webmanifest", manifiesto),
            Route("/sw.js", service_worker),
            Mount("/estaticos", StaticFiles(directory=ESTATICOS), name="estaticos"),
        ]
        + (
            [
                Route("/entrar", ver_entrar),
                Route("/entrar/google", entrar_google),
                Route(acceso.RUTA_CALLBACK, vuelta_google),
                Route("/salir", salir, methods=["POST"]),
            ]
            if publica
            else []
        ),
        middleware=[
            Middleware(
                AccesoMiddleware,
                publica=publica,
                usuarios=settings.usuarios,
                cuentas_google=settings.cuentas_google,
                abrir_db=abrir_db,
            )
        ],
    )
