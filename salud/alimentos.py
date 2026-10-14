"""Productos de Open Food Facts por código de barras (EAN-8, EAN-13 y UPC-A).

Open Food Facts pide identificarse con un User-Agent propio y no abusar de la
API (unas 100 consultas de producto por minuto). Los valores nutricionales se
dan por 100 g o por 100 ml; si faltan no se ponen a 0, se dejan en `None` y el
producto queda marcado como incompleto para que la app pueda avisar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

import httpx

URL_PRODUCTO = "https://world.openfoodfacts.org/api/v2/product/{codigo}.json"
CAMPOS = (
    "code",
    "product_name",
    "product_name_es",
    "brands",
    "nutriscore_grade",
    "quantity",
    "product_quantity_unit",
    "nutriments",
)
TIMEOUT = 10.0
KJ_POR_KCAL = 4.184
LONGITUDES_VALIDAS = (8, 12, 13)

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

    @property
    def incompleto(self) -> bool:
        """Falta alguno de los valores nutricionales básicos."""
        return any(v is None for v in (self.kcal, self.proteinas, self.hidratos, self.grasas))


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
    return producto_desde_json(codigo, producto)


def producto_desde_json(codigo: str, p: dict[str, Any]) -> Producto:
    """Convierte el `product` de la API de Open Food Facts en un `Producto`."""
    nutrientes = p.get("nutriments")
    n: dict[str, Any] = nutrientes if isinstance(nutrientes, dict) else {}
    kcal = _numero(n.get("energy-kcal_100g"))
    if kcal is None:
        kj = _numero(n.get("energy_100g"))
        kcal = round(kj / KJ_POR_KCAL, 1) if kj is not None else None
    return Producto(
        codigo=codigo,
        nombre=_texto(p.get("product_name_es")) or _texto(p.get("product_name")),
        marca=_primera_marca(p.get("brands")),
        nutriscore=_nutriscore(p.get("nutriscore_grade")),
        unidad=_unidad(p),
        kcal=kcal,
        proteinas=_numero(n.get("proteins_100g")),
        hidratos=_numero(n.get("carbohydrates_100g")),
        grasas=_numero(n.get("fat_100g")),
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
    """Open Food Facts da las marcas separadas por comas: se usa la primera."""
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
