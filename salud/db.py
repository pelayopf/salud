"""Persistencia en SQLite.

- `crudos`: la respuesta tal cual de cada fuente y día, para poder sacar
  métricas nuevas más adelante sin volver a pedírselas a Garmin.
- `metricas`: un valor por día, métrica y fuente. Si un día hay varias
  fuentes para la misma métrica, manda la primera de `PRIORIDAD`.
- `actividades`: entrenos y partidos, uno por actividad de cada fuente.
- `lecturas`: la lectura diaria hecha con Claude (una por día).
- `productos`: caché de Open Food Facts por código de barras.
- `comidas`: diario de alimentos por fecha y momento del día.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .metricas import Actividad, Metrica

# Qué fuente manda cuando hay varias para la misma métrica y día.
PRIORIDAD = ("manual", "garmin", "samsung", "propia")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crudos (
    fuente TEXT NOT NULL,
    tipo TEXT NOT NULL,
    fecha TEXT NOT NULL,
    datos TEXT NOT NULL,
    obtenido TEXT NOT NULL,
    PRIMARY KEY (fuente, tipo, fecha)
);
CREATE TABLE IF NOT EXISTS metricas (
    fecha TEXT NOT NULL,
    clave TEXT NOT NULL,
    fuente TEXT NOT NULL,
    valor REAL,
    texto TEXT,
    actualizado TEXT NOT NULL,
    PRIMARY KEY (fecha, clave, fuente)
);
CREATE INDEX IF NOT EXISTS metricas_clave ON metricas (clave, fecha);
CREATE TABLE IF NOT EXISTS actividades (
    fuente TEXT NOT NULL,
    id_fuente TEXT NOT NULL,
    inicio TEXT NOT NULL,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL,
    nombre TEXT NOT NULL DEFAULT '',
    duracion_s REAL,
    distancia_m REAL,
    kcal REAL,
    fc_media REAL,
    fc_max REAL,
    potencia_media REAL,
    potencia_normalizada REAL,
    tss REAL,
    carga REAL,
    efecto_aerobico REAL,
    efecto_anaerobico REAL,
    desnivel_m REAL,
    bruto TEXT NOT NULL,
    importado TEXT NOT NULL,
    PRIMARY KEY (fuente, id_fuente)
);
CREATE INDEX IF NOT EXISTS actividades_fecha ON actividades (fecha);
CREATE TABLE IF NOT EXISTS sincronizaciones (
    id INTEGER PRIMARY KEY,
    fuente TEXT NOT NULL,
    desde TEXT NOT NULL,
    hasta TEXT NOT NULL,
    estado TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT '',
    hecha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lecturas (
    fecha TEXT PRIMARY KEY,
    estado TEXT NOT NULL,
    contenido TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    modelo TEXT NOT NULL DEFAULT '',
    segundos REAL,
    generada TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS productos (
    codigo TEXT PRIMARY KEY,
    nombre TEXT,
    marca TEXT,
    nutriscore TEXT,
    unidad TEXT NOT NULL,
    kcal REAL,
    proteinas REAL,
    hidratos REAL,
    grasas REAL,
    datos TEXT NOT NULL,
    consultado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comidas (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    momento TEXT NOT NULL,
    nombre TEXT NOT NULL,
    codigo TEXT,
    cantidad REAL NOT NULL,
    unidad TEXT NOT NULL,
    kcal REAL,
    proteinas REAL,
    hidratos REAL,
    grasas REAL,
    creado TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comidas_fecha ON comidas (fecha);
"""

_CAMPOS_ACTIVIDAD = (
    "duracion_s",
    "distancia_m",
    "kcal",
    "fc_media",
    "fc_max",
    "potencia_media",
    "potencia_normalizada",
    "tss",
    "carga",
    "efecto_aerobico",
    "efecto_anaerobico",
    "desnivel_m",
)
_COLUMNAS_ACTIVIDAD = (
    "fuente",
    "id_fuente",
    "inicio",
    "fecha",
    "tipo",
    "nombre",
    *_CAMPOS_ACTIVIDAD,
    "bruto",
    "importado",
)
_INSERTAR_ACTIVIDAD = (
    f"INSERT OR REPLACE INTO actividades ({', '.join(_COLUMNAS_ACTIVIDAD)})"
    f" VALUES ({', '.join('?' * len(_COLUMNAS_ACTIVIDAD))})"
)


@dataclass(frozen=True)
class Valor:
    """El valor de una métrica en un día, con la fuente de la que sale."""

    fecha: str
    valor: float | None
    texto: str | None
    fuente: str


MOMENTOS = ("desayuno", "comida", "merienda", "cena", "otro")


@dataclass(frozen=True)
class Comida:
    """Una entrada en el diario de comidas."""

    id: int
    fecha: str
    momento: str
    nombre: str
    cantidad: float
    unidad: str
    kcal: float | None = None
    proteinas: float | None = None
    hidratos: float | None = None
    grasas: float | None = None
    codigo: str | None = None
    creado: str = ""


@dataclass(frozen=True)
class Totales:
    """Suma de nutrientes de varias comidas.

    Los nutrientes que falten (None) no cuentan como 0: se suman los
    disponibles y se contabiliza en `sin_*` cuántas entradas no tenían ese dato.
    """

    kcal: float = 0.0
    proteinas: float = 0.0
    hidratos: float = 0.0
    grasas: float = 0.0
    sin_kcal: int = 0
    sin_proteinas: int = 0
    sin_hidratos: int = 0
    sin_grasas: int = 0
    entradas: int = 0
    incompletas: int = 0


class TotalesDia(NamedTuple):
    """Totales del día agrupados por momento y el total general."""

    por_momento: dict[str, Totales]
    total: Totales


def ahora() -> str:
    """Marca de tiempo en UTC, con segundos."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def conectar(ruta: Path | str) -> sqlite3.Connection:
    """Abre (creando si hace falta) la base de datos y aplica el esquema."""
    if str(ruta) != ":memory:":
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


# --- Escritura ---------------------------------------------------------------


def guardar_crudo(con: sqlite3.Connection, fuente: str, tipo: str, fecha: str, datos: Any) -> None:
    """Guarda (o reemplaza) la respuesta de una fuente para un día."""
    with con:
        con.execute(
            "INSERT OR REPLACE INTO crudos (fuente, tipo, fecha, datos, obtenido)"
            " VALUES (?, ?, ?, ?, ?)",
            (fuente, tipo, fecha, json.dumps(datos, ensure_ascii=False), ahora()),
        )


def guardar_metricas(con: sqlite3.Connection, metricas: Iterable[Metrica]) -> int:
    """Guarda métricas: si ya había valor de esa fuente y día, lo reemplaza."""
    momento = ahora()
    filas = [(m.fecha, m.clave, m.fuente, m.valor, m.texto, momento) for m in metricas]
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO metricas (fecha, clave, fuente, valor, texto, actualizado)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            filas,
        )
    return len(filas)


def guardar_actividades(con: sqlite3.Connection, actividades: Iterable[Actividad]) -> int:
    """Guarda actividades nuevas y actualiza las que ya estaban."""
    momento = ahora()
    n = 0
    with con:
        for a in actividades:
            con.execute(
                _INSERTAR_ACTIVIDAD,
                (
                    a.fuente,
                    a.id_fuente,
                    a.inicio,
                    a.fecha,
                    a.tipo,
                    a.nombre,
                    *(getattr(a, c) for c in _CAMPOS_ACTIVIDAD),
                    a.bruto,
                    momento,
                ),
            )
            n += 1
    return n


def registrar_sincronizacion(
    con: sqlite3.Connection, fuente: str, desde: str, hasta: str, estado: str, detalle: str = ""
) -> None:
    """Apunta una sincronización ("ok", "parcial" o "error")."""
    with con:
        con.execute(
            "INSERT INTO sincronizaciones (fuente, desde, hasta, estado, detalle, hecha)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (fuente, desde, hasta, estado, detalle, ahora()),
        )


def guardar_lectura(
    con: sqlite3.Connection,
    fecha: str,
    *,
    contenido: dict[str, Any] | None = None,
    error: str = "",
    modelo: str = "",
    segundos: float | None = None,
) -> None:
    """Guarda la lectura de un día (con su contenido o con el error)."""
    with con:
        con.execute(
            "INSERT OR REPLACE INTO lecturas"
            " (fecha, estado, contenido, error, modelo, segundos, generada)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fecha,
                "ok" if contenido is not None else "error",
                json.dumps(contenido, ensure_ascii=False) if contenido is not None else "",
                error,
                modelo,
                segundos,
                ahora(),
            ),
        )


def guardar_producto(
    con: sqlite3.Connection,
    codigo: str | dict[str, Any],
    nombre: str | None = None,
    marca: str | None = None,
    nutriscore: str | None = None,
    unidad: str = "g",
    kcal: float | None = None,
    proteinas: float | None = None,
    hidratos: float | None = None,
    grasas: float | None = None,
    *,
    datos: str | dict[str, Any] = "",
    consultado: str | None = None,
) -> None:
    """Guarda (o actualiza) un producto en la caché de Open Food Facts."""
    if isinstance(codigo, dict):
        d = codigo
        cod = str(d["codigo"])
        nom = d.get("nombre")
        mar = d.get("marca")
        nut = d.get("nutriscore")
        uni = d.get("unidad", "g")
        kc = d.get("kcal")
        prot = d.get("proteinas")
        hid = d.get("hidratos")
        gra = d.get("grasas")
        dat = d.get("datos", "")
        momento = d.get("consultado") or consultado or ahora()
    else:
        cod = codigo
        nom = nombre
        mar = marca
        nut = nutriscore
        uni = unidad
        kc = kcal
        prot = proteinas
        hid = hidratos
        gra = grasas
        dat = datos
        momento = consultado or ahora()

    datos_json = json.dumps(dat, ensure_ascii=False) if isinstance(dat, dict) else str(dat)

    with con:
        con.execute(
            "INSERT OR REPLACE INTO productos"
            " (codigo, nombre, marca, nutriscore, unidad, kcal, proteinas, hidratos,"
            " grasas, datos, consultado)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cod, nom, mar, nut, uni, kc, prot, hid, gra, datos_json, momento),
        )


def anadir_comida(
    con: sqlite3.Connection,
    fecha: str,
    momento: str,
    nombre: str,
    cantidad: float,
    unidad: str,
    *,
    kcal: float | None = None,
    proteinas: float | None = None,
    hidratos: float | None = None,
    grasas: float | None = None,
    codigo: str | None = None,
    creado: str | None = None,
) -> int:
    """Añade una entrada al diario de comidas y devuelve su identificador."""
    nombre_limpio = nombre.strip()
    if not nombre_limpio:
        raise ValueError("el nombre de la comida no puede estar vacío")
    if momento not in MOMENTOS:
        raise ValueError(f"momento no válido: {momento!r} (debe ser uno de {MOMENTOS})")
    if unidad not in ("g", "ml"):
        raise ValueError(f"unidad no válida: {unidad!r} (debe ser 'g' o 'ml')")
    if cantidad <= 0:
        raise ValueError(f"la cantidad debe ser mayor que 0: {cantidad}")

    momento_creado = creado or ahora()
    with con:
        cur = con.execute(
            "INSERT INTO comidas"
            " (fecha, momento, nombre, codigo, cantidad, unidad, kcal, proteinas,"
            " hidratos, grasas, creado)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fecha,
                momento,
                nombre_limpio,
                codigo,
                cantidad,
                unidad,
                kcal,
                proteinas,
                hidratos,
                grasas,
                momento_creado,
            ),
        )
        return int(cur.lastrowid or 0)


def borrar_comida(con: sqlite3.Connection, id_: int) -> bool:
    """Borra una comida por su identificador. Devuelve True si existía."""
    with con:
        cur = con.execute("DELETE FROM comidas WHERE id = ?", (id_,))
        return cur.rowcount > 0


# --- Lectura -----------------------------------------------------------------


def _prioridad(fuente: str) -> int:
    return PRIORIDAD.index(fuente) if fuente in PRIORIDAD else len(PRIORIDAD)


def _mejores(filas: Iterable[sqlite3.Row]) -> dict[tuple[str, str], Valor]:
    """De varias fuentes para la misma métrica y día, la que manda."""
    mejores: dict[tuple[str, str], Valor] = {}
    for f in filas:
        clave = (f["clave"], f["fecha"])
        actual = mejores.get(clave)
        if actual is None or _prioridad(f["fuente"]) < _prioridad(actual.fuente):
            mejores[clave] = Valor(f["fecha"], f["valor"], f["texto"], f["fuente"])
    return mejores


def dia(con: sqlite3.Connection, fecha: str) -> dict[str, Valor]:
    """Todas las métricas de un día."""
    filas = con.execute("SELECT * FROM metricas WHERE fecha = ?", (fecha,))
    return {clave: v for (clave, _), v in _mejores(filas).items()}


def series(
    con: sqlite3.Connection, claves: Iterable[str], desde: str, hasta: str
) -> dict[str, list[Valor]]:
    """Valores de varias métricas entre dos fechas (incluidas), ordenados por fecha."""
    claves = list(claves)
    resultado: dict[str, list[Valor]] = {c: [] for c in claves}
    if not claves:
        return resultado
    filas = con.execute(
        f"SELECT * FROM metricas WHERE clave IN ({', '.join('?' * len(claves))})"
        " AND fecha BETWEEN ? AND ?",
        (*claves, desde, hasta),
    )
    for (clave, _), v in sorted(_mejores(filas).items(), key=lambda kv: kv[0][1]):
        resultado[clave].append(v)
    return resultado


def serie(con: sqlite3.Connection, clave: str, desde: str, hasta: str) -> list[Valor]:
    """Valores de una métrica entre dos fechas (incluidas)."""
    return series(con, [clave], desde, hasta)[clave]


def ultimo(con: sqlite3.Connection, clave: str, hasta: str) -> Valor | None:
    """El último valor de una métrica hasta esa fecha (peso, VO2max, FTP...)."""
    fila = con.execute(
        "SELECT MAX(fecha) AS fecha FROM metricas WHERE clave = ? AND fecha <= ?"
        " AND (valor IS NOT NULL OR texto IS NOT NULL)",
        (clave, hasta),
    ).fetchone()
    if fila is None or fila["fecha"] is None:
        return None
    valores = serie(con, clave, fila["fecha"], fila["fecha"])
    return valores[0] if valores else None


def anterior(con: sqlite3.Connection, clave: str, antes_de: str) -> Valor | None:
    """El último valor estrictamente anterior a una fecha."""
    fila = con.execute(
        "SELECT MAX(fecha) AS fecha FROM metricas WHERE clave = ? AND fecha < ?"
        " AND valor IS NOT NULL",
        (clave, antes_de),
    ).fetchone()
    if fila is None or fila["fecha"] is None:
        return None
    return ultimo(con, clave, fila["fecha"])


def actividades(con: sqlite3.Connection, desde: str, hasta: str) -> list[sqlite3.Row]:
    """Actividades entre dos fechas (incluidas), la más reciente primero."""
    return con.execute(
        "SELECT * FROM actividades WHERE fecha BETWEEN ? AND ? ORDER BY inicio DESC",
        (desde, hasta),
    ).fetchall()


def ultima_sincronizacion(con: sqlite3.Connection, fuente: str) -> sqlite3.Row | None:
    """La última sincronización de una fuente."""
    return con.execute(
        "SELECT * FROM sincronizaciones WHERE fuente = ? ORDER BY id DESC LIMIT 1", (fuente,)
    ).fetchone()


def _lectura(fila: sqlite3.Row | None) -> dict[str, Any] | None:
    if fila is None:
        return None
    lectura = dict(fila)
    lectura["contenido"] = json.loads(fila["contenido"]) if fila["contenido"] else None
    return lectura


def lectura(con: sqlite3.Connection, fecha: str) -> dict[str, Any] | None:
    """La lectura de un día (con el contenido ya convertido), o None."""
    return _lectura(con.execute("SELECT * FROM lecturas WHERE fecha = ?", (fecha,)).fetchone())


def lecturas(con: sqlite3.Connection, limite: int = 60) -> list[dict[str, Any]]:
    """Las últimas lecturas, la más reciente primero."""
    filas = con.execute("SELECT * FROM lecturas ORDER BY fecha DESC LIMIT ?", (limite,))
    return [lec for f in filas if (lec := _lectura(f)) is not None]


def leer_producto(con: sqlite3.Connection, codigo: str) -> sqlite3.Row | None:
    """Lee la fila de un producto de la caché por su código, o None si no está."""
    return con.execute("SELECT * FROM productos WHERE codigo = ?", (codigo,)).fetchone()


def _fila_a_comida(f: sqlite3.Row) -> Comida:
    return Comida(
        id=f["id"],
        fecha=f["fecha"],
        momento=f["momento"],
        nombre=f["nombre"],
        cantidad=f["cantidad"],
        unidad=f["unidad"],
        kcal=f["kcal"],
        proteinas=f["proteinas"],
        hidratos=f["hidratos"],
        grasas=f["grasas"],
        codigo=f["codigo"],
        creado=f["creado"],
    )


def comidas(con: sqlite3.Connection, fecha: str) -> list[Comida]:
    """Lista las comidas de un día, ordenadas por identificador (orden de inserción)."""
    filas = con.execute(
        "SELECT * FROM comidas WHERE fecha = ? ORDER BY id ASC",
        (fecha,),
    ).fetchall()
    return [_fila_a_comida(f) for f in filas]


def calcular_totales(items: Iterable[Comida]) -> Totales:
    """Calcula la suma de nutrientes y las ausencias para un conjunto de comidas."""
    kcal = 0.0
    proteinas = 0.0
    hidratos = 0.0
    grasas = 0.0
    sin_kcal = 0
    sin_proteinas = 0
    sin_hidratos = 0
    sin_grasas = 0
    entradas = 0
    incompletas = 0

    for c in items:
        entradas += 1
        falta = False
        if c.kcal is not None:
            kcal += c.kcal
        else:
            sin_kcal += 1
            falta = True

        if c.proteinas is not None:
            proteinas += c.proteinas
        else:
            sin_proteinas += 1
            falta = True

        if c.hidratos is not None:
            hidratos += c.hidratos
        else:
            sin_hidratos += 1
            falta = True

        if c.grasas is not None:
            grasas += c.grasas
        else:
            sin_grasas += 1
            falta = True

        if falta:
            incompletas += 1

    return Totales(
        kcal=round(kcal, 1),
        proteinas=round(proteinas, 1),
        hidratos=round(hidratos, 1),
        grasas=round(grasas, 1),
        sin_kcal=sin_kcal,
        sin_proteinas=sin_proteinas,
        sin_hidratos=sin_hidratos,
        sin_grasas=sin_grasas,
        entradas=entradas,
        incompletas=incompletas,
    )


def totales_dia(con: sqlite3.Connection, fecha: str) -> TotalesDia:
    """Calcula los totales de comida de un día por momento y en total."""
    lista = comidas(con, fecha)
    agrupadas: dict[str, list[Comida]] = {m: [] for m in MOMENTOS}
    for c in lista:
        agrupadas.setdefault(c.momento, []).append(c)

    por_momento = {m: calcular_totales(items) for m, items in agrupadas.items()}
    total = calcular_totales(lista)
    return TotalesDia(por_momento=por_momento, total=total)


def productos_recientes(con: sqlite3.Connection, limite: int = 8) -> list[sqlite3.Row]:
    """Los últimos productos distintos (con código) usados en comidas, el más reciente primero."""
    return con.execute(
        """
        SELECT p.codigo, p.nombre, p.marca, p.unidad
        FROM comidas AS c
        JOIN productos AS p ON p.codigo = c.codigo
        WHERE c.codigo IS NOT NULL AND c.codigo != ''
        GROUP BY p.codigo, p.nombre, p.marca, p.unidad
        ORDER BY MAX(c.id) DESC
        LIMIT ?
        """,
        (limite,),
    ).fetchall()
