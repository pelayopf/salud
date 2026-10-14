"""Alimentos genéricos de la tabla CIQUAL 2020 (ANSES).

CIQUAL (Centre d'Information sur la Qualité des Aliments) es la base de datos
francesa de composición nutricional de alimentos elaborada por la ANSES
(Agence nationale de sécurité sanitaire de l'alimentation, de l'environnement
et du travail).

Se distribuye bajo la licencia abierta «Licence Ouverte / Open Licence» de
Etalab (Francia), que permite su reproducción, redistribución y adaptación
citando obligatoriamente la fuente:

    CIQUAL 2020 (ANSES), Licence Ouverte

Este módulo permite parsear los XML originales de CIQUAL, traducir los nombres
al castellano de España por lotes usando la IA con caché persistente, generar
el fichero CSV de distribución del paquete y consultar o buscar alimentos
genéricos por texto o código.
"""

from __future__ import annotations

import csv
import functools
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ia
from .config import Settings
from .db import normalizar_titulo

FUENTE = "CIQUAL 2020 (ANSES), Licence Ouverte"
CSV = Path(__file__).resolve().parent / "datos" / "ciqual_2020_es.csv"

# Códigos de constituyentes en CIQUAL:
# 328: Energie, Règlement UE (kcal/100 g)
# 333: Energie Jones (alternativa si falta 328)
# 25000: Protéines (g/100 g)
# 31000: Glucides (g/100 g)
# 40000: Lipides (g/100 g)
# 34100: Fibres alimentaires (g/100 g)
# 32000: Sucres (g/100 g)
# 10004: Sel chlorure de sodium (g/100 g)
CONST_INTERES = {"328", "333", "25000", "31000", "40000", "34100", "32000", "10004"}

ESQUEMA_TRADUCCION: dict[str, Any] = {
    "type": "object",
    "properties": {
        "traducciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "es": {"type": "string"},
                },
                "required": ["original", "es"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["traducciones"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Generico:
    """Alimento genérico con su información nutricional por 100 g."""

    codigo: str
    nombre: str  # en castellano
    nombre_original: str  # el inglés de CIQUAL
    grupo: str  # en castellano
    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None
    fibra: float | None
    azucares: float | None
    sal: float | None


def valor_teneur(texto: str | None) -> float | None:
    """Convierte un valor de teneur de CIQUAL a float o None.

    Maneja comas decimales ('3,6'), 'traces' (0.0), rangos menores ('< 0,5' -> 0.5),
    guiones ('-') y cadenas vacías o None.
    """
    if texto is None:
        return None
    t = texto.strip()
    if not t or t == "-":
        return None
    if t.lower() == "traces":
        return 0.0
    if t.startswith("<"):
        t = t.lstrip("<").strip()
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


# Los XML de CIQUAL 2020 no están bien formados: hay textos con "<" y "&" sin
# escapar ("Panaché préemballé (<1° alc.)", "< 0,5", "Fruits & légumes"). Se
# escapan antes de leerlos.
_MENOR_SUELTO = re.compile(r"<(?![A-Za-z_/?!])")
_AMP_SUELTO = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _raiz(fichero: Path) -> ET.Element:
    """Lee un XML de CIQUAL (windows-1252) tolerando los "<" sin escapar."""
    texto = fichero.read_bytes().decode("windows-1252")
    texto = re.sub(r"^<\?xml[^>]*\?>", "", texto.lstrip())
    texto = _AMP_SUELTO.sub("&amp;", texto)
    return ET.fromstring(_MENOR_SUELTO.sub("&lt;", texto))


def kcal_desde_macros(
    proteinas: float | None, hidratos: float | None, grasas: float | None, fibra: float | None
) -> float | None:
    """Kcal con los factores del Reglamento (UE) 1169/2011 si CIQUAL no trae la energía.

    Unos 900 alimentos (verduras crudas, sobre todo) vienen sin energía pero con
    macros: 4 kcal/g proteínas e hidratos, 9 grasas, 2 fibra.
    """
    if proteinas is None or hidratos is None or grasas is None:
        return None
    return round(4 * proteinas + 4 * hidratos + 9 * grasas + 2 * (fibra or 0), 1)


def _buscar_fichero_xml(directorio: Path, tipo: str) -> Path:
    """Encuentra el fichero XML correspondiente al tipo en el directorio dado."""
    if tipo == "alim_grp":
        candidatos = [f for f in directorio.glob("*.xml") if f.name.startswith("alim_grp")]
    elif tipo == "alim":
        candidatos = [
            f
            for f in directorio.glob("*.xml")
            if f.name.startswith("alim") and not f.name.startswith("alim_grp")
        ]
    elif tipo == "compo":
        candidatos = [f for f in directorio.glob("*.xml") if f.name.startswith("compo")]
    else:
        raise ValueError(f"Tipo de fichero XML desconocido: {tipo}")

    if not candidatos:
        raise FileNotFoundError(
            f"No se encontró ningún fichero XML de tipo '{tipo}' en {directorio}"
        )
    return sorted(candidatos)[0]


def leer_xml(directorio: Path) -> list[Generico]:
    """Lee los ficheros XML de CIQUAL en el directorio y devuelve la lista de alimentos.

    El nombre de cada alimento es inicialmente el inglés original (nombre_original),
    y el grupo está en inglés.
    """
    fichero_grp = _buscar_fichero_xml(directorio, "alim_grp")
    grupos: dict[str, str] = {}
    for elem in _raiz(fichero_grp).findall("ALIM_GRP"):
        c_elem = elem.find("alim_grp_code")
        n_elem = elem.find("alim_grp_nom_eng")
        if c_elem is not None and c_elem.text and n_elem is not None and n_elem.text:
            code = c_elem.text.strip()
            nom = n_elem.text.strip()
            if code and code not in grupos:
                grupos[code] = nom

    fichero_alim = _buscar_fichero_xml(directorio, "alim")
    alimentos_base: list[tuple[str, str, str]] = []
    for elem in _raiz(fichero_alim).findall("ALIM"):
        c_elem = elem.find("alim_code")
        n_elem = elem.find("alim_nom_eng")
        g_elem = elem.find("alim_grp_code")
        code = c_elem.text.strip() if c_elem is not None and c_elem.text else ""
        nom = n_elem.text.strip() if n_elem is not None and n_elem.text else ""
        grp_code = g_elem.text.strip() if g_elem is not None and g_elem.text else ""
        if code:
            alimentos_base.append((code, nom, grupos.get(grp_code, "")))

    fichero_compo = _buscar_fichero_xml(directorio, "compo")
    composicion: dict[str, dict[str, float | None]] = {}
    for elem in _raiz(fichero_compo).iter("COMPO"):
        c_elem = elem.find("const_code")
        const_code = c_elem.text.strip() if c_elem is not None and c_elem.text else ""
        if const_code not in CONST_INTERES:
            continue
        a_elem = elem.find("alim_code")
        alim_code = a_elem.text.strip() if a_elem is not None and a_elem.text else ""
        if alim_code:
            t_elem = elem.find("teneur")
            val = valor_teneur(t_elem.text if t_elem is not None else None)
            composicion.setdefault(alim_code, {})[const_code] = val

    alimentos: list[Generico] = []
    for code, nom, grp in alimentos_base:
        c = composicion.get(code, {})
        kcal = c.get("328")
        if kcal is None:
            kcal = c.get("333")
        if kcal is None:
            kcal = kcal_desde_macros(c.get("25000"), c.get("31000"), c.get("40000"), c.get("34100"))
        alimentos.append(
            Generico(
                codigo=code,
                nombre=nom,
                nombre_original=nom,
                grupo=grp,
                kcal=kcal,
                proteinas=c.get("25000"),
                hidratos=c.get("31000"),
                grasas=c.get("40000"),
                fibra=c.get("34100"),
                azucares=c.get("32000"),
                sal=c.get("10004"),
            )
        )
    return alimentos


def traducir(
    nombres: list[str],
    settings: Settings,
    ejecutar: Callable[..., dict[str, Any]] = ia.ejecutar,
    lote: int = 150,
    cache: Path | None = None,
) -> dict[str, str]:
    """Traduce al castellano de España nombres de alimentos en inglés, por lotes.

    Instrucciones: nombre corto y natural como se diría en España, conservando
    la información de preparación; sin marcas; mismo orden; no inventes.
    Si `cache` (JSON) existe, no repite lo ya traducido y va guardando tras cada
    lote, para poder retomar si se corta. Ignora traducciones vacías u 'original'
    desconocido.
    """
    traducciones: dict[str, str] = {}
    if cache is not None and cache.exists():
        try:
            traducciones = json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            traducciones = {}

    pendientes: list[str] = []
    vistos: set[str] = set()
    for n in nombres:
        n_limpio = n.strip()
        if n_limpio and n_limpio not in traducciones and n_limpio not in vistos:
            pendientes.append(n_limpio)
            vistos.add(n_limpio)

    for i in range(0, len(pendientes), lote):
        trozo = pendientes[i : i + lote]
        lista_texto = "\n".join(f"- {nom}" for nom in trozo)
        instrucciones = (
            "Traduce al castellano de España los siguientes nombres de alimentos en inglés.\n"
            "Usa un nombre corto y natural como se diría en España "
            '("Plátano, crudo", "Pan de centeno y trigo", "Pechuga de pollo, a la plancha"), '
            "conservando la información de preparación; sin marcas; mismo orden; no inventes.\n\n"
            f"{lista_texto}"
        )
        tarea = ia.Tarea(
            nombre="traduccion",
            instrucciones=instrucciones,
            esquema=ESQUEMA_TRADUCCION,
        )
        resultado = ejecutar(tarea, settings)
        items = resultado.get("traducciones", [])
        trozo_set = set(trozo)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    orig = str(item.get("original") or "").strip()
                    es = str(item.get("es") or "").strip()
                    if orig in trozo_set and es:
                        traducciones[orig] = es

        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(traducciones, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return traducciones


def generar_csv(
    directorio: Path,
    settings: Settings,
    destino: Path = CSV,
    cache: Path | None = None,
    ejecutar: Callable[..., dict[str, Any]] = ia.ejecutar,
) -> int:
    """Genera el CSV de alimentos de CIQUAL traducidos y ordenados por código.

    Devuelve cuántas filas de alimentos escribe. Los que no se traduzcan
    se quedan con su nombre en inglés.
    """
    alimentos = leer_xml(directorio)

    a_traducir: list[str] = []
    for a in alimentos:
        if a.nombre_original:
            a_traducir.append(a.nombre_original)
        if a.grupo:
            a_traducir.append(a.grupo)

    traducciones = traducir(a_traducir, settings, ejecutar=ejecutar, cache=cache)

    procesados: list[Generico] = []
    for a in alimentos:
        nom_es = traducciones.get(a.nombre_original) or a.nombre_original
        grp_es = traducciones.get(a.grupo) or a.grupo
        procesados.append(
            Generico(
                codigo=a.codigo,
                nombre=nom_es,
                nombre_original=a.nombre_original,
                grupo=grp_es,
                kcal=a.kcal,
                proteinas=a.proteinas,
                hidratos=a.hidratos,
                grasas=a.grasas,
                fibra=a.fibra,
                azucares=a.azucares,
                sal=a.sal,
            )
        )

    procesados.sort(key=lambda x: x.codigo)

    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "codigo",
                "nombre",
                "nombre_original",
                "grupo",
                "kcal",
                "proteinas",
                "hidratos",
                "grasas",
                "fibra",
                "azucares",
                "sal",
            ]
        )
        for a in procesados:
            writer.writerow(
                [
                    a.codigo,
                    a.nombre,
                    a.nombre_original,
                    a.grupo,
                    "" if a.kcal is None else a.kcal,
                    "" if a.proteinas is None else a.proteinas,
                    "" if a.hidratos is None else a.hidratos,
                    "" if a.grasas is None else a.grasas,
                    "" if a.fibra is None else a.fibra,
                    "" if a.azucares is None else a.azucares,
                    "" if a.sal is None else a.sal,
                ]
            )

    return len(procesados)


@functools.cache
def cargar(ruta: Path = CSV) -> tuple[Generico, ...]:
    """Carga los alimentos genéricos desde el CSV. Devuelve tupla vacía si no existe."""
    if not ruta.exists():
        return ()

    def _flt(v: str | None) -> float | None:
        if not v:
            return None
        s = v.strip()
        return float(s) if s else None

    alimentos: list[Generico] = []
    with open(ruta, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            alimentos.append(
                Generico(
                    codigo=fila.get("codigo", "").strip(),
                    nombre=fila.get("nombre", "").strip(),
                    nombre_original=fila.get("nombre_original", "").strip(),
                    grupo=fila.get("grupo", "").strip(),
                    kcal=_flt(fila.get("kcal")),
                    proteinas=_flt(fila.get("proteinas")),
                    hidratos=_flt(fila.get("hidratos")),
                    grasas=_flt(fila.get("grasas")),
                    fibra=_flt(fila.get("fibra")),
                    azucares=_flt(fila.get("azucares")),
                    sal=_flt(fila.get("sal")),
                )
            )
    return tuple(alimentos)


def buscar(
    texto: str,
    limite: int = 10,
    alimentos: Sequence[Generico] | None = None,
) -> list[Generico]:
    """Busca alimentos genéricos por texto.

    Normaliza con db.normalizar_titulo. Todas las palabras de la búsqueda
    (de 2+ letras) tienen que aparecer como prefijo de alguna palabra del
    nombre normalizado.
    Orden: primero los que empiezan por la primera palabra buscada, luego
    los de nombre más corto (por número de palabras), luego por nombre.
    """
    if limite <= 0:
        return []

    fuente = alimentos if alimentos is not None else cargar()
    if not fuente:
        return []

    busqueda_norm = normalizar_titulo(texto)
    palabras_busqueda = [p for p in re.split(r"[^\w]+", busqueda_norm) if len(p) >= 2]
    if not palabras_busqueda:
        return []

    primera_palabra = palabras_busqueda[0]
    coincidencias: list[Generico] = []

    for a in fuente:
        nom_norm = normalizar_titulo(a.nombre)
        palabras_nombre = [p for p in re.split(r"[^\w]+", nom_norm) if p]
        if all(any(pn.startswith(pb) for pn in palabras_nombre) for pb in palabras_busqueda):
            coincidencias.append(a)

    def clave_orden(a: Generico) -> tuple[int, int, str]:
        nom_norm = normalizar_titulo(a.nombre)
        palabras = [p for p in re.split(r"[^\w]+", nom_norm) if p]
        empieza = 0 if palabras and palabras[0].startswith(primera_palabra) else 1
        return (empieza, len(palabras), nom_norm)

    coincidencias.sort(key=clave_orden)
    return coincidencias[:limite]


def por_codigo(codigo: str, alimentos: Sequence[Generico] | None = None) -> Generico | None:
    """Devuelve el alimento genérico con el código indicado, o None si no existe."""
    fuente = alimentos if alimentos is not None else cargar()
    cod = codigo.strip()
    for a in fuente:
        if a.codigo == cod:
            return a
    return None
