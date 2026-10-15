"""Catálogo de platos preparados de Artema (artema.club, tienda Shopify).

Descarga `products.json` y lee la etiqueta nutricional de cada ficha HTML.
"""

from __future__ import annotations

import html as html_lib
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import db

BASE_URL = "https://artema.club"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

# Productos de la tienda que no son platos.
EXCLUDED_HANDLES = frozenset({"tarjetaregalo"})

REQUEST_DELAY_S = 0.2

_NUM = r"([\d]+(?:[.,]\d+)?)"
_LABEL_FIELDS: dict[str, re.Pattern[str]] = {
    "kcal": re.compile(_NUM + r"\s*kcal", re.IGNORECASE),
    "grasas": re.compile(r"\bGrasas\s+" + _NUM + r"\s*g", re.IGNORECASE),
    "saturadas": re.compile(r"\bGrasas\s+saturadas\s+" + _NUM + r"\s*g", re.IGNORECASE),
    "hidratos": re.compile(r"\bHidratos(?:\s+de\s+carbono)?\s+" + _NUM + r"\s*g", re.IGNORECASE),
    "azucares": re.compile(r"\bAz[uú]cares\s+" + _NUM + r"\s*g", re.IGNORECASE),
    "fibra": re.compile(r"\bFibra(?:\s+alimentaria)?\s+" + _NUM + r"\s*g", re.IGNORECASE),
    "proteinas": re.compile(r"\bProte[ií]nas?\s+" + _NUM + r"\s*g", re.IGNORECASE),
    "sal": re.compile(r"\bSal\s+" + _NUM + r"\s*g", re.IGNORECASE),
}
_RE_SERVING_WEIGHT = re.compile(r"\(\s*" + _NUM + r"\s*g\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class Plato:
    """Plato del catálogo de Artema con precio y valores nutricionales por ración."""

    handle: str
    variant_id: int
    nombre: str
    url: str
    categoria: str
    precio: float
    en_oferta: bool
    precio_original: float | None
    peso_g: float | None
    kcal: float | None
    proteinas: float | None
    hidratos: float | None
    grasas: float | None
    azucares: float | None
    saturadas: float | None
    sal: float | None
    ingredientes: str | None
    disponible: bool
    actualizado: str

    @classmethod
    def desde_fila(cls, fila: Any) -> Plato:
        """Construye un Plato desde una fila de SQLite o diccionario."""
        return cls(
            handle=fila["handle"],
            variant_id=int(fila["variant_id"]),
            nombre=fila["nombre"],
            url=fila["url"],
            categoria=fila["categoria"],
            precio=float(fila["precio"]),
            en_oferta=bool(fila["en_oferta"]),
            precio_original=(
                float(fila["precio_original"]) if fila["precio_original"] is not None else None
            ),
            peso_g=float(fila["peso_g"]) if fila["peso_g"] is not None else None,
            kcal=float(fila["kcal"]) if fila["kcal"] is not None else None,
            proteinas=float(fila["proteinas"]) if fila["proteinas"] is not None else None,
            hidratos=float(fila["hidratos"]) if fila["hidratos"] is not None else None,
            grasas=float(fila["grasas"]) if fila["grasas"] is not None else None,
            azucares=float(fila["azucares"]) if fila["azucares"] is not None else None,
            saturadas=float(fila["saturadas"]) if fila["saturadas"] is not None else None,
            sal=float(fila["sal"]) if fila["sal"] is not None else None,
            ingredientes=fila["ingredientes"],
            disponible=bool(fila["disponible"]),
            actualizado=fila["actualizado"],
        )


@dataclass(frozen=True)
class ResultadoArtema:
    """Resultado de la sincronización del catálogo de Artema."""

    platos: int
    nuevos: int
    errores: list[str]


def _num(value: str) -> float:
    return float(value.replace(",", "."))


def parse_label(text: str) -> dict[str, float | None] | None:
    """Extrae los nutrientes de una etiqueta en texto ("Energía 890,90kJ, 212,93kcal...").

    Devuelve None si faltan kcal o proteínas; el resto queda en None si no aparecen.
    """
    values: dict[str, float | None] = {}
    for field, pattern in _LABEL_FIELDS.items():
        m = pattern.search(text)
        values[field] = _num(m.group(1)) if m else None
    if values["kcal"] is None or values["proteinas"] is None:
        return None
    return values


def _clean_html(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = html_lib.unescape(text)
    return " ".join(text.split())


def parse_product_page(html: str) -> dict[str, Any]:
    """Lee de la ficha de un producto la etiqueta por ración, su peso y los ingredientes.

    Si solo hay etiqueta por 100 g se devuelve en `per_100g` para escalarla
    con el peso de la ración.
    """
    out: dict[str, Any] = {
        "peso_g": None,
        "per_serving": None,
        "per_100g": None,
        "ingredientes": None,
    }
    pattern = re.compile(
        r'<span[^>]*class=["\'][^"\']*apm__nutr-sub[^"\']*["\'][^>]*>(.*?)</span>\s*<div[^>]*class=["\'][^"\']*apm__nutr-body[^"\']*["\'][^>]*>(.*?)</div>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        label = _clean_html(m.group(1))
        body_text = _clean_html(m.group(2))
        lower = label.lower()
        if lower.startswith("por raci"):
            weight = _RE_SERVING_WEIGHT.search(label)
            out["peso_g"] = _num(weight.group(1)) if weight else None
            out["per_serving"] = parse_label(body_text)
        elif lower.startswith("por 100"):
            out["per_100g"] = parse_label(body_text)
        elif lower.startswith("ingredientes"):
            out["ingredientes"] = body_text or None
    return out


def _serving_nutrition(
    page: dict[str, Any], variant_grams: float | None
) -> tuple[float | None, dict[str, float | None] | None]:
    """Devuelve (peso de la ración, nutrientes por ración) a partir de la ficha."""
    weight = page.get("peso_g") or variant_grams or None
    if page.get("per_serving"):
        return weight, page["per_serving"]
    if page.get("per_100g") and weight:
        factor = weight / 100
        return weight, {
            k: round(v * factor, 2) if v is not None else None for k, v in page["per_100g"].items()
        }
    return weight, None


def build_plato(product: dict[str, Any], page: dict[str, Any] | None, actualizado: str) -> Plato:
    """Construye un Plato a partir del producto en Shopify y de su ficha opcional."""
    variant = product["variants"][0]
    price = float(variant["price"])
    compare_at = variant.get("compare_at_price")
    on_sale = bool(compare_at) and float(compare_at) > price
    variant_grams = float(variant.get("grams") or 0) or None

    weight, nutr = _serving_nutrition(page, variant_grams) if page else (variant_grams, None)
    nutr = nutr or {}

    return Plato(
        handle=product["handle"],
        variant_id=int(variant["id"]),
        nombre=product["title"],
        url=f"{BASE_URL}/products/{product['handle']}",
        categoria=product.get("product_type") or "Principal",
        precio=price,
        en_oferta=on_sale,
        precio_original=float(compare_at) if on_sale else None,
        peso_g=weight,
        kcal=nutr.get("kcal"),
        proteinas=nutr.get("proteinas"),
        hidratos=nutr.get("hidratos"),
        grasas=nutr.get("grasas"),
        azucares=nutr.get("azucares"),
        saturadas=nutr.get("saturadas"),
        sal=nutr.get("sal"),
        ingredientes=page.get("ingredientes") if page else None,
        disponible=any(v.get("available", True) for v in product["variants"]),
        actualizado=actualizado,
    )


def list_products(cliente: httpx.Client, pausa_s: float = REQUEST_DELAY_S) -> list[dict[str, Any]]:
    """Descarga todos los productos de `/products.json` que son platos preparados."""
    products: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = cliente.get(
            f"{BASE_URL}/products.json?limit=250&page={page}",
            headers=HEADERS,
            timeout=20.0,
        )
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
        if pausa_s > 0:
            time.sleep(pausa_s)
    return [
        p
        for p in products
        if p.get("handle") not in EXCLUDED_HANDLES
        and p.get("variants")
        and p["variants"][0].get("requires_shipping", True) is not False
    ]


def fetch_product_page(handle: str, cliente: httpx.Client) -> dict[str, Any]:
    """Descarga y parsea la ficha HTML de un producto."""
    resp = cliente.get(f"{BASE_URL}/products/{handle}", headers=HEADERS, timeout=20.0)
    resp.raise_for_status()
    return parse_product_page(resp.text)


def actualizar(
    con: sqlite3.Connection,
    cliente: httpx.Client,
    completo: bool = False,
    pausa_s: float = REQUEST_DELAY_S,
) -> ResultadoArtema:
    """Actualiza la tabla de platos de Artema a partir de la tienda online.

    Sin `completo=True`, solo lee fichas de platos nuevos o sin macros en la BD.
    Los platos que ya no están en la tienda se marcan con disponible=0.
    Si falla una ficha, se conservan los datos anteriores.
    """
    products = list_products(cliente, pausa_s=pausa_s)
    if not products:
        raise RuntimeError("artema.club no ha devuelto ningún producto; no se toca el catálogo")

    platos_viejos = {f["handle"]: f for f in db.platos_artema(con, solo_disponibles=False)}
    ahora_str = db.ahora()
    platos_a_guardar: list[Plato] = []
    errores: list[str] = []
    nuevos = 0

    for prod in products:
        handle = prod["handle"]
        viejo = platos_viejos.get(handle)
        if handle not in platos_viejos:
            nuevos += 1

        if not completo and viejo is not None and viejo["kcal"] is not None:
            variant = prod["variants"][0]
            price = float(variant["price"])
            compare_at = variant.get("compare_at_price")
            on_sale = bool(compare_at) and float(compare_at) > price
            plato = Plato(
                handle=handle,
                variant_id=int(variant["id"]),
                nombre=prod["title"],
                url=f"{BASE_URL}/products/{handle}",
                categoria=prod.get("product_type") or "Principal",
                precio=price,
                en_oferta=on_sale,
                precio_original=float(compare_at) if on_sale else None,
                peso_g=viejo["peso_g"],
                kcal=viejo["kcal"],
                proteinas=viejo["proteinas"],
                hidratos=viejo["hidratos"],
                grasas=viejo["grasas"],
                azucares=viejo["azucares"],
                saturadas=viejo["saturadas"],
                sal=viejo["sal"],
                ingredientes=viejo["ingredientes"],
                disponible=any(v.get("available", True) for v in prod["variants"]),
                actualizado=ahora_str,
            )
            platos_a_guardar.append(plato)
            continue

        try:
            page = fetch_product_page(handle, cliente)
        except (httpx.HTTPError, Exception) as exc:
            errores.append(f"{prod['title']}: {exc}")
            if viejo is not None and viejo["kcal"] is not None:
                variant = prod["variants"][0]
                price = float(variant["price"])
                compare_at = variant.get("compare_at_price")
                on_sale = bool(compare_at) and float(compare_at) > price
                plato = Plato(
                    handle=handle,
                    variant_id=int(variant["id"]),
                    nombre=prod["title"],
                    url=f"{BASE_URL}/products/{handle}",
                    categoria=prod.get("product_type") or "Principal",
                    precio=price,
                    en_oferta=on_sale,
                    precio_original=float(compare_at) if on_sale else None,
                    peso_g=viejo["peso_g"],
                    kcal=viejo["kcal"],
                    proteinas=viejo["proteinas"],
                    hidratos=viejo["hidratos"],
                    grasas=viejo["grasas"],
                    azucares=viejo["azucares"],
                    saturadas=viejo["saturadas"],
                    sal=viejo["sal"],
                    ingredientes=viejo["ingredientes"],
                    disponible=any(v.get("available", True) for v in prod["variants"]),
                    actualizado=ahora_str,
                )
            else:
                plato = build_plato(prod, None, ahora_str)
            platos_a_guardar.append(plato)
            continue

        plato = build_plato(prod, page, ahora_str)
        platos_a_guardar.append(plato)
        if pausa_s > 0:
            time.sleep(pausa_s)

    handles_actuales = {p["handle"] for p in products}
    for viejo in platos_viejos.values():
        if viejo["handle"] not in handles_actuales:
            plato_retirado = Plato(
                handle=viejo["handle"],
                variant_id=int(viejo["variant_id"]),
                nombre=viejo["nombre"],
                url=viejo["url"],
                categoria=viejo["categoria"],
                precio=float(viejo["precio"]),
                en_oferta=bool(viejo["en_oferta"]),
                precio_original=(
                    float(viejo["precio_original"])
                    if viejo["precio_original"] is not None
                    else None
                ),
                peso_g=float(viejo["peso_g"]) if viejo["peso_g"] is not None else None,
                kcal=float(viejo["kcal"]) if viejo["kcal"] is not None else None,
                proteinas=float(viejo["proteinas"]) if viejo["proteinas"] is not None else None,
                hidratos=float(viejo["hidratos"]) if viejo["hidratos"] is not None else None,
                grasas=float(viejo["grasas"]) if viejo["grasas"] is not None else None,
                azucares=float(viejo["azucares"]) if viejo["azucares"] is not None else None,
                saturadas=float(viejo["saturadas"]) if viejo["saturadas"] is not None else None,
                sal=float(viejo["sal"]) if viejo["sal"] is not None else None,
                ingredientes=viejo["ingredientes"],
                disponible=False,
                actualizado=viejo["actualizado"],
            )
            platos_a_guardar.append(plato_retirado)

    db.guardar_platos_artema(con, platos_a_guardar)
    return ResultadoArtema(platos=len(products), nuevos=nuevos, errores=errores)
