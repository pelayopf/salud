"""Web de la app: Hoy, Tendencias, Actividades y Lecturas."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import db, graficas, lectura, sync
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
    from urllib.parse import urlencode

    separador = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{separador}{urlencode({'mensaje': mensaje})}", 303)


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


# --- Aplicación ----------------------------------------------------------------


def crear_app(
    settings: Settings,
    abrir_db: Callable[[], sqlite3.Connection],
    conectar_garmin: Callable[[], FuenteGarmin],
    lector: Callable[[sqlite3.Connection, date], dict[str, Any]] | None = None,
    *,
    hoy: Callable[[], date] = lambda: datetime.now(ZONA).date(),
) -> Starlette:
    """Construye la aplicación web."""
    generando = threading.Lock()

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

    return Starlette(
        routes=[
            Route("/", ver_hoy),
            Route("/tendencias", ver_tendencias),
            Route("/actividades", ver_actividades),
            Route("/lecturas", ver_lecturas),
            Route("/sincronizar", sincronizar, methods=["POST"]),
            Route("/lectura", pedir_lectura, methods=["POST"]),
            Route("/manifest.webmanifest", manifiesto),
            Route("/sw.js", service_worker),
            Mount("/estaticos", StaticFiles(directory=ESTATICOS), name="estaticos"),
        ],
        middleware=[Middleware(AccesoMiddleware, usuarios=settings.usuarios)],
    )
