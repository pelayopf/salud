"""Persistencia en SQLite.

- `crudos`: la respuesta tal cual de cada fuente y día, para poder sacar
  métricas nuevas más adelante sin volver a pedírselas a Garmin.
- `metricas`: un valor por día, métrica y fuente. Si un día hay varias
  fuentes para la misma métrica, manda la primera de `PRIORIDAD`.
- `actividades`: entrenos y partidos, uno por actividad de cada fuente.
- `lecturas`: la lectura diaria hecha con Claude (una por día).
- `productos`: caché de Open Food Facts por código de barras.
- `productos_propios`: productos creados o corregidos (manual o por etiqueta con IA).
- `comidas`: diario de alimentos por fecha y momento del día.
- `ajustes_comida`: configuración de objetivos de comida (déficit, macros, reparto).
- `planificado`: actividades planificadas (Google Calendar o semana tipo).
- `titulos_calendario`: clasificación de títulos del calendario (deporte y tipo).
- `semana_tipo`: plantilla semanal propuesta a partir de los hábitos de Garmin.
- `consejos_comida`: consejos de comida propuestos por la IA.
- `pauta`: configuración de la pauta farmacológica (fármaco, dosis e intervalos).
- `dosis`: registro de dosis administradas.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

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
CREATE TABLE IF NOT EXISTS productos_propios (
    id INTEGER PRIMARY KEY,
    codigo TEXT UNIQUE,
    nombre TEXT NOT NULL,
    marca TEXT,
    unidad TEXT NOT NULL,
    kcal REAL,
    proteinas REAL,
    hidratos REAL,
    grasas REAL,
    racion_g REAL,
    racion_nombre TEXT,
    origen TEXT NOT NULL,
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS planificado (
    id INTEGER PRIMARY KEY,
    origen TEXT NOT NULL,
    uid TEXT NOT NULL,
    fecha TEXT NOT NULL,
    inicio TEXT,
    duracion_min INTEGER,
    titulo TEXT NOT NULL,
    tipo TEXT,
    intensidad TEXT NOT NULL,
    actualizado TEXT NOT NULL,
    UNIQUE (origen, uid)
);
CREATE INDEX IF NOT EXISTS planificado_fecha ON planificado (fecha);
CREATE TABLE IF NOT EXISTS titulos_calendario (
    titulo TEXT PRIMARY KEY,
    deporte INTEGER NOT NULL,
    tipo TEXT,
    actualizado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS semana_tipo (
    id INTEGER PRIMARY KEY,
    dia INTEGER NOT NULL,
    inicio TEXT,
    duracion_min INTEGER,
    tipo TEXT NOT NULL,
    intensidad TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ajustes_comida (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    deficit_kcal INTEGER NOT NULL,
    proteinas_g_kg REAL NOT NULL,
    grasas_g_kg REAL NOT NULL,
    peso_kg REAL,
    reparto TEXT NOT NULL,
    actualizado TEXT NOT NULL,
    kcal_minimo INTEGER,
    peso_objetivo_kg REAL
);
CREATE TABLE IF NOT EXISTS consejos_comida (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    creado TEXT NOT NULL,
    nota TEXT NOT NULL DEFAULT '',
    contenido TEXT NOT NULL,
    modelo TEXT NOT NULL,
    segundos REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS consejos_comida_fecha ON consejos_comida (fecha);
CREATE TABLE IF NOT EXISTS pauta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    farmaco TEXT NOT NULL,
    dosis_mg REAL NOT NULL,
    intervalo_dias INTEGER NOT NULL,
    margen_olvido_dias INTEGER NOT NULL,
    separacion_min_dias INTEGER NOT NULL,
    actualizado TEXT NOT NULL,
    dosis_previstas INTEGER
);
CREATE TABLE IF NOT EXISTS dosis (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,          -- AAAA-MM-DD, hora local
    farmaco TEXT NOT NULL,
    dosis_mg REAL NOT NULL,
    zona TEXT NOT NULL,
    nota TEXT NOT NULL DEFAULT '',
    creado TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dosis_fecha ON dosis (fecha);
CREATE TABLE IF NOT EXISTS sesiones_web (
    token_hash TEXT PRIMARY KEY,
    correo TEXT NOT NULL,
    creada TEXT NOT NULL,
    caduca TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autorizaciones_pendientes (
    state TEXT PRIMARY KEY,
    creada TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS borradores_comida (
    id INTEGER PRIMARY KEY,
    creado TEXT NOT NULL,
    fecha TEXT NOT NULL,
    momento TEXT NOT NULL,
    texto TEXT NOT NULL,
    con_foto INTEGER NOT NULL,
    estado TEXT NOT NULL,
    error TEXT,
    alimentos TEXT
);
CREATE TABLE IF NOT EXISTS uso_ia (
    id INTEGER PRIMARY KEY,
    cuando TEXT NOT NULL,
    tarea TEXT NOT NULL,
    motor TEXT NOT NULL,
    modelo TEXT NOT NULL,
    estado TEXT NOT NULL,
    segundos REAL NOT NULL,
    entrada INTEGER,
    salida INTEGER,
    razonamiento INTEGER,
    coste_usd REAL
);
CREATE INDEX IF NOT EXISTS uso_ia_cuando ON uso_ia (cuando);
CREATE TABLE IF NOT EXISTS estado_ia (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
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


MOMENTOS = ("desayuno", "media mañana", "comida", "merienda", "cena", "otro")
REPARTO_DEFECTO = {
    "desayuno": 20,
    "media mañana": 10,
    "comida": 35,
    "merienda": 10,
    "cena": 25,
}


@dataclass(frozen=True)
class AjustesComida:
    """Configuración de objetivos nutricionales y reparto calórico diario."""

    deficit_kcal: int = 250
    proteinas_g_kg: float = 1.6
    grasas_g_kg: float = 0.9
    peso_kg: float | None = None
    reparto: dict[str, int] = field(default_factory=lambda: dict(REPARTO_DEFECTO))
    kcal_minimo: int | None = None
    peso_objetivo_kg: float | None = None


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


PREPARANDO_MAX_S = 600


@dataclass(frozen=True)
class BorradorComida:
    """Borrador de comida analizada con IA en proceso o lista para revisar."""

    id: int
    creado: str
    fecha: str
    momento: str
    texto: str
    con_foto: bool
    estado: str  # 'preparando' | 'listo' | 'error'
    error: str | None = None
    alimentos: list[dict[str, Any]] = field(default_factory=list)


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


@dataclass(frozen=True)
class Planificado:
    """Una actividad deportiva planificada para un día."""

    fecha: str
    inicio: str | None
    duracion_min: int | None
    titulo: str
    tipo: str | None
    intensidad: str
    origen: str  # "google" o "semana-tipo"
    uid: str = ""


@dataclass(frozen=True)
class EntradaSemana:
    """Una sesión habitual en la plantilla de semana tipo."""

    dia: int
    inicio: str | None
    duracion_min: int | None
    tipo: str
    intensidad: str


@dataclass(frozen=True)
class Pauta:
    """Configuración de la pauta de tratamiento (fármaco, dosis e intervalos)."""

    farmaco: str = "Mounjaro"
    dosis_mg: float = 5.0
    intervalo_dias: int = 7
    margen_olvido_dias: int = 4
    separacion_min_dias: int = 3
    dosis_previstas: int | None = None


@dataclass(frozen=True)
class Dosis:
    """Una dosis de tratamiento administrada."""

    id: int
    fecha: str
    farmaco: str
    dosis_mg: float
    zona: str
    nota: str = ""
    creado: str = ""


def normalizar_titulo(texto: str) -> str:
    """Normaliza un texto: minúsculas, sin tildes y espacios colapsados."""
    limpio = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in limpio if not unicodedata.combining(c))
    return " ".join(sin_tildes.lower().split())


def ahora() -> str:
    """Marca de tiempo en UTC, con segundos."""
    return datetime.now(UTC).isoformat(timespec="seconds")


ZONA = ZoneInfo("Europe/Madrid")


def hora_local(momento_utc: str | None) -> str:
    """Convierte una marca de tiempo UTC a hora local de Madrid HH:MM."""
    if not momento_utc:
        return ""
    try:
        dt = datetime.fromisoformat(momento_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ZONA).strftime("%H:%M")
    except Exception:
        return ""


def fecha_local(momento_utc: str | None) -> str:
    """Convierte una marca de tiempo UTC a fecha local de Madrid YYYY-MM-DD."""
    if not momento_utc:
        return ""
    if len(momento_utc) == 10 and momento_utc[4] == "-" and momento_utc[7] == "-":
        return momento_utc
    try:
        dt = datetime.fromisoformat(momento_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ZONA).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _añadir_columnas(con: sqlite3.Connection, tabla: str, columnas: dict[str, str]) -> None:
    """Añade columnas a una tabla si no existen (migración idempotente)."""
    filas = con.execute(f"PRAGMA table_info({tabla})").fetchall()
    existentes = {f["name"] for f in filas}
    for col, tipo in columnas.items():
        if col not in existentes:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")


def conectar(ruta: Path | str) -> sqlite3.Connection:
    """Abre (creando si hace falta) la base de datos y aplica el esquema."""
    if str(ruta) != ":memory:":
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    con.create_function("fecha_local_madrid", 1, fecha_local)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    _añadir_columnas(con, "productos_propios", {"racion_g": "REAL", "racion_nombre": "TEXT"})
    _añadir_columnas(con, "ajustes_comida", {"kcal_minimo": "INTEGER", "peso_objetivo_kg": "REAL"})
    _añadir_columnas(con, "pauta", {"dosis_previstas": "INTEGER"})
    return con


# --- Escritura ---------------------------------------------------------------


def registrar_uso_ia(
    con: sqlite3.Connection,
    *,
    tarea: str,
    motor: str,
    modelo: str,
    estado: str,
    segundos: float,
    uso: tuple[int, int, int] | None,
    coste_usd: float | None,
    cuando: str | None = None,
) -> int:
    """Registra una llamada (o intento) a un motor de IA."""
    momento = cuando or ahora()
    entrada, salida, razonamiento = uso if uso is not None else (None, None, None)
    with con:
        cur = con.execute(
            "INSERT INTO uso_ia ("
            " cuando, tarea, motor, modelo, estado, segundos,"
            " entrada, salida, razonamiento, coste_usd"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                momento,
                tarea,
                motor,
                modelo,
                estado,
                segundos,
                entrada,
                salida,
                razonamiento,
                coste_usd,
            ),
        )
    return int(cur.lastrowid or 0)


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
    *,
    codigo: str,
    nombre: str | None,
    marca: str | None,
    nutriscore: str | None,
    unidad: str,
    kcal: float | None,
    proteinas: float | None,
    hidratos: float | None,
    grasas: float | None,
    datos: dict[str, Any],
    consultado: str | None = None,
) -> None:
    """Guarda (o actualiza) un producto en la caché de Open Food Facts."""
    momento = consultado or ahora()
    datos_json = json.dumps(datos, ensure_ascii=False)

    with con:
        con.execute(
            "INSERT OR REPLACE INTO productos"
            " (codigo, nombre, marca, nutriscore, unidad, kcal, proteinas, hidratos,"
            " grasas, datos, consultado)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                codigo,
                nombre,
                marca,
                nutriscore,
                unidad,
                kcal,
                proteinas,
                hidratos,
                grasas,
                datos_json,
                momento,
            ),
        )


ORIGENES_PROPIOS = ("manual", "etiqueta-ia", "comida-ia", "ciqual")


def guardar_producto_propio(
    con: sqlite3.Connection,
    *,
    id_: int | None,
    codigo: str | None,
    nombre: str,
    marca: str | None,
    unidad: str,
    kcal: float | None,
    proteinas: float | None,
    hidratos: float | None,
    grasas: float | None,
    origen: str,
    racion_g: float | None = None,
    racion_nombre: str | None = None,
) -> int:
    """Guarda o actualiza un producto propio o corregido."""
    if origen not in ORIGENES_PROPIOS:
        raise ValueError(f"origen no válido: {origen!r} (debe ser uno de {ORIGENES_PROPIOS})")

    momento = ahora()
    with con:
        if id_ is not None:
            cur = con.execute(
                "UPDATE productos_propios SET"
                " codigo = ?, nombre = ?, marca = ?, unidad = ?,"
                " kcal = ?, proteinas = ?, hidratos = ?, grasas = ?,"
                " racion_g = ?, racion_nombre = ?,"
                " origen = ?, actualizado = ?"
                " WHERE id = ?",
                (
                    codigo,
                    nombre,
                    marca,
                    unidad,
                    kcal,
                    proteinas,
                    hidratos,
                    grasas,
                    racion_g,
                    racion_nombre,
                    origen,
                    momento,
                    id_,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError("Producto no encontrado")
            return id_

        if codigo is not None:
            existente = con.execute(
                "SELECT id FROM productos_propios WHERE codigo = ?", (codigo,)
            ).fetchone()
            if existente is not None:
                existente_id = int(existente["id"])
                con.execute(
                    "UPDATE productos_propios SET"
                    " nombre = ?, marca = ?, unidad = ?,"
                    " kcal = ?, proteinas = ?, hidratos = ?, grasas = ?,"
                    " racion_g = ?, racion_nombre = ?,"
                    " origen = ?, actualizado = ?"
                    " WHERE id = ?",
                    (
                        nombre,
                        marca,
                        unidad,
                        kcal,
                        proteinas,
                        hidratos,
                        grasas,
                        racion_g,
                        racion_nombre,
                        origen,
                        momento,
                        existente_id,
                    ),
                )
                return existente_id

        cur = con.execute(
            "INSERT INTO productos_propios"
            " (codigo, nombre, marca, unidad, kcal, proteinas, hidratos,"
            " grasas, racion_g, racion_nombre, origen, creado, actualizado)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                codigo,
                nombre,
                marca,
                unidad,
                kcal,
                proteinas,
                hidratos,
                grasas,
                racion_g,
                racion_nombre,
                origen,
                momento,
                momento,
            ),
        )
        return int(cur.lastrowid or 0)


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


def guardar_ajustes_comida(con: sqlite3.Connection, ajustes: AjustesComida) -> None:
    """Guarda (upsert id=1) la configuración de objetivos de comida."""
    momento = ahora()
    reparto_json = json.dumps(ajustes.reparto, ensure_ascii=False)
    with con:
        con.execute(
            """
            INSERT INTO ajustes_comida (
                id, deficit_kcal, proteinas_g_kg, grasas_g_kg, peso_kg, reparto,
                actualizado, kcal_minimo, peso_objetivo_kg
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                deficit_kcal = excluded.deficit_kcal,
                proteinas_g_kg = excluded.proteinas_g_kg,
                grasas_g_kg = excluded.grasas_g_kg,
                peso_kg = excluded.peso_kg,
                reparto = excluded.reparto,
                actualizado = excluded.actualizado,
                kcal_minimo = excluded.kcal_minimo,
                peso_objetivo_kg = excluded.peso_objetivo_kg
            """,
            (
                ajustes.deficit_kcal,
                ajustes.proteinas_g_kg,
                ajustes.grasas_g_kg,
                ajustes.peso_kg,
                reparto_json,
                momento,
                ajustes.kcal_minimo,
                ajustes.peso_objetivo_kg,
            ),
        )


def guardar_consejo(
    con: sqlite3.Connection,
    *,
    fecha: str,
    nota: str,
    contenido: dict[str, Any],
    modelo: str,
    segundos: float,
) -> int:
    """Guarda un consejo de comida generado por la IA."""
    momento = ahora()
    with con:
        cur = con.execute(
            """
            INSERT INTO consejos_comida (fecha, creado, nota, contenido, modelo, segundos)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fecha, momento, nota, json.dumps(contenido, ensure_ascii=False), modelo, segundos),
        )
        return int(cur.lastrowid or 0)


def crear_borrador_comida(
    con: sqlite3.Connection,
    fecha: str,
    momento: str,
    texto: str,
    con_foto: bool,
) -> int:
    """Crea un borrador en estado 'preparando' tras borrar los de más de 2 días."""
    limite = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM borradores_comida WHERE creado < ?", (limite,))
        cur = con.execute(
            """
            INSERT INTO borradores_comida (creado, fecha, momento, texto, con_foto, estado)
            VALUES (?, ?, ?, ?, ?, 'preparando')
            """,
            (ahora(), fecha, momento, texto, 1 if con_foto else 0),
        )
        return int(cur.lastrowid or 0)


def completar_borrador_comida(
    con: sqlite3.Connection,
    id_: int,
    alimentos: list[dict[str, Any]],
) -> None:
    """Marca un borrador como 'listo' y guarda sus alimentos propuestos en JSON."""
    alimentos_json = json.dumps(alimentos, ensure_ascii=False)
    with con:
        con.execute(
            """
            UPDATE borradores_comida
            SET estado = 'listo', alimentos = ?, error = NULL
            WHERE id = ?
            """,
            (alimentos_json, id_),
        )


def fallar_borrador_comida(con: sqlite3.Connection, id_: int, error: str) -> None:
    """Marca un borrador como 'error' con el mensaje correspondiente."""
    with con:
        con.execute(
            """
            UPDATE borradores_comida
            SET estado = 'error', error = ?
            WHERE id = ?
            """,
            (error, id_),
        )


def leer_borrador_comida(
    con: sqlite3.Connection,
    id_: int,
    ahora: datetime | None = None,
) -> BorradorComida | None:
    """Lee un borrador de comida. Si lleva más de 600 s preparando, lo devuelve como error."""
    fila = con.execute(
        "SELECT id, creado, fecha, momento, texto, con_foto, estado, error, alimentos "
        "FROM borradores_comida WHERE id = ?",
        (id_,),
    ).fetchone()
    if not fila:
        return None

    estado = fila["estado"]
    error = fila["error"]
    if estado == "preparando":
        dt_creado = datetime.fromisoformat(fila["creado"])
        if dt_creado.tzinfo is None:
            dt_creado = dt_creado.replace(tzinfo=UTC)
        ref = ahora if ahora is not None else datetime.now(UTC)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)
        if (ref - dt_creado).total_seconds() > PREPARANDO_MAX_S:
            estado = "error"
            error = "Se interrumpió el análisis; vuelve a intentarlo"

    alimentos_raw = fila["alimentos"]
    try:
        alimentos = json.loads(alimentos_raw) if alimentos_raw else []
    except (json.JSONDecodeError, TypeError):
        alimentos = []

    return BorradorComida(
        id=int(fila["id"]),
        creado=fila["creado"],
        fecha=fila["fecha"],
        momento=fila["momento"],
        texto=fila["texto"],
        con_foto=bool(fila["con_foto"]),
        estado=estado,
        error=error,
        alimentos=alimentos,
    )


def borrar_borrador_comida(con: sqlite3.Connection, id_: int) -> bool:
    """Borra un borrador por su identificador. Devuelve True si existía."""
    with con:
        cur = con.execute("DELETE FROM borradores_comida WHERE id = ?", (id_,))
        return cur.rowcount > 0


def guardar_estado_ia(con: sqlite3.Connection, clave: str, valor: str) -> None:
    """Guarda o actualiza un valor de estado de la IA con la marca de tiempo actual."""
    with con:
        con.execute(
            "INSERT INTO estado_ia (clave, valor, actualizado)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(clave) DO UPDATE SET"
            " valor = excluded.valor, actualizado = excluded.actualizado",
            (clave, valor, ahora()),
        )


def leer_estado_ia(con: sqlite3.Connection, clave: str) -> str | None:
    """Lee el valor de una clave en estado_ia, o None si no existe."""
    fila = con.execute("SELECT valor FROM estado_ia WHERE clave = ?", (clave,)).fetchone()
    return str(fila["valor"]) if fila is not None else None


# --- Lectura -----------------------------------------------------------------


def gasto_vertex_usd(con: sqlite3.Connection, desde: str, hasta: str) -> float:
    """Coste estimado en dólares de las llamadas a Vertex entre dos fechas locales (incluidas)."""
    try:
        con.create_function("fecha_local_madrid", 1, fecha_local)
    except sqlite3.OperationalError:
        pass
    fila = con.execute(
        "SELECT COALESCE(SUM(coste_usd), 0.0) FROM uso_ia"
        " WHERE motor = 'vertex' AND fecha_local_madrid(cuando) BETWEEN ? AND ?",
        (desde, hasta),
    ).fetchone()
    return float(fila[0])


def uso_ia_resumen(con: sqlite3.Connection, desde: str, hasta: str) -> list[sqlite3.Row]:
    """Resumen de uso de la IA agrupado por día (fecha local Madrid), motor y modelo.

    Suma llamadas, tokens y coste para el rango de fechas [desde, hasta].
    """
    try:
        con.create_function("fecha_local_madrid", 1, fecha_local)
    except sqlite3.OperationalError:
        pass
    return list(
        con.execute(
            """
            SELECT
                fecha_local_madrid(cuando) AS fecha,
                motor,
                modelo,
                COUNT(*) AS llamadas,
                COALESCE(SUM(entrada), 0) AS entrada,
                COALESCE(SUM(salida), 0) AS salida,
                COALESCE(SUM(razonamiento), 0) AS razonamiento,
                COALESCE(SUM(coste_usd), 0.0) AS coste_usd,
                SUM(CASE WHEN estado != 'ok' THEN 1 ELSE 0 END) AS fallos
            FROM uso_ia
            WHERE fecha_local_madrid(cuando) BETWEEN ? AND ?
            GROUP BY fecha_local_madrid(cuando), motor, modelo
            ORDER BY fecha_local_madrid(cuando) ASC, motor ASC, modelo ASC
            """,
            (desde, hasta),
        ).fetchall()
    )


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


def leer_producto_propio(con: sqlite3.Connection, id_: int) -> sqlite3.Row | None:
    """Lee un producto propio por su identificador, o None si no existe."""
    return con.execute("SELECT * FROM productos_propios WHERE id = ?", (id_,)).fetchone()


def producto_propio_por_codigo(con: sqlite3.Connection, codigo: str) -> sqlite3.Row | None:
    """Lee un producto propio por su código de barras, o None si no existe."""
    return con.execute("SELECT * FROM productos_propios WHERE codigo = ?", (codigo,)).fetchone()


def productos_propios(con: sqlite3.Connection, limite: int = 50) -> list[sqlite3.Row]:
    """Lista los productos propios ordenados por fecha de actualización descendente."""
    return con.execute(
        "SELECT * FROM productos_propios ORDER BY actualizado DESC LIMIT ?", (limite,)
    ).fetchall()


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


def leer_comida(con: sqlite3.Connection, id_: int) -> Comida | None:
    """Lee una entrada del diario de comidas por su identificador, o None si no existe."""
    fila = con.execute("SELECT * FROM comidas WHERE id = ?", (id_,)).fetchone()
    return _fila_a_comida(fila) if fila is not None else None


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


def palabras(texto: str) -> list[str]:
    """Palabras del texto normalizado con `normalizar_titulo` (sin tildes ni mayúsculas)."""
    return [p for p in re.split(r"[^\w]+", normalizar_titulo(texto)) if p]


def palabras_buscadas(texto: str) -> list[str]:
    """Palabras de una búsqueda: las de 2 letras o más."""
    return [p for p in palabras(texto) if len(p) >= 2]


def encaja(buscadas: list[str], *textos: str | None) -> bool:
    """Si cada palabra buscada es prefijo de alguna palabra de los textos."""
    tokens = palabras(" ".join(t for t in textos if t))
    return all(any(token.startswith(b) for token in tokens) for b in buscadas)


def buscar_productos(con: sqlite3.Connection, texto: str, limite: int = 10) -> list[dict[str, Any]]:
    """Busca productos propios y de la caché de Open Food Facts por nombre o marca."""
    if limite <= 0:
        return []
    palabras_busqueda = palabras_buscadas(texto)
    if not palabras_busqueda:
        return []

    resultados: list[dict[str, Any]] = []
    codigos_vistos: set[str] = set()

    filas_propios = con.execute(
        "SELECT id, codigo, nombre, marca, kcal FROM productos_propios ORDER BY actualizado DESC"
    ).fetchall()
    for f in filas_propios:
        if encaja(palabras_busqueda, f["nombre"], f["marca"]):
            codigo = f["codigo"]
            if codigo:
                codigos_vistos.add(codigo)
            resultados.append(
                {
                    "tipo": "propio",
                    "id": f["id"],
                    "codigo": codigo,
                    "nombre": f["nombre"],
                    "marca": f["marca"],
                    "kcal": f["kcal"],
                }
            )
            if len(resultados) >= limite:
                return resultados

    filas_off = con.execute(
        "SELECT codigo, nombre, marca, kcal FROM productos ORDER BY consultado DESC"
    ).fetchall()
    for f in filas_off:
        codigo = f["codigo"]
        if codigo in codigos_vistos:
            continue
        if encaja(palabras_busqueda, f["nombre"], f["marca"]):
            codigos_vistos.add(codigo)
            resultados.append(
                {
                    "tipo": "off",
                    "id": None,
                    "codigo": codigo,
                    "nombre": f["nombre"],
                    "marca": f["marca"],
                    "kcal": f["kcal"],
                }
            )
            if len(resultados) >= limite:
                return resultados

    return resultados


def comidas_anteriores(
    con: sqlite3.Connection, texto: str, hasta: str, limite: int = 8
) -> list[Comida]:
    """Comidas sin código de los últimos 120 días hasta 'hasta' que encajen por nombre."""
    if limite <= 0:
        return []
    palabras_busqueda = palabras_buscadas(texto)
    if not palabras_busqueda:
        return []

    try:
        hasta_dt = date.fromisoformat(hasta)
    except ValueError:
        return []

    desde_str = (hasta_dt - timedelta(days=120)).isoformat()
    filas = con.execute(
        """
        SELECT * FROM comidas
        WHERE (codigo IS NULL OR codigo = '') AND fecha >= ? AND fecha <= ?
        ORDER BY fecha DESC, id DESC
        """,
        (desde_str, hasta),
    ).fetchall()

    vistos_nombres: set[str] = set()
    resultados: list[Comida] = []
    for f in filas:
        c = _fila_a_comida(f)
        norm = normalizar_titulo(c.nombre)
        if norm in vistos_nombres:
            continue
        if encaja(palabras_busqueda, c.nombre):
            vistos_nombres.add(norm)
            resultados.append(c)
            if len(resultados) >= limite:
                break

    return resultados


def ajustes_comida(con: sqlite3.Connection) -> AjustesComida:
    """Configuración de objetivos de comida o los valores por defecto si no hay fila."""
    fila = con.execute("SELECT * FROM ajustes_comida WHERE id = 1").fetchone()
    if fila is None:
        return AjustesComida()
    try:
        reparto = json.loads(fila["reparto"])
        if not isinstance(reparto, dict):
            reparto = dict(REPARTO_DEFECTO)
        else:
            reparto = {str(k): int(v) for k, v in reparto.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        reparto = dict(REPARTO_DEFECTO)
    kcal_min = (
        int(fila["kcal_minimo"])
        if "kcal_minimo" in fila.keys() and fila["kcal_minimo"] is not None
        else None
    )
    peso_obj = (
        float(fila["peso_objetivo_kg"])
        if "peso_objetivo_kg" in fila.keys() and fila["peso_objetivo_kg"] is not None
        else None
    )
    return AjustesComida(
        deficit_kcal=int(fila["deficit_kcal"]),
        proteinas_g_kg=float(fila["proteinas_g_kg"]),
        grasas_g_kg=float(fila["grasas_g_kg"]),
        peso_kg=float(fila["peso_kg"]) if fila["peso_kg"] is not None else None,
        reparto=reparto,
        kcal_minimo=kcal_min,
        peso_objetivo_kg=peso_obj,
    )


def ultimo_consejo(con: sqlite3.Connection, fecha: str) -> dict[str, Any] | None:
    """El consejo más reciente de ese día.

    Formato: {"creado": ..., "nota": ..., "modelo": ..., **contenido}.
    """
    fila = con.execute(
        "SELECT creado, nota, contenido, modelo FROM consejos_comida WHERE fecha = ?"
        " ORDER BY id DESC LIMIT 1",
        (fecha,),
    ).fetchone()
    if fila is None:
        return None
    contenido = json.loads(fila["contenido"]) if fila["contenido"] else {}
    return {
        "creado": fila["creado"],
        "nota": fila["nota"],
        "modelo": fila["modelo"],
        **contenido,
    }


# --- Calendario y planificación ----------------------------------------------


def reemplazar_planificado(
    con: sqlite3.Connection,
    *,
    origen: str,
    desde: str,
    hasta: str,
    filas: Iterable[Planificado],
) -> int:
    """Reemplaza las entradas planificadas de un origen entre dos fechas (ambas incluidas)."""
    with con:
        con.execute(
            "DELETE FROM planificado WHERE origen = ? AND fecha >= ? AND fecha <= ?",
            (origen, desde, hasta),
        )
        n = 0
        now = ahora()
        for f in filas:
            con.execute(
                """
                INSERT OR REPLACE INTO planificado (
                    origen, uid, fecha, inicio, duracion_min, titulo, tipo, intensidad, actualizado
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f.origen,
                    f.uid,
                    f.fecha,
                    f.inicio,
                    f.duracion_min,
                    f.titulo,
                    f.tipo,
                    f.intensidad,
                    now,
                ),
            )
            n += 1
        return n


def planificado(con: sqlite3.Connection, desde: str, hasta: str) -> list[Planificado]:
    """Entradas planificadas entre dos fechas, ordenadas por fecha e inicio (NULL primero)."""
    filas = con.execute(
        """
        SELECT fecha, inicio, duracion_min, titulo, tipo, intensidad, origen, uid
        FROM planificado
        WHERE fecha >= ? AND fecha <= ?
        ORDER BY fecha ASC, CASE WHEN inicio IS NULL THEN 0 ELSE 1 END, inicio ASC, id ASC
        """,
        (desde, hasta),
    ).fetchall()
    return [
        Planificado(
            fecha=f["fecha"],
            inicio=f["inicio"],
            duracion_min=f["duracion_min"],
            titulo=f["titulo"],
            tipo=f["tipo"],
            intensidad=f["intensidad"],
            origen=f["origen"],
            uid=f["uid"],
        )
        for f in filas
    ]


def borrar_planificado_titulo(con: sqlite3.Connection, titulo: str) -> int:
    """Borra entradas de origen 'google' cuyo título normalizado coincida con el dado."""
    objetivo = normalizar_titulo(titulo)
    filas = con.execute("SELECT id, titulo FROM planificado WHERE origen = 'google'").fetchall()
    ids_a_borrar = [f["id"] for f in filas if normalizar_titulo(f["titulo"]) == objetivo]
    if not ids_a_borrar:
        return 0
    with con:
        con.executemany("DELETE FROM planificado WHERE id = ?", [(i,) for i in ids_a_borrar])
    return len(ids_a_borrar)


def actualizar_tipo_titulo(
    con: sqlite3.Connection, *, titulo: str, tipo: str, intensidad: str
) -> int:
    """Pone tipo e intensidad a las filas de origen 'google' cuyo título normalizado coincide."""
    objetivo = normalizar_titulo(titulo)
    filas = con.execute("SELECT id, titulo FROM planificado WHERE origen = 'google'").fetchall()
    ids_a_actualizar = [f["id"] for f in filas if normalizar_titulo(f["titulo"]) == objetivo]
    if not ids_a_actualizar:
        return 0
    now = ahora()
    with con:
        con.executemany(
            "UPDATE planificado SET tipo = ?, intensidad = ?, actualizado = ? WHERE id = ?",
            [(tipo, intensidad, now, i) for i in ids_a_actualizar],
        )
    return len(ids_a_actualizar)


def titulos_calendario(con: sqlite3.Connection) -> dict[str, tuple[bool, str | None]]:
    """Diccionario de títulos del calendario clasificados (deporte, tipo de Garmin)."""
    filas = con.execute("SELECT titulo, deporte, tipo FROM titulos_calendario").fetchall()
    return {f["titulo"]: (bool(f["deporte"]), f["tipo"]) for f in filas}


def marcar_titulo(
    con: sqlite3.Connection,
    *,
    titulo: str,
    deporte: bool,
    tipo: str | None,
) -> None:
    """Registra o actualiza la clasificación de un título de calendario (normalizado)."""
    t_norm = normalizar_titulo(titulo)
    with con:
        con.execute(
            """
            INSERT INTO titulos_calendario (titulo, deporte, tipo, actualizado)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(titulo) DO UPDATE SET
                deporte = excluded.deporte,
                tipo = excluded.tipo,
                actualizado = excluded.actualizado
            """,
            (t_norm, int(deporte), tipo, ahora()),
        )


def semana_tipo(con: sqlite3.Connection) -> list[EntradaSemana]:
    """Entradas de la semana tipo, ordenadas por día e inicio (NULL primero)."""
    filas = con.execute(
        """
        SELECT dia, inicio, duracion_min, tipo, intensidad
        FROM semana_tipo
        ORDER BY dia ASC, CASE WHEN inicio IS NULL THEN 0 ELSE 1 END, inicio ASC, id ASC
        """
    ).fetchall()
    return [
        EntradaSemana(
            dia=f["dia"],
            inicio=f["inicio"],
            duracion_min=f["duracion_min"],
            tipo=f["tipo"],
            intensidad=f["intensidad"],
        )
        for f in filas
    ]


def guardar_semana_tipo(con: sqlite3.Connection, entradas: Iterable[EntradaSemana]) -> int:
    """Reemplaza toda la tabla de la semana tipo en una sola transacción."""
    with con:
        con.execute("DELETE FROM semana_tipo")
        now = ahora()
        n = 0
        for e in entradas:
            con.execute(
                """
                INSERT INTO semana_tipo (dia, inicio, duracion_min, tipo, intensidad, actualizado)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (e.dia, e.inicio, e.duracion_min, e.tipo, e.intensidad, now),
            )
            n += 1
        return n


# --- Tratamiento -------------------------------------------------------------


def pauta(con: sqlite3.Connection) -> Pauta:
    """Pauta de tratamiento o los valores por defecto si no hay fila."""
    fila = con.execute("SELECT * FROM pauta WHERE id = 1").fetchone()
    if fila is None:
        return Pauta()
    dosis_prev = (
        int(fila["dosis_previstas"])
        if "dosis_previstas" in fila.keys() and fila["dosis_previstas"] is not None
        else None
    )
    return Pauta(
        farmaco=str(fila["farmaco"]),
        dosis_mg=float(fila["dosis_mg"]),
        intervalo_dias=int(fila["intervalo_dias"]),
        margen_olvido_dias=int(fila["margen_olvido_dias"]),
        separacion_min_dias=int(fila["separacion_min_dias"]),
        dosis_previstas=dosis_prev,
    )


def hay_pauta(con: sqlite3.Connection) -> bool:
    """Si se ha guardado una pauta (si no, `pauta` da la de por defecto)."""
    return con.execute("SELECT 1 FROM pauta WHERE id = 1").fetchone() is not None


def guardar_pauta(con: sqlite3.Connection, p: Pauta) -> None:
    """Guarda (upsert id=1) la pauta de tratamiento."""
    momento = ahora()
    with con:
        con.execute(
            """
            INSERT INTO pauta (
                id, farmaco, dosis_mg, intervalo_dias, margen_olvido_dias,
                separacion_min_dias, actualizado, dosis_previstas
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                farmaco = excluded.farmaco,
                dosis_mg = excluded.dosis_mg,
                intervalo_dias = excluded.intervalo_dias,
                margen_olvido_dias = excluded.margen_olvido_dias,
                separacion_min_dias = excluded.separacion_min_dias,
                actualizado = excluded.actualizado,
                dosis_previstas = excluded.dosis_previstas
            """,
            (
                p.farmaco,
                p.dosis_mg,
                p.intervalo_dias,
                p.margen_olvido_dias,
                p.separacion_min_dias,
                momento,
                p.dosis_previstas,
            ),
        )


def contar_dosis(
    con: sqlite3.Connection,
    *,
    farmaco: str | None = None,
    dosis_mg: float | None = None,
) -> int:
    """Cuenta las dosis registradas, opcionalmente filtrando por fármaco y dosis_mg."""
    where: list[str] = []
    params: list[Any] = []
    if farmaco is not None:
        where.append("farmaco = ?")
        params.append(farmaco)
    if dosis_mg is not None:
        where.append("dosis_mg = ?")
        params.append(dosis_mg)
    query = "SELECT COUNT(*) FROM dosis"
    if where:
        query += " WHERE " + " AND ".join(where)
    return int(con.execute(query, params).fetchone()[0])


def anadir_dosis(
    con: sqlite3.Connection,
    *,
    fecha: str,
    farmaco: str,
    dosis_mg: float,
    zona: str,
    nota: str = "",
) -> int:
    """Añade una dosis administrada. Devuelve el identificador generado."""
    momento = ahora()
    with con:
        cur = con.execute(
            """
            INSERT INTO dosis (fecha, farmaco, dosis_mg, zona, nota, creado)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fecha, farmaco, dosis_mg, zona, nota, momento),
        )
        return int(cur.lastrowid or 0)


def dosis(con: sqlite3.Connection, limite: int = 100) -> list[Dosis]:
    """Lista las dosis registradas, la más reciente primero."""
    filas = con.execute(
        """
        SELECT id, fecha, farmaco, dosis_mg, zona, nota, creado
        FROM dosis
        ORDER BY fecha DESC, id DESC
        LIMIT ?
        """,
        (limite,),
    ).fetchall()
    return [
        Dosis(
            id=f["id"],
            fecha=f["fecha"],
            farmaco=f["farmaco"],
            dosis_mg=float(f["dosis_mg"]),
            zona=f["zona"],
            nota=f["nota"],
            creado=f["creado"],
        )
        for f in filas
    ]


def borrar_dosis(con: sqlite3.Connection, id_: int) -> bool:
    """Borra una dosis por su identificador. Devuelve True si existía."""
    with con:
        cur = con.execute("DELETE FROM dosis WHERE id = ?", (id_,))
        return cur.rowcount > 0


# --- Sesiones de la web pública (ver acceso.py) ---------------------------

DIAS_SESION = 90
DIAS_RENOVACION = 30
HORAS_AUTORIZACION = 1


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def crear_sesion_web(con: sqlite3.Connection, correo: str, dias: int = DIAS_SESION) -> str:
    """Abre una sesión para un correo y devuelve su token (solo se guarda su hash)."""
    token = secrets.token_urlsafe(32)
    caduca = (datetime.now(UTC) + timedelta(days=dias)).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM sesiones_web WHERE caduca < ?", (ahora(),))
        con.execute(
            "INSERT INTO sesiones_web (token_hash, correo, creada, caduca) VALUES (?, ?, ?, ?)",
            (_hash_token(token), correo, ahora(), caduca),
        )
    return token


def correo_de_sesion_web(con: sqlite3.Connection, token: str) -> str | None:
    """El correo de una sesión vigente (None si no existe o caducó).

    Si a la sesión le quedan menos de DIAS_RENOVACION (30 días), amplía
    su caducidad a DIAS_SESION (90 días) desde hoy manteniendo el token.
    """
    th = _hash_token(token)
    fila = con.execute(
        "SELECT correo, caduca FROM sesiones_web WHERE token_hash = ? AND caduca >= ?",
        (th, ahora()),
    ).fetchone()
    if not fila:
        return None
    correo = fila["correo"]
    caduca_str = fila["caduca"]
    limite_renovacion = (datetime.now(UTC) + timedelta(days=DIAS_RENOVACION)).isoformat(
        timespec="seconds"
    )
    if caduca_str < limite_renovacion:
        nueva_caduca = (datetime.now(UTC) + timedelta(days=DIAS_SESION)).isoformat(
            timespec="seconds"
        )
        with con:
            con.execute(
                "UPDATE sesiones_web SET caduca = ? WHERE token_hash = ?",
                (nueva_caduca, th),
            )
    return str(correo)


def cerrar_sesion_web(con: sqlite3.Connection, token: str) -> None:
    """Cierra una sesión."""
    with con:
        con.execute("DELETE FROM sesiones_web WHERE token_hash = ?", (_hash_token(token),))


def guardar_autorizacion(con: sqlite3.Connection, state: str) -> None:
    """Recuerda el `state` de una autorización iniciada y olvida las abandonadas."""
    limite = (datetime.now(UTC) - timedelta(hours=HORAS_AUTORIZACION)).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM autorizaciones_pendientes WHERE creada < ?", (limite,))
        con.execute(
            "INSERT INTO autorizaciones_pendientes (state, creada) VALUES (?, ?)", (state, ahora())
        )


def consumir_autorizacion(con: sqlite3.Connection, state: str) -> bool:
    """Borra el `state` y devuelve si existía (protege el callback de peticiones ajenas)."""
    with con:
        cur = con.execute("DELETE FROM autorizaciones_pendientes WHERE state = ?", (state,))
    return cur.rowcount == 1
