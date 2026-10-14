"""Línea de órdenes de salud.

Subcomandos: `serve | sync | lectura | garmin-entrar | estado | producto | calendario | ciqual`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import alimentos, calendario, db, garmin, lectura, sync
from .config import ConfigError, Settings, load_settings
from .garmin import GarminNoConectado

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
# Los últimos días se vuelven a pedir: Garmin completa el día cuando el reloj sincroniza.
DIAS_SYNC = 3
DIAS_SYNC_LECTURA = 2


def _hoy() -> date:
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def _fecha(valor: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"fecha no válida (AAAA-MM-DD): {valor}") from exc


def _sincronizar(settings: Settings, desde: date, hasta: date) -> int:
    try:
        cliente = garmin.conectar(settings.garmin_tokens)
    except GarminNoConectado as exc:
        print(exc, file=sys.stderr)
        return 1
    con = db.conectar(settings.db_path)
    try:
        r = sync.sincronizar(con, cliente, desde, hasta, avisar=lambda m: print(m, flush=True))
    finally:
        con.close()
    print(f"{r.estado}: {r.metricas} datos y {r.actividades} actividades")
    for e in r.errores:
        print(f"  fallo: {e}", file=sys.stderr)
    return 0 if r.estado != "error" else 1


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """Arranca la web."""
    import uvicorn

    from .web import crear_app

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: garmin.conectar(settings.garmin_tokens),
    )
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    """Trae de Garmin los últimos días (o desde una fecha)."""
    hasta = _hoy()
    desde = args.desde or hasta - timedelta(days=args.dias - 1)
    codigo = _sincronizar(settings, desde, hasta)
    if settings.gcal_ics:
        con = db.conectar(settings.db_path)
        try:
            r = calendario.sincronizar_calendario(con, settings.gcal_ics, hasta)
            print(f"Calendario: {r.deportes} deportes ({r.descartados} eventos descartados)")
        except calendario.ErrorCalendario as exc:
            print(f"Calendario: fallo: {exc}", file=sys.stderr)
        finally:
            con.close()
    return codigo


def cmd_calendario(settings: Settings, _args: argparse.Namespace) -> int:
    """Sincroniza los eventos deportivos de Google Calendar."""
    if not settings.gcal_ics:
        print("Falta SALUD_GCAL_ICS en el .env")
        return 2
    con = db.conectar(settings.db_path)
    try:
        r = calendario.sincronizar_calendario(con, settings.gcal_ics, _hoy())
    except calendario.ErrorCalendario as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        con.close()
    print(f"Calendario: {r.deportes} deportes ({r.descartados} eventos descartados)")
    return 0


def cmd_lectura(settings: Settings, args: argparse.Namespace) -> int:
    """Prepara la lectura del día con Claude Code."""
    fecha = args.fecha or _hoy()
    con = db.conectar(settings.db_path)
    try:
        previa = db.lectura(con, fecha.isoformat())
        if previa and previa["estado"] == "ok" and not args.rehacer:
            print(f"Ya hay lectura del {fecha} (usa --rehacer para repetirla)")
            return 0
    finally:
        con.close()
    if args.sincronizar:
        # Si Garmin falla, la lectura se hace igual con lo que haya.
        _sincronizar(settings, fecha - timedelta(days=DIAS_SYNC_LECTURA - 1), fecha)
    con = db.conectar(settings.db_path)
    try:
        contenido = lectura.generar(con, settings, fecha)
    except lectura.LecturaError as exc:
        print(f"La lectura falló: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()
    print(f"[{contenido['estado']}] {contenido['titular']}")
    print(contenido["analisis"])
    print(f"Entreno: {contenido['entreno']['titulo']}. {contenido['entreno']['detalle']}")
    print(f"Comida: {contenido['comida']}")
    for a in contenido["alertas"]:
        print(f"Ojo: {a}")
    return 0


def cmd_garmin_entrar(settings: Settings, _args: argparse.Namespace) -> int:
    """Entra en Garmin y guarda la sesión (solo los tokens)."""
    correo = input("Correo de Garmin: ").strip()
    contrasena = getpass.getpass("Contraseña (no se guarda): ")
    try:
        pendiente = garmin.empezar_entrada(settings.garmin_tokens, correo, contrasena)
        if pendiente is not None:
            codigo = input("Código de verificación que te ha mandado Garmin: ").strip()
            garmin.terminar_entrada(settings.garmin_tokens, pendiente, codigo)
    except Exception as exc:  # la librería lanza varios tipos según el fallo
        print(f"No se pudo entrar: {exc}", file=sys.stderr)
        return 1
    print(f"Sesión de Garmin guardada en {settings.garmin_tokens}")
    return 0


def cmd_estado(settings: Settings, _args: argparse.Namespace) -> int:
    """Resume qué hay en la base de datos."""
    con = db.conectar(settings.db_path)
    try:
        s = db.ultima_sincronizacion(con, "garmin")
        n = con.execute("SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM metricas").fetchone()
        a = con.execute("SELECT COUNT(*) FROM actividades").fetchone()[0]
        lecturas = db.lecturas(con, limite=1)
    finally:
        con.close()
    print(f"Base de datos: {settings.db_path}")
    print(f"Métricas: {n[0]} ({n[1]} a {n[2]}) · actividades: {a}")
    print(f"Última sincronización: {s['hecha']} ({s['estado']})" if s else "Sin sincronizar")
    if lecturas:
        ultima = lecturas[0]
        print(f"Última lectura: {ultima['fecha']} ({ultima['estado']})")
    return 0


def cmd_producto(settings: Settings, args: argparse.Namespace) -> int:
    """Muestra un producto de Open Food Facts (o de la caché)."""
    con = db.conectar(settings.db_path)
    try:
        prod, desde_cache = alimentos.obtener_producto(con, args.codigo)
    except alimentos.CodigoInvalido as exc:
        print(f"Código no válido: {exc}", file=sys.stderr)
        return 1
    except alimentos.ProductoNoEncontrado as exc:
        print(f"No encontrado: {exc}", file=sys.stderr)
        return 1
    except alimentos.OpenFoodFactsNoDisponible as exc:
        print(f"Open Food Facts no disponible: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()

    origen = "caché" if desde_cache else "Open Food Facts"
    nombre = prod.nombre or "Sin nombre"
    marca = f" · {prod.marca}" if prod.marca else ""
    print(f"[{prod.codigo}] {nombre}{marca} ({origen})")
    if prod.nutriscore:
        print(f"Nutri-Score: {prod.nutriscore}")

    def _val(v: float | None) -> str:
        return f"{v:.1f}" if v is not None else "—"

    print(
        f"Por 100 {prod.unidad}: {_val(prod.kcal)} kcal · "
        f"P: {_val(prod.proteinas)} g · "
        f"HC: {_val(prod.hidratos)} g · "
        f"G: {_val(prod.grasas)} g"
    )
    if prod.incompleto:
        print("Aviso: datos nutricionales incompletos")
    return 0


def cmd_ciqual(settings: Settings, args: argparse.Namespace) -> int:
    """Genera el CSV de alimentos genéricos de CIQUAL."""
    from . import ciqual

    origen = args.origen.expanduser()
    cache = args.cache.expanduser() if args.cache else None
    if not origen.is_dir():
        print(f"El directorio no existe: {origen}", file=sys.stderr)
        return 1
    filas = ciqual.generar_csv(origen, settings, cache=cache)
    print(f"CIQUAL: {filas} alimentos guardados en {ciqual.CSV}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada."""
    parser = argparse.ArgumentParser(prog="salud")
    sub = parser.add_subparsers(dest="comando", required=True)
    serve = sub.add_parser("serve", help="arranca la web")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.set_defaults(func=cmd_serve)
    s = sub.add_parser("sync", help="trae los datos de Garmin")
    s.add_argument("--dias", type=int, default=DIAS_SYNC, help="últimos N días (3)")
    s.add_argument("--desde", type=_fecha, help="desde esta fecha hasta hoy (AAAA-MM-DD)")
    s.set_defaults(func=cmd_sync)
    lec = sub.add_parser("lectura", help="prepara la lectura del día con Claude")
    lec.add_argument("--fecha", type=_fecha, help="otro día (AAAA-MM-DD)")
    lec.add_argument("--rehacer", action="store_true", help="aunque ya haya una")
    lec.add_argument("--sincronizar", action="store_true", help="trae antes los datos de Garmin")
    lec.set_defaults(func=cmd_lectura)
    sub.add_parser("garmin-entrar", help="entra en Garmin y guarda la sesión").set_defaults(
        func=cmd_garmin_entrar
    )
    sub.add_parser("estado", help="qué hay en la base de datos").set_defaults(func=cmd_estado)
    prod = sub.add_parser("producto", help="muestra un producto por su código de barras")
    prod.add_argument("codigo", help="código EAN-8, EAN-13 o UPC")
    prod.set_defaults(func=cmd_producto)
    sub.add_parser("calendario", help="sincroniza el calendario de Google").set_defaults(
        func=cmd_calendario
    )
    ciq = sub.add_parser("ciqual", help="genera el CSV de alimentos genéricos de CIQUAL")
    ciq.add_argument(
        "--origen",
        type=Path,
        required=True,
        help="directorio con los XML de CIQUAL",
    )
    ciq.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="fichero JSON de caché de traducciones",
    )
    ciq.set_defaults(func=cmd_ciqual)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuración: {exc}", file=sys.stderr)
        return 2
    return args.func(settings, args)


if __name__ == "__main__":
    sys.exit(main())
