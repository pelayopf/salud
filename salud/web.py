"""Web de la app: Hoy, Tendencias, Actividades, Lecturas y Conexiones."""

from __future__ import annotations

import hashlib
import logging
import math
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
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
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import alimentos, db, garmin, graficas, ia, lectura, sync
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
PLANTILLAS.filters["cifra"] = cifra
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


PLANTILLAS.filters["horas"] = horas_y_minutos
PLANTILLAS.filters["fecha_larga"] = fecha_larga
PLANTILLAS.filters["fecha_corta"] = lambda f: graficas.fecha_corta(f)
PLANTILLAS.filters["hace"] = hace


def _render(nombre: str, **contexto: Any) -> HTMLResponse:
    return HTMLResponse(PLANTILLAS.get_template(nombre).render(**contexto))


def _volver(mensaje: str, destino: str = "/") -> RedirectResponse:
    separador = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{separador}{urlencode({'mensaje': mensaje})}", 303)


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


@dataclass(frozen=True)
class Tarjeta:
    """Una cifra de Hoy: valor, comparación con lo normal y mini-gráfica."""

    titulo: str
    valor: str
    unidad: str = ""
    detalle: str = ""
    comparacion: str = ""
    clase: str = "neutro"  # bien / mal / neutro: sentido del cambio por si subir es bueno
    spark: Markup = Markup("")
    fecha: str = ""  # si el valor no es del día que se mira


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
    claves = ("sueno_h", "vfc_noche", "fc_reposo", "bb_despertar", "preparacion")
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
    sueno = valor("sueno_h")
    puntuacion = d.get("sueno_puntuacion")
    detalle_sueno = ""
    if puntuacion and puntuacion.valor is not None:
        detalle_sueno = f"Puntuación {cifra(puntuacion.valor)}"
        if puntuacion.texto:
            detalle_sueno += f" · {texto('sueno_puntuacion', puntuacion.texto)}"
    comp, clase = normal("sueno_h")
    tarjetas.append(
        Tarjeta("Sueño", horas_y_minutos(sueno), "", detalle_sueno, comp, clase, spark("sueno_h"))
    )

    vfc = d.get("vfc_noche")
    lo, hi = valor("vfc_base_baja"), valor("vfc_base_alta")
    partes_vfc = [texto("vfc_noche", vfc.texto).capitalize()] if vfc and vfc.texto else []
    if lo is not None and hi is not None:
        partes_vfc.append(f"tu franja equilibrada: {cifra(lo)}–{cifra(hi)} ms")
    detalle_vfc = " · ".join(partes_vfc)
    comp, clase = normal("vfc_noche")
    tarjetas.append(
        Tarjeta(
            "VFC nocturna",
            cifra(valor("vfc_noche")),
            "ms",
            detalle_vfc,
            comp,
            clase,
            spark("vfc_noche"),
        )
    )

    comp, clase = normal("fc_reposo")
    tarjetas.append(
        Tarjeta(
            "FC en reposo", cifra(valor("fc_reposo")), "lpm", "", comp, clase, spark("fc_reposo")
        )
    )

    detalle_bb = ""
    if valor("bb_max") is not None and valor("bb_min") is not None:
        detalle_bb = f"Máxima {cifra(valor('bb_max'))} · mínima {cifra(valor('bb_min'))}"
    comp, clase = normal("bb_despertar")
    tarjetas.append(
        Tarjeta(
            "Body Battery al despertar",
            cifra(valor("bb_despertar")),
            "",
            detalle_bb,
            comp,
            clase,
            spark("bb_despertar"),
        )
    )

    prep = d.get("preparacion")
    partes = []
    if prep and prep.texto:
        partes.append(texto("preparacion", prep.texto).capitalize())
    if valor("recuperacion_h"):
        partes.append(f"recuperación {cifra(valor('recuperacion_h'))} h")
    comp, clase = normal("preparacion")
    tarjetas.append(
        Tarjeta(
            "Preparación para entrenar",
            cifra(valor("preparacion")),
            "",
            " · ".join(partes),
            comp,
            clase,
            spark("preparacion"),
        )
    )

    peso = db.ultimo(con, "peso", hoy)
    if peso is not None and peso.valor is not None:
        antes = db.ultimo(
            con, "peso", (date.fromisoformat(peso.fecha) - timedelta(days=30)).isoformat()
        )
        comp, clase = ("", "neutro")
        if antes is not None and antes.valor is not None:
            comp, clase = _comparacion("peso", peso.valor, antes.valor, "en 30 días")
        grasa = db.ultimo(con, "grasa", hoy)
        detalle_peso = (
            f"Grasa {cifra(grasa.valor, 1)} %" if grasa and grasa.fecha == peso.fecha else ""
        )
        serie_peso = db.serie(con, "peso", (fecha - timedelta(days=60)).isoformat(), hoy)
        tarjetas.append(
            Tarjeta(
                "Peso",
                cifra(peso.valor, 1),
                "kg",
                detalle_peso,
                comp,
                clase,
                graficas.sparkline([v.valor for v in serie_peso]),
                peso.fecha if peso.fecha != hoy else "",
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

    desayuno si hora < 11, comida si < 16, merienda si < 20, cena el resto.
    """
    if hora < 11:
        return "desayuno"
    if hora < 16:
        return "comida"
    if hora < 20:
        return "merienda"
    return "cena"


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

    origen = "etiqueta-ia" if datos.get("ia") == "1" else "manual"

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
    )


# --- Aplicación ----------------------------------------------------------------


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
) -> Starlette:
    """Construye la aplicación web."""
    leer = lector_etiqueta or (lambda imagen: ia.leer_etiqueta(settings, imagen))
    generando = threading.Lock()
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
            semana = (fecha - timedelta(days=6)).isoformat()
            return _render(
                "hoy.html",
                seccion="hoy",
                fecha=fecha.isoformat(),
                es_hoy=fecha == hoy(),
                anterior=(fecha - timedelta(days=1)).isoformat(),
                siguiente=(fecha + timedelta(days=1)).isoformat() if fecha < hoy() else None,
                lectura=db.lectura(con, fecha.isoformat()),
                generando=generando.locked(),
                tarjetas=_tarjetas(con, fecha),
                rendimiento=_rendimiento(con, fecha),
                actividades=_actividades(db.actividades(con, semana, fecha.isoformat())),
                sync=db.ultima_sincronizacion(con, GARMIN),
                mensaje=request.query_params.get("mensaje"),
            )
        finally:
            con.close()

    def ver_tendencias(request: Request) -> Response:
        try:
            dias = int(request.query_params.get("dias", "90"))
        except ValueError:
            dias = 90
        dias = dias if dias in PERIODOS else 90
        fin = hoy()
        inicio = fin - timedelta(days=dias - 1)
        fechas = [(inicio + timedelta(days=i)).isoformat() for i in range(dias)]
        claves = (
            "peso",
            "fc_reposo",
            "vfc_noche",
            "vfc_base_baja",
            "vfc_base_alta",
            "sueno_h",
            "preparacion",
            "bb_despertar",
            "vo2max",
        )
        con = abrir_db()
        try:
            datos = db.series(con, claves, inicio.isoformat(), fin.isoformat())
            carga = _carga_semanal(con, inicio, fin)
        finally:
            con.close()
        por_fecha = {c: {v.fecha: v.valor for v in valores} for c, valores in datos.items()}

        def puntos(clave: str) -> list[tuple[str, float | None]]:
            return [(f, por_fecha[clave].get(f)) for f in fechas]

        franja_vfc = [
            (f, por_fecha["vfc_base_baja"].get(f), por_fecha["vfc_base_alta"].get(f))
            for f in fechas
        ]
        graficas_ = []
        # (métrica, título, unir huecos: el peso y el VO2max no son diarios,
        #  rango mínimo del eje: que 0,3 de VO2max no parezca un salto)
        for clave, titulo, unir, rango in (
            ("peso", "Peso", True, 4),
            ("fc_reposo", "FC en reposo", False, 10),
            ("vfc_noche", "VFC nocturna", False, 20),
            ("sueno_h", "Sueño", False, 4),
            ("preparacion", "Preparación para entrenar", False, 0),
            ("bb_despertar", "Body Battery al despertar", False, 0),
            ("vo2max", "VO2max", True, 4),
        ):
            d = CATALOGO[clave]
            g = graficas.linea(
                puntos(clave),
                decimales=d.decimales,
                unidad=d.unidad,
                titulo=titulo,
                unir_huecos=unir,
                rango_minimo=rango,
                banda=franja_vfc if clave == "vfc_noche" else None,
            )
            nota = (
                "La franja gris es tu VFC equilibrada según Garmin." if clave == "vfc_noche" else ""
            )
            graficas_.append({"titulo": titulo, "unidad": d.unidad, "grafica": g, "nota": nota})
        g_carga = graficas.columnas(carga, titulo="Carga de entrenamiento por semana")
        graficas_.append(
            {
                "titulo": "Carga de entrenamiento por semana",
                "unidad": "",
                "grafica": g_carga,
                "nota": "Suma de la carga de Garmin de cada semana, de lunes a domingo.",
            }
        )
        return _render(
            "tendencias.html",
            seccion="tendencias",
            dias=dias,
            periodos=PERIODOS,
            graficas=graficas_,
        )

    def ver_actividades(request: Request) -> Response:
        try:
            dias = int(request.query_params.get("dias", "30"))
        except ValueError:
            dias = 30
        dias = dias if dias in PERIODOS else 30
        fin = hoy()
        con = abrir_db()
        try:
            filas = db.actividades(
                con, (fin - timedelta(days=dias - 1)).isoformat(), fin.isoformat()
            )
        finally:
            con.close()
        return _render(
            "actividades.html",
            seccion="actividades",
            dias=dias,
            periodos=PERIODOS,
            actividades=_actividades(filas),
        )

    def ver_lecturas(_request: Request) -> Response:
        con = abrir_db()
        try:
            return _render("lecturas.html", seccion="lecturas", lecturas=db.lecturas(con))
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
        con = abrir_db()
        try:
            sync_garmin = db.ultima_sincronizacion(con, GARMIN)
        finally:
            con.close()
        return _render(
            "conexiones.html",
            seccion="conexiones",
            sesion_desde=desde,
            sync=sync_garmin,
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
    ) -> HTMLResponse:
        con = abrir_db()
        try:
            comidas_dia = db.comidas(con, fecha.isoformat())
            totales = db.totales_dia(con, fecha.isoformat())
            recientes = db.productos_recientes(con)
            propios = db.productos_propios(con)
        finally:
            con.close()
        comidas_por_momento: dict[str, list[db.Comida]] = {m: [] for m in db.MOMENTOS}
        for c in comidas_dia:
            comidas_por_momento.setdefault(c.momento, []).append(c)
        return _render(
            "comida.html",
            seccion="comida",
            fecha=fecha.isoformat(),
            es_hoy=fecha == hoy(),
            anterior=(fecha - timedelta(days=1)).isoformat(),
            siguiente=(fecha + timedelta(days=1)).isoformat() if fecha < hoy() else None,
            comidas_por_momento=comidas_por_momento,
            totales=totales,
            recientes=recientes,
            productos_propios=propios,
            momentos=db.MOMENTOS,
            momento_defecto=momento_por_hora(datetime.now(ZONA).hour),
            mensaje=request.query_params.get("mensaje"),
            error=error or request.query_params.get("error"),
            codigo_buscado=codigo_buscado,
        )

    def ver_comida(request: Request) -> Response:
        return _pagina_comida(request, _fecha(request))

    def ver_comida_nuevo(request: Request) -> Response:
        fecha = _fecha(request)
        codigo = request.query_params.get("codigo", "").strip()
        aviso = request.query_params.get("aviso")
        mensaje = request.query_params.get("mensaje")
        error = request.query_params.get("error")
        return _render(
            "comida_nuevo.html",
            seccion="comida",
            fecha=fecha.isoformat(),
            codigo=codigo,
            aviso=aviso,
            mensaje=mensaje,
            error=error,
        )

    def ver_producto(request: Request) -> Response:
        fecha = _fecha(request)
        codigo = request.query_params.get("codigo", "").strip()
        if not codigo:
            return _pagina_comida(request, fecha, error="Escribe un código de barras")
        con = abrir_db()
        try:
            try:
                res = alimentos.obtener_producto(con, codigo, cliente_off)
            except alimentos.ErrorAlimentos as exc:
                if isinstance(exc, alimentos.CodigoInvalido):
                    error_msg = f"Código no válido: {codigo}"
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
                    )
                else:
                    error_msg = "Open Food Facts no responde en este momento"
                return _pagina_comida(request, fecha, error=error_msg, codigo_buscado=codigo)
        finally:
            con.close()
        return _render(
            "comida_producto.html",
            seccion="comida",
            producto=res.producto,
            desde_cache=res.desde_cache,
            fecha=fecha.isoformat(),
            momentos=db.MOMENTOS,
            momento_defecto=momento_por_hora(datetime.now(ZONA).hour),
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

        destino_error = (
            f"/comida/propio/{id_}?fecha={fecha_str}"
            if id_ is not None
            else (
                f"/comida/producto?{urlencode({'codigo': codigo_form, 'fecha': fecha_str})}"
                if codigo_form
                else f"/comida/nuevo?fecha={fecha_str}"
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
                )
            finally:
                con.close()

        try:
            guardado_id = await run_in_threadpool(_guardar)
        except ValueError as exc:
            return _volver(str(exc), destino_error)

        if dp.codigo:
            return _volver(
                "Producto guardado", f"/comida/producto?codigo={dp.codigo}&fecha={fecha_str}"
            )
        return _volver("Producto guardado", f"/comida/propio/{guardado_id}?fecha={fecha_str}")

    def ver_comida_propio(request: Request) -> Response:
        id_ = int(request.path_params["id"])
        fecha = _fecha(request)
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
            momento_defecto=momento_por_hora(datetime.now(ZONA).hour),
            mensaje=request.query_params.get("mensaje"),
        )

    async def leer_etiqueta_foto(request: Request) -> Response:
        cl_hdr = request.headers.get("content-length")
        if cl_hdr:
            try:
                if int(cl_hdr) > ia.MAX_BYTES_IMAGEN:
                    return JSONResponse({"error": "La foto es demasiado grande"}, 413)
            except ValueError:
                pass

        partes: list[bytes] = []
        tam = 0
        async for trozo in request.stream():
            tam += len(trozo)
            if tam > ia.MAX_BYTES_IMAGEN:
                return JSONResponse({"error": "La foto es demasiado grande"}, 413)
            partes.append(trozo)
        cuerpo = b"".join(partes)

        if ia.tipo_imagen(cuerpo) is None:
            return JSONResponse({"error": "El fichero no es una imagen JPEG, PNG o WebP"}, 415)

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
        momento = datos.get("momento", "").strip()
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
                nombre = prod.nombre or f"Producto {prod.codigo or id_propio}"
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
                nombre = prod.nombre or f"Producto {codigo}"
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

        propio_raw = datos.get("propio", "").strip()
        codigo = datos.get("codigo", "").strip()
        try:
            cantidad_str = (
                datos.get("cantidad", "100" if (codigo or propio_raw) else "")
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

    return Starlette(
        routes=[
            Route("/", ver_hoy),
            Route("/tendencias", ver_tendencias),
            Route("/actividades", ver_actividades),
            Route("/lecturas", ver_lecturas),
            Route("/comida", ver_comida),
            Route("/comida/nuevo", ver_comida_nuevo),
            Route("/comida/productos", guardar_productos, methods=["POST"]),
            Route("/comida/producto", ver_producto),
            Route("/comida/propio/{id:int}", ver_comida_propio),
            Route("/comida/etiqueta", leer_etiqueta_foto, methods=["POST"]),
            Route("/comida/anadir", anadir, methods=["POST"]),
            Route("/comida/{id:int}/borrar", borrar, methods=["POST"]),
            Route("/sincronizar", sincronizar, methods=["POST"]),
            Route("/lectura", pedir_lectura, methods=["POST"]),
            Route("/conexiones", ver_conexiones),
            Route("/conexiones/garmin/entrar", garmin_entrar, methods=["POST"]),
            Route("/conexiones/garmin/codigo", garmin_codigo, methods=["POST"]),
            Route("/conexiones/garmin/probar", garmin_probar, methods=["POST"]),
            Route("/manifest.webmanifest", manifiesto),
            Route("/sw.js", service_worker),
            Mount("/estaticos", StaticFiles(directory=ESTATICOS), name="estaticos"),
        ],
        middleware=[Middleware(AccesoMiddleware, usuarios=settings.usuarios)],
    )
