"""Productos de Open Food Facts por código de barras (EAN-8, EAN-13 y UPC-A).

Open Food Facts pide identificarse con un User-Agent propio y no abusar de la
API (unas 100 consultas de producto por minuto). Los valores nutricionales se
dan por 100 g o por 100 ml; si faltan no se ponen a 0, se dejan en `None` y el
producto queda marcado como incompleto para que la app pueda avisar.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal, NamedTuple

import httpx

from . import ciqual, db
from .raciones import limpiar_racion_nombre

URL_PRODUCTO = "https://world.openfoodfacts.org/api/v2/product/{codigo}.json"
# Buscador nuevo de Open Food Facts (search-a-licious): el antiguo cgi/search.pl da 503 a menudo.
URL_BUSQUEDA = "https://search.openfoodfacts.org/search"
# Caracteres con significado en su sintaxis de consulta (Lucene): se quitan del texto buscado.
_SINTAXIS_BUSQUEDA = re.compile(r'[:"()\[\]{}^~*?\\/+!&|-]')
TIMEOUT_BUSQUEDA = 8.0
CAMPOS = (
    "code",
    "product_name",
    "product_name_es",
    "brands",
    "nutriscore_grade",
    "quantity",
    "product_quantity_unit",
    "nutriments",
    "serving_quantity",
    "serving_size",
)
TIMEOUT = 10.0
KJ_POR_KCAL = 4.184
LONGITUDES_VALIDAS = (8, 12, 13)
DIAS_CACHE = 30

Unidad = Literal["g", "ml"]


class ErrorAlimentos(Exception):
    """Error al consultar un producto en Open Food Facts."""


class CodigoInvalido(ErrorAlimentos):
    """El código no es un EAN-8, EAN-13 o UPC-A con el dígito de control bien."""


class ProductoNoEncontrado(ErrorAlimentos):
    """Open Food Facts no tiene ese producto."""


class OpenFoodFactsNoDisponible(ErrorAlimentos):
    """Error de red o respuesta inesperada de Open Food Facts."""


@dataclass(frozen=True)
class Producto:
    """Producto con sus valores nutricionales por 100 g o 100 ml.

    Los nutrientes que Open Food Facts no tiene quedan en `None`, nunca a 0.
    """

    codigo: str
    nombre: str | None
    marca: str | None
    nutriscore: str | None
    unidad: Unidad
    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None
    preparado: bool = False
    origen: str = "off"
    id_propio: int | None = None
    racion_g: float | None = None
    racion_nombre: str | None = None

    @property
    def incompleto(self) -> bool:
        """Falta alguno de los valores nutricionales básicos."""
        return any(v is None for v in (self.kcal, self.proteinas, self.hidratos, self.grasas))


class ResultadoProducto(NamedTuple):
    """Resultado de consultar un producto, indicando si proviene de la caché."""

    producto: Producto
    desde_cache: bool


class Nutrientes(NamedTuple):
    """Nutrientes calculados para una cantidad concreta de producto."""

    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None

    @property
    def incompleto(self) -> bool:
        """Falta alguno de los valores nutricionales básicos."""
        return any(v is None for v in (self.kcal, self.proteinas, self.hidratos, self.grasas))


@dataclass(frozen=True)
class ResultadoBusqueda:
    """Resultado básico de una búsqueda de producto en Open Food Facts."""

    codigo: str
    nombre: str
    marca: str | None
    kcal: float | None


def user_agent() -> str:
    """User-Agent propio, como pide Open Food Facts."""
    try:
        v = version("salud")
    except PackageNotFoundError:
        v = "0.0.0"
    return f"salud/{v} (pelayopf@gmail.com)"


def normalizar_codigo(codigo: str) -> str:
    """Quita espacios y guiones del código leído o tecleado."""
    return re.sub(r"[\s-]", "", codigo)


def codigo_valido(codigo: str) -> bool:
    """Comprueba longitud y dígito de control GS1 (EAN-8, UPC-A de 12, EAN-13)."""
    if not codigo.isascii() or not codigo.isdigit() or len(codigo) not in LONGITUDES_VALIDAS:
        return False
    *cuerpo, control = (int(c) for c in codigo)
    # Desde la derecha (sin el control), los dígitos alternan peso 3 y 1.
    suma = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(cuerpo)))
    return (10 - suma % 10) % 10 == control


def consultar_open_food_facts(
    codigo: str, cliente: httpx.Client | None = None
) -> tuple[Producto, dict[str, Any]]:
    """Consulta Open Food Facts y devuelve el Producto y el JSON crudo del producto."""
    codigo = normalizar_codigo(codigo)
    if not codigo_valido(codigo):
        raise CodigoInvalido(f"código de barras no válido: {codigo!r}")
    propio = cliente is None
    http = cliente or httpx.Client(timeout=TIMEOUT)
    try:
        respuesta = http.get(
            URL_PRODUCTO.format(codigo=codigo),
            params={"fields": ",".join(CAMPOS)},
            headers={"User-Agent": user_agent()},
        )
    except httpx.HTTPError as e:
        raise OpenFoodFactsNoDisponible(f"no se pudo consultar Open Food Facts: {e}") from e
    finally:
        if propio:
            http.close()
    if respuesta.status_code == 404:
        raise ProductoNoEncontrado(f"Open Food Facts no tiene el producto {codigo}")
    if respuesta.status_code != 200:
        raise OpenFoodFactsNoDisponible(
            f"Open Food Facts respondió {respuesta.status_code} para {codigo}"
        )
    try:
        datos = respuesta.json()
    except ValueError as e:
        raise OpenFoodFactsNoDisponible("Open Food Facts devolvió una respuesta no JSON") from e
    if not isinstance(datos, dict):
        raise OpenFoodFactsNoDisponible("Open Food Facts devolvió una respuesta inesperada")
    producto = datos.get("product")
    if datos.get("status") != 1 or not isinstance(producto, dict):
        raise ProductoNoEncontrado(f"Open Food Facts no tiene el producto {codigo}")
    return producto_desde_json(codigo, producto), producto


def buscar_producto(codigo: str, cliente: httpx.Client | None = None) -> Producto:
    """Consulta un producto en Open Food Facts por su código de barras.

    Args:
        codigo: EAN-8, EAN-13 o UPC-A (se ignoran espacios y guiones).
        cliente: cliente HTTP a usar (en los tests, uno con transporte falso).

    Raises:
        CodigoInvalido: el código no tiene un formato o dígito de control válido.
        ProductoNoEncontrado: Open Food Facts no tiene el producto.
        OpenFoodFactsNoDisponible: error de red o respuesta inesperada.
    """
    producto, _ = consultar_open_food_facts(codigo, cliente=cliente)
    return producto


def buscar_off(
    texto: str, cliente: httpx.Client | None = None, limite: int = 10
) -> list[ResultadoBusqueda]:
    """Busca productos en Open Food Facts por texto."""
    q = " ".join(_SINTAXIS_BUSQUEDA.sub(" ", texto).split())
    if not q or limite <= 0:
        return []

    propio = cliente is None
    http = cliente or httpx.Client(timeout=TIMEOUT_BUSQUEDA)
    try:
        respuesta = http.get(
            URL_BUSQUEDA,
            params={
                "q": f'{q} countries_tags:"en:spain"',
                "langs": "es",
                # Pide de más: busca palabras enteras y luego se filtra por prefijos.
                "page_size": limite * 3,
                "fields": "code,product_name,product_name_es,brands,nutriments",
            },
            headers={"User-Agent": user_agent()},
        )
    except httpx.HTTPError:
        raise OpenFoodFactsNoDisponible("Open Food Facts no responde ahora") from None
    finally:
        if propio:
            http.close()

    if respuesta.status_code != 200:
        raise OpenFoodFactsNoDisponible("Open Food Facts no responde ahora")

    try:
        datos = respuesta.json()
    except (ValueError, TypeError):
        raise OpenFoodFactsNoDisponible("Open Food Facts no responde ahora") from None

    products = datos.get("hits") if isinstance(datos, dict) else None
    if not isinstance(products, list):
        raise OpenFoodFactsNoDisponible("Open Food Facts no responde ahora")

    buscadas = db.palabras_buscadas(q)
    resultados: list[ResultadoBusqueda] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        codigo_raw = p.get("code")
        if not isinstance(codigo_raw, str):
            continue
        codigo = normalizar_codigo(codigo_raw)
        if not codigo_valido(codigo):
            continue
        nombre = _texto(p.get("product_name_es")) or _texto(p.get("product_name"))
        if not nombre:
            continue
        marca = _primera_marca(p.get("brands"))
        if not db.encaja(buscadas, nombre, marca):
            continue
        nutrientes = p.get("nutriments")
        n: dict[str, Any] = nutrientes if isinstance(nutrientes, dict) else {}
        kcal = _numero(n.get("energy-kcal_100g"))
        if kcal is None:
            kj = _numero(n.get("energy_100g"))
            if kj is not None:
                kcal = round(kj / KJ_POR_KCAL, 1)
        resultados.append(ResultadoBusqueda(codigo=codigo, nombre=nombre, marca=marca, kcal=kcal))

    return resultados[:limite]


def producto_desde_json(codigo: str, p: dict[str, Any]) -> Producto:
    """Convierte el `product` de la API de Open Food Facts en un `Producto`."""
    nutrientes = p.get("nutriments")
    n: dict[str, Any] = nutrientes if isinstance(nutrientes, dict) else {}

    preparado = False

    kcal = _numero(n.get("energy-kcal_100g"))
    if kcal is None:
        kj = _numero(n.get("energy_100g"))
        if kj is not None:
            kcal = round(kj / KJ_POR_KCAL, 1)
        else:
            kcal_prep = _numero(n.get("energy-kcal_prepared_100g"))
            if kcal_prep is not None:
                kcal = kcal_prep
                preparado = True
            else:
                kj_prep = _numero(n.get("energy_prepared_100g"))
                if kj_prep is not None:
                    kcal = round(kj_prep / KJ_POR_KCAL, 1)
                    preparado = True

    proteinas = _numero(n.get("proteins_100g"))
    if proteinas is None:
        proteinas_prep = _numero(n.get("proteins_prepared_100g"))
        if proteinas_prep is not None:
            proteinas = proteinas_prep
            preparado = True

    hidratos = _numero(n.get("carbohydrates_100g"))
    if hidratos is None:
        hidratos_prep = _numero(n.get("carbohydrates_prepared_100g"))
        if hidratos_prep is not None:
            hidratos = hidratos_prep
            preparado = True

    grasas = _numero(n.get("fat_100g"))
    if grasas is None:
        grasas_prep = _numero(n.get("fat_prepared_100g"))
        if grasas_prep is not None:
            grasas = grasas_prep
            preparado = True

    sq = _numero(p.get("serving_quantity"))
    racion_g = sq if sq is not None and sq > 0 else None
    racion_nombre = "ración" if racion_g is not None else None

    return Producto(
        codigo=codigo,
        nombre=_texto(p.get("product_name_es")) or _texto(p.get("product_name")),
        marca=_primera_marca(p.get("brands")),
        nutriscore=_nutriscore(p.get("nutriscore_grade")),
        unidad=_unidad(p),
        kcal=kcal,
        proteinas=proteinas,
        hidratos=hidratos,
        grasas=grasas,
        preparado=preparado,
        racion_g=racion_g,
        racion_nombre=racion_nombre,
    )


def producto_desde_fila(fila: sqlite3.Row) -> Producto:
    """Construye un Producto a partir de una fila de la tabla productos."""
    try:
        raw = fila["datos"]
    except (IndexError, KeyError):
        raw = None

    if raw and isinstance(raw, str) and raw.strip():
        try:
            datos = json.loads(raw)
            if isinstance(datos, dict) and datos:
                return producto_desde_json(fila["codigo"], datos)
        except (json.JSONDecodeError, ValueError):
            pass

    return Producto(
        codigo=fila["codigo"],
        nombre=fila["nombre"],
        marca=fila["marca"],
        nutriscore=fila["nutriscore"],
        unidad=fila["unidad"],
        kcal=fila["kcal"],
        proteinas=fila["proteinas"],
        hidratos=fila["hidratos"],
        grasas=fila["grasas"],
    )


def producto_desde_propio(fila: sqlite3.Row, nutriscore: str | None = None) -> Producto:
    """Construye un Producto a partir de una fila de la tabla productos_propios."""
    try:
        racion_g = fila["racion_g"]
        racion_nombre = fila["racion_nombre"]
    except (IndexError, KeyError):
        racion_g = None
        racion_nombre = None

    return Producto(
        codigo=fila["codigo"] or "",
        nombre=fila["nombre"],
        marca=fila["marca"],
        nutriscore=nutriscore,
        unidad=fila["unidad"],
        kcal=fila["kcal"],
        proteinas=fila["proteinas"],
        hidratos=fila["hidratos"],
        grasas=fila["grasas"],
        preparado=False,
        origen=fila["origen"],
        id_propio=fila["id"],
        racion_g=racion_g,
        racion_nombre=racion_nombre,
    )


def producto_desde_generico(g: ciqual.Generico) -> Producto:
    """Construye un Producto a partir de un alimento genérico de CIQUAL."""
    return Producto(
        codigo="",
        nombre=g.nombre,
        marca=None,
        nutriscore=None,
        unidad="g",
        kcal=g.kcal,
        proteinas=g.proteinas,
        hidratos=g.hidratos,
        grasas=g.grasas,
        preparado=False,
        origen="ciqual",
    )


def nombre_con_raciones(
    nombre: str,
    cantidad: float,
    racion_g: float | None,
    racion_nombre: str | None,
) -> str:
    """Añade la ración al nombre si la cantidad es un múltiplo exacto de la ración."""
    racion_limpia = limpiar_racion_nombre(racion_nombre)
    if not racion_g or racion_g <= 0 or not racion_limpia:
        return nombre

    ratio = cantidad / racion_g
    n = round(ratio)
    if n >= 1 and abs(cantidad - n * racion_g) < 1e-6:
        if n == 1:
            texto_racion = f"1 {racion_limpia}"
        else:
            palabras = racion_limpia.split()
            if len(palabras) == 1:
                if racion_limpia.endswith("ón"):
                    plural = racion_limpia[:-2] + "ones"
                elif racion_limpia.endswith("s"):
                    plural = racion_limpia
                else:
                    plural = f"{racion_limpia}s"
                texto_racion = f"{n} {plural}"
            else:
                texto_racion = f"{n} × {racion_limpia}"
        return f"{nombre} ({texto_racion})"
    return nombre


def obtener_producto(
    con: sqlite3.Connection,
    codigo: str,
    cliente: httpx.Client | None = None,
    *,
    dias_max: int = DIAS_CACHE,
) -> ResultadoProducto:
    """Devuelve un producto usando primero la caché y solo si no está consulta Open Food Facts.

    Si el producto está en la base de datos y su fecha de consulta es de hace menos
    de `dias_max` días (30 por defecto), se devuelve directamente de la caché.
    Si la caché ha caducado o no existe, se consulta Open Food Facts. Si Open Food
    Facts no está disponible pero existe una entrada en caché (aunque esté caducada),
    se devuelve la versión en caché en lugar de fallar.
    """
    codigo = normalizar_codigo(codigo)
    if not codigo_valido(codigo):
        raise CodigoInvalido(f"código de barras no válido: {codigo!r}")

    propio = db.producto_propio_por_codigo(con, codigo)
    if propio is not None:
        fila_off = db.leer_producto(con, codigo)
        nutriscore = fila_off["nutriscore"] if fila_off is not None else None
        return ResultadoProducto(producto_desde_propio(propio, nutriscore), desde_cache=True)

    fila = db.leer_producto(con, codigo)
    if fila is not None and fila["consultado"]:
        try:
            fecha_consulta = datetime.fromisoformat(fila["consultado"])
            if fecha_consulta.tzinfo is None:
                fecha_consulta = fecha_consulta.replace(tzinfo=UTC)
            if (datetime.now(UTC) - fecha_consulta) < timedelta(days=dias_max):
                return ResultadoProducto(producto_desde_fila(fila), desde_cache=True)
        except ValueError:
            pass

    try:
        producto_off, datos_crudos = consultar_open_food_facts(codigo, cliente=cliente)
    except OpenFoodFactsNoDisponible:
        if fila is not None:
            return ResultadoProducto(producto_desde_fila(fila), desde_cache=True)
        raise

    db.guardar_producto(
        con,
        codigo=producto_off.codigo,
        nombre=producto_off.nombre,
        marca=producto_off.marca,
        nutriscore=producto_off.nutriscore,
        unidad=producto_off.unidad,
        kcal=producto_off.kcal,
        proteinas=producto_off.proteinas,
        hidratos=producto_off.hidratos,
        grasas=producto_off.grasas,
        datos=datos_crudos,
    )
    return ResultadoProducto(producto_off, desde_cache=False)


def calcular_nutrientes(producto: Producto, cantidad: float) -> Nutrientes:
    """Calcula los nutrientes para una cantidad (en g o ml) según el producto.

    Aplica una regla de tres sobre los valores por 100 g o 100 ml.
    Si algún nutriente es `None`, se mantiene como `None` sin contar como 0.
    """
    factor = cantidad / 100.0
    return Nutrientes(
        kcal=round(producto.kcal * factor, 1) if producto.kcal is not None else None,
        proteinas=round(producto.proteinas * factor, 1) if producto.proteinas is not None else None,
        hidratos=round(producto.hidratos * factor, 1) if producto.hidratos is not None else None,
        grasas=round(producto.grasas * factor, 1) if producto.grasas is not None else None,
    )


def _numero(valor: Any) -> float | None:
    """Número de la API (a veces llega como texto); `None` si no lo hay."""
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, int | float):
        return float(valor)
    if isinstance(valor, str):
        try:
            return float(valor.replace(",", "."))
        except ValueError:
            return None
    return None


def _texto(valor: Any) -> str | None:
    """Texto sin espacios sobrantes; `None` si viene vacío."""
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    return None


def _primera_marca(valor: Any) -> str | None:
    """Primera marca: Open Food Facts las da separadas por comas (o en lista, al buscar)."""
    if isinstance(valor, list):
        valor = valor[0] if valor else None
    texto = _texto(valor)
    return _texto(texto.split(",")[0]) if texto else None


def _nutriscore(valor: Any) -> str | None:
    """Letra del Nutri-Score en mayúscula; `None` si no hay o no aplica."""
    texto = _texto(valor)
    if texto and len(texto) == 1 and texto.lower() in "abcde":
        return texto.upper()
    return None


def _unidad(p: dict[str, Any]) -> Unidad:
    """Ml si el producto se vende por volumen; si no, g.

    Open Food Facts nombra los nutrientes `_100g` también en las bebidas; la
    unidad se deduce de la cantidad del envase.
    """
    unidad = _texto(p.get("product_quantity_unit"))
    if unidad is None:
        cantidad = _texto(p.get("quantity")) or ""
        m = re.search(r"([a-zA-Z]+)\s*$", cantidad)
        unidad = m.group(1) if m else None
    return "ml" if unidad and unidad.lower() in ("ml", "cl", "dl", "l") else "g"
