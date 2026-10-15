"""Catálogo de platos preparados de Artema (artema.club, tienda Shopify).

Descarga `products.json` y lee la etiqueta nutricional de cada ficha HTML.
"""

from __future__ import annotations

import html as html_lib
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
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

# Artema responde 429 si se le piden las fichas seguidas: pausa entre
# peticiones y, si aun así corta, reintento esperando lo que pida.
REQUEST_DELAY_S = 1.0
REINTENTOS_429 = 4
ESPERA_429_S = 5.0

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


def _espera_reintento(resp: httpx.Response, intento: int) -> float:
    """Segundos a esperar tras un 429: el Retry-After si lo trae, si no, backoff."""
    retry_after = resp.headers.get("Retry-After", "")
    try:
        return max(float(retry_after), 1.0)
    except ValueError:
        return ESPERA_429_S * 2**intento


def _get(cliente: httpx.Client, url: str) -> httpx.Response:
    """GET a la tienda que reintenta ante 429 y lanza en cualquier otro error HTTP."""
    for intento in range(REINTENTOS_429 + 1):
        resp = cliente.get(url, headers=HEADERS, timeout=20.0)
        if resp.status_code != 429 or intento == REINTENTOS_429:
            break
        time.sleep(_espera_reintento(resp, intento))
    resp.raise_for_status()
    return resp


def list_products(cliente: httpx.Client, pausa_s: float = REQUEST_DELAY_S) -> list[dict[str, Any]]:
    """Descarga todos los productos de `/products.json` que son platos preparados."""
    products: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = _get(cliente, f"{BASE_URL}/products.json?limit=250&page={page}")
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
    return parse_product_page(_get(cliente, f"{BASE_URL}/products/{handle}").text)


def _plato_con_macros_guardados(prod: dict[str, Any], viejo: Any, actualizado: str) -> Plato:
    """Plato con precio y disponibilidad de la tienda y la etiqueta ya guardada en la BD."""
    plato = build_plato(prod, None, actualizado)
    return replace(
        plato,
        peso_g=viejo["peso_g"],
        kcal=viejo["kcal"],
        proteinas=viejo["proteinas"],
        hidratos=viejo["hidratos"],
        grasas=viejo["grasas"],
        azucares=viejo["azucares"],
        saturadas=viejo["saturadas"],
        sal=viejo["sal"],
        ingredientes=viejo["ingredientes"],
    )


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
    pidio_ficha = False

    for prod in products:
        handle = prod["handle"]
        viejo = platos_viejos.get(handle)
        if handle not in platos_viejos:
            nuevos += 1

        con_macros = viejo is not None and viejo["kcal"] is not None
        if not completo and con_macros:
            platos_a_guardar.append(_plato_con_macros_guardados(prod, viejo, ahora_str))
            continue

        if pausa_s > 0 and pidio_ficha:
            time.sleep(pausa_s)
        pidio_ficha = True
        try:
            page = fetch_product_page(handle, cliente)
        except Exception as exc:
            errores.append(f"{prod['title']}: {exc}")
            if con_macros:
                platos_a_guardar.append(_plato_con_macros_guardados(prod, viejo, ahora_str))
            else:
                platos_a_guardar.append(build_plato(prod, None, ahora_str))
            continue

        platos_a_guardar.append(build_plato(prod, page, ahora_str))

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


# ── Recomendación de platos ───────────────────────────────────────────────────

GRUPOS_PROTEINA: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "pescado",
        (
            "bacalao",
            "merluza",
            "salmon",
            "atun",
            "calamar",
            "gamba",
            "marisco",
            "sepia",
            "pescado",
        ),
    ),
    ("pollo", ("pollo", "pavo")),
    ("ternera", ("ternera", "morcillo", "buey", "vacuno")),
    (
        "cerdo",
        (
            "cerdo",
            "secreto",
            "solomillo de cerdo",
            "carrillera",
            "panceta",
            "lomo",
            "presa",
            "iberico",
        ),
    ),
    ("legumbre", ("lenteja", "garbanzo", "alubia", "fabada")),
)


def _grupo_en(texto: str) -> str | None:
    texto = db.normalizar_titulo(texto)
    for grupo, palabras in GRUPOS_PROTEINA:
        for palabra in palabras:
            if re.search(r"\b" + re.escape(db.normalizar_titulo(palabra)), texto):
                return grupo
    return None


def detectar_proteina(nombre: str, ingredientes: str | None) -> str:
    """Identifica el grupo de proteína principal del plato.

    Manda el nombre ("Lentejas a la burgalesa" es legumbre aunque lleve
    chorizo); los ingredientes solo deciden si el nombre no dice nada. En cada
    texto se comprueba en orden: pescado, pollo, ternera, cerdo, legumbre.
    """
    return _grupo_en(nombre) or _grupo_en(ingredientes or "") or "otro"


def puntuacion_plato(plato: Plato) -> float:
    """Calcula la puntuación nutricional de un plato.

    Puntuación = proteinas − 0,1 × (kcal por encima de 500) − 5 × (1 si sal > 2,5).
    """
    proteinas = plato.proteinas or 0.0
    kcal = plato.kcal or 0.0
    sal = plato.sal or 0.0

    kcal_exceso = max(0.0, kcal - 500.0)
    penalizacion_sal = 5.0 if sal > 2.5 else 0.0
    return proteinas - 0.1 * kcal_exceso - penalizacion_sal


def recomendar(platos: Iterable[Any], n: int = 8, max_por_proteina: int = 3) -> list[Plato]:
    """Propone hasta `n` platos según objetivos y variedad proteica.

    - Solo platos de categoría 'Principal', disponibles y con proteína y kcal.
    - Ordenados de forma codiciosa por puntuación nutricional.
    - Como máximo `max_por_proteina` platos del mismo grupo de proteína.
    """
    candidatos: list[Plato] = []
    for item in platos:
        p = item if isinstance(item, Plato) else Plato.desde_fila(item)
        if (
            p.categoria.strip().lower() == "principal"
            and p.disponible
            and p.proteinas is not None
            and p.kcal is not None
        ):
            candidatos.append(p)

    candidatos.sort(key=puntuacion_plato, reverse=True)

    cuenta_por_grupo: dict[str, int] = defaultdict(int)
    seleccionados: list[Plato] = []

    for plato in candidatos:
        grupo = detectar_proteina(plato.nombre, plato.ingredientes)
        if cuenta_por_grupo[grupo] < max_por_proteina:
            cuenta_por_grupo[grupo] += 1
            seleccionados.append(plato)
            if len(seleccionados) >= n:
                break

    return seleccionados
