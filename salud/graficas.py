"""Gráficas en SVG hechas en el servidor (sin librerías en el navegador).

Todas son de una sola serie, así que no llevan leyenda: el título de la
tarjeta dice qué se ve. Línea de 2 px, punto final con anillo del color del
fondo, rejilla de un pelo y sin números en cada punto: el valor final va
rotulado y el resto está en el tooltip y en la tabla. Los colores salen de
las variables CSS (`--serie`, `--serie-suave`...).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from markupsafe import Markup, escape

MESES_CORTOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def cifra(valor: float | None, decimales: int = 0) -> str:
    """Número en castellano: 1.234,5 (y una raya si no hay valor)."""
    if valor is None:
        return "—"
    texto = f"{valor:,.{decimales}f}"
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def ticks(minimo: float, maximo: float, n: int = 4) -> list[float]:
    """Marcas redondas del eje (1, 2, 2,5 o 5 por una potencia de 10)."""
    if maximo <= minimo:
        minimo, maximo = minimo - 1, maximo + 1
    bruto = (maximo - minimo) / n
    magnitud = 10 ** math.floor(math.log10(bruto))
    paso = magnitud * 10
    for m in (1, 2, 2.5, 5, 10):
        if m * magnitud >= bruto * (1 - 1e-9):  # tolerancia de coma flotante
            paso = m * magnitud
            break
    inicio = math.floor(minimo / paso) * paso
    fin = math.ceil(maximo / paso) * paso
    return [round(inicio + i * paso, 6) for i in range(round((fin - inicio) / paso) + 1)]


def _decimales_eje(marcas: list[float]) -> int:
    """Los decimales justos para escribir todas las marcas (80, 80,2 u 80,25)."""
    return next((d for d in range(3) if all(round(m, d) == m for m in marcas)), 3)


def sparkline(valores: Sequence[float | None], ancho: int = 112, alto: int = 30) -> Markup:
    """Mini-gráfica de tendencia para una tarjeta (el valor ya va escrito al lado)."""
    puntos = [(i, v) for i, v in enumerate(valores) if v is not None]
    if len(puntos) < 2:
        return Markup("")
    margen = 5
    lo, hi = min(v for _, v in puntos), max(v for _, v in puntos)
    rango = (hi - lo) or 1
    paso = (ancho - 2 * margen) / max(len(valores) - 1, 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        return margen + i * paso, alto - margen - (v - lo) / rango * (alto - 2 * margen)

    trazo = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in puntos))
    fx, fy = xy(*puntos[-1])
    return Markup(
        f'<svg class="spark" viewBox="0 0 {ancho} {alto}" width="{ancho}" height="{alto}"'
        ' aria-hidden="true" focusable="false">'
        f'<polyline class="spark-linea" points="{trazo}"/>'
        f'<circle class="spark-punto" cx="{fx:.1f}" cy="{fy:.1f}" r="3.5"/></svg>'
    )


def anillo(
    fraccion: float | None,
    *,
    clase: str,
    tamano: int = 76,
    grosor: int = 8,
    texto: str = "",
    subtexto: str = "",
    etiqueta: str,
) -> Markup:
    """Anillo de progreso (0–1, recortado a [0, 1]; None = solo el fondo)."""
    c = tamano / 2
    r = (tamano - grosor) / 2
    circ = 2 * math.pi * r
    f = max(0.0, min(1.0, float(fraccion))) if fraccion is not None else 0.0

    partes = [
        f'<circle class="anillo-fondo {escape(clase)}" cx="{c:.1f}" cy="{c:.1f}"'
        f' r="{r:.1f}" stroke-width="{grosor}"/>'
    ]
    if fraccion is not None and f > 0:
        arco = f * circ
        partes.append(
            f'<circle class="anillo-valor {escape(clase)}" cx="{c:.1f}" cy="{c:.1f}" r="{r:.1f}"'
            f' stroke-width="{grosor}" stroke-linecap="round"'
            f' stroke-dasharray="{arco:.1f} {circ:.1f}" transform="rotate(-90 {c:.1f} {c:.1f})"/>'
        )
    if texto and subtexto:
        partes.append(
            f'<text class="anillo-texto {escape(clase)}" x="{c:.1f}" y="{c - 2:.1f}"'
            f' text-anchor="middle">{escape(texto)}</text>'
        )
        partes.append(
            f'<text class="anillo-subtexto {escape(clase)}" x="{c:.1f}" y="{c + 13:.1f}"'
            f' text-anchor="middle">{escape(subtexto)}</text>'
        )
    elif texto:
        partes.append(
            f'<text class="anillo-texto {escape(clase)}" x="{c:.1f}" y="{c + 6:.1f}"'
            f' text-anchor="middle">{escape(texto)}</text>'
        )
    elif subtexto:
        partes.append(
            f'<text class="anillo-subtexto {escape(clase)}" x="{c:.1f}" y="{c + 4:.1f}"'
            f' text-anchor="middle">{escape(subtexto)}</text>'
        )

    aria = f'role="img" aria-label="{escape(etiqueta)}"' if etiqueta else 'aria-hidden="true"'
    return Markup(
        f'<svg class="anillo {escape(clase)}" width="{tamano}" height="{tamano}"'
        f' viewBox="0 0 {tamano} {tamano}" {aria}>' + "".join(partes) + "</svg>"
    )


def anillos(
    exterior: float | None,
    interior: float | None,
    *,
    clase_ext: str,
    clase_int: str,
    tamano: int = 64,
    texto: str = "",
    subtexto: str = "",
    etiqueta: str,
) -> Markup:
    """Dos anillos concéntricos (exterior e interior)."""
    c = tamano / 2
    if tamano >= 100:
        g_ext = 12.0
        g_int = 8.0
        r_ext = 56.0
        r_int = 39.0
    else:
        g_ext = max(2.0, tamano * 7.0 / 64.0)
        g_int = max(2.0, tamano * 5.0 / 64.0)
        r_ext = tamano * 26.0 / 64.0
        r_int = tamano * 17.0 / 64.0
    circ_ext = 2 * math.pi * r_ext
    circ_int = 2 * math.pi * r_int

    f_ext = max(0.0, min(1.0, float(exterior))) if exterior is not None else 0.0
    f_int = max(0.0, min(1.0, float(interior))) if interior is not None else 0.0

    partes = [
        f'<circle class="anillo-fondo {escape(clase_ext)}" cx="{c:.1f}" cy="{c:.1f}"'
        f' r="{r_ext:.1f}" stroke-width="{g_ext:.1f}"/>'
    ]
    if exterior is not None and f_ext > 0:
        arco_ext = f_ext * circ_ext
        partes.append(
            f'<circle class="anillo-valor {escape(clase_ext)}" cx="{c:.1f}" cy="{c:.1f}"'
            f' r="{r_ext:.1f}" stroke-width="{g_ext:.1f}" stroke-linecap="round"'
            f' stroke-dasharray="{arco_ext:.1f} {circ_ext:.1f}"'
            f' transform="rotate(-90 {c:.1f} {c:.1f})"/>'
        )
    partes.append(
        f'<circle class="anillo-fondo {escape(clase_int)}" cx="{c:.1f}" cy="{c:.1f}"'
        f' r="{r_int:.1f}" stroke-width="{g_int:.1f}"/>'
    )
    if interior is not None and f_int > 0:
        arco_int = f_int * circ_int
        partes.append(
            f'<circle class="anillo-valor {escape(clase_int)}" cx="{c:.1f}" cy="{c:.1f}"'
            f' r="{r_int:.1f}" stroke-width="{g_int:.1f}" stroke-linecap="round"'
            f' stroke-dasharray="{arco_int:.1f} {circ_int:.1f}"'
            f' transform="rotate(-90 {c:.1f} {c:.1f})"/>'
        )

    if texto and subtexto:
        y_txt = 64.0 if tamano >= 100 else c - 2.0
        y_sub = 82.0 if tamano >= 100 else c + 11.0
        partes.append(
            f'<text class="anillo-texto" x="{c:.1f}" y="{y_txt:.1f}"'
            f' text-anchor="middle">{escape(texto)}</text>'
        )
        partes.append(
            f'<text class="anillo-subtexto" x="{c:.1f}" y="{y_sub:.1f}"'
            f' text-anchor="middle">{escape(subtexto)}</text>'
        )
    elif texto:
        y_txt = 74.0 if tamano >= 100 else c + 5.0
        partes.append(
            f'<text class="anillo-texto" x="{c:.1f}" y="{y_txt:.1f}"'
            f' text-anchor="middle">{escape(texto)}</text>'
        )
    elif subtexto:
        y_sub = 74.0 if tamano >= 100 else c + 3.0
        partes.append(
            f'<text class="anillo-subtexto" x="{c:.1f}" y="{y_sub:.1f}"'
            f' text-anchor="middle">{escape(subtexto)}</text>'
        )

    aria = f'role="img" aria-label="{escape(etiqueta)}"' if etiqueta else 'aria-hidden="true"'
    return Markup(
        f'<svg class="anillos" width="{tamano}" height="{tamano}"'
        f' viewBox="0 0 {tamano} {tamano}" {aria}>' + "".join(partes) + "</svg>"
    )


def fases_sueno(
    profundo: float | None,
    rem: float | None,
    ligero: float | None,
    despierto: float | None,
) -> Markup:
    """Barra horizontal de 6 px partida en tramos proporcionales."""
    fases = [
        ("fase-profundo", profundo),
        ("fase-rem", rem),
        ("fase-ligero", ligero),
        ("fase-despierto", despierto),
    ]
    validas = [(clase, v) for clase, v in fases if v is not None and v > 0]
    if not validas:
        return Markup("")

    total = sum(v for _, v in validas)
    if total <= 0:
        return Markup("")

    ancho = 100.0
    alto = 6
    partes: list[str] = []
    x = 0.0
    for i, (clase, v) in enumerate(validas):
        if i == len(validas) - 1:
            w = ancho - x
        else:
            w = round((v / total) * ancho, 2)
        partes.append(f'<rect class="{clase}" x="{x:.2f}" y="0" width="{w:.2f}" height="{alto}"/>')
        x += w

    return Markup(
        f'<svg class="fases-sueno" width="100%" height="{alto}" viewBox="0 0 {int(ancho)} {alto}"'
        f' preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        + "".join(partes)
        + "</svg>"
    )


def rango(valor: float | None, bajo: float | None, alto: float | None) -> Markup:
    """Barra con la franja [bajo, alto] y un punto en el valor."""
    if bajo is None or alto is None:
        return Markup("")

    ancho = 140
    alto_svg = 22
    vals = [bajo, alto]
    if valor is not None:
        vals.append(valor)
    v_min = min(vals)
    v_max = max(vals)
    delta = v_max - v_min
    if delta <= 0:
        delta = 1.0

    margen = 0.20 * delta
    e_min = v_min - margen
    e_max = v_max + margen
    e_rango = e_max - e_min

    def x_pos(v: float) -> float:
        return (v - e_min) / e_rango * ancho

    x_bajo = max(0.0, x_pos(bajo))
    x_alto = min(float(ancho), x_pos(alto))
    w_franja = max(0.0, x_alto - x_bajo)

    partes = [
        f'<rect class="rango-fondo" x="0" y="6" width="{ancho}" height="10" rx="5"/>',
        f'<rect class="rango-franja" x="{x_bajo:.1f}" y="6" width="{w_franja:.1f}"'
        f' height="10" rx="5"/>',
    ]

    if valor is not None:
        cx = max(0.0, min(float(ancho), x_pos(valor)))
        clase_punto = "dentro" if bajo <= valor <= alto else "fuera"
        partes.append(f'<circle class="rango-punto {clase_punto}" cx="{cx:.1f}" cy="11" r="6"/>')

    return Markup(
        f'<svg class="rango" width="100%" height="{alto_svg}" viewBox="0 0 {ancho} {alto_svg}"'
        f' preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        + "".join(partes)
        + "</svg>"
    )


@dataclass(frozen=True)
class Grafica:
    """Una gráfica lista para la plantilla: el SVG y las filas de su tabla."""

    svg: Markup
    filas: list[tuple[str, str]]


def fecha_corta(fecha: str) -> str:
    """ "2026-09-09" -> "9 sep"."""
    d = date.fromisoformat(fecha)
    return f"{d.day} {MESES_CORTOS[d.month - 1]}"


def linea(
    puntos: Sequence[tuple[str, float | None]],
    *,
    decimales: int = 0,
    unidad: str = "",
    banda: Sequence[tuple[str, float | None, float | None]] | None = None,
    unir_huecos: bool = False,
    rango_minimo: float = 0,
    ancho: int = 400,
    alto: int = 190,
    titulo: str = "",
    referencia: float | None = None,
    marcas: Sequence[str] = (),
) -> Grafica | None:
    """Línea de una serie diaria con eje, rejilla, punto final, tooltip y tabla.

    `puntos` lleva todos los días del periodo (None si ese día no hay dato).
    Con `unir_huecos` la línea salta los días sin dato (el peso no es diario);
    si no, se corta. `banda` dibuja una franja de referencia (la VFC equilibrada).
    `rango_minimo` abre el eje para que un cambio pequeño no parezca un salto.
    `referencia` dibuja una línea horizontal discontinua y abre el eje.
    `marcas` añade marcas y líneas verticales en las fechas indicadas dentro del periodo.
    """
    con_valor = [(i, f, v) for i, (f, v) in enumerate(puntos) if v is not None]
    if len(con_valor) < 2:
        return None
    izq, der, arriba, abajo = 40, 58, 12, 26
    valores = [v for _, _, v in con_valor]
    if banda:
        valores += [x for _, lo, hi in banda for x in (lo, hi) if x is not None]
    if referencia is not None:
        valores.append(referencia)
    minimo, maximo = min(valores), max(valores)
    if maximo - minimo < rango_minimo:
        centro = (maximo + minimo) / 2
        minimo, maximo = centro - rango_minimo / 2, centro + rango_minimo / 2
    marcas_eje = ticks(minimo, maximo)
    lo, hi = marcas_eje[0], marcas_eje[-1]
    ancho_util, alto_util = ancho - izq - der, alto - arriba - abajo
    paso = ancho_util / max(len(puntos) - 1, 1)

    def x(i: int) -> float:
        return izq + i * paso

    def y(v: float) -> float:
        return arriba + alto_util - (v - lo) / (hi - lo) * alto_util

    partes: list[str] = []
    dec_eje = _decimales_eje(marcas_eje)
    for m in marcas_eje:
        partes.append(
            f'<line class="rejilla" x1="{izq}" x2="{ancho - der}" y1="{y(m):.1f}" y2="{y(m):.1f}"/>'
            f'<text class="eje" x="{izq - 6}" y="{y(m) + 4:.1f}" text-anchor="end">'
            f"{cifra(m, dec_eje)}</text>"
        )
    if referencia is not None:
        y_ref = y(referencia)
        dec_ref = 1 if (referencia % 1 != 0) else 0
        partes.append(
            f'<line class="linea-referencia" x1="{izq}" x2="{ancho - der}"'
            f' y1="{y_ref:.1f}" y2="{y_ref:.1f}" stroke-dasharray="4 4"/>'
            f'<text class="eje-referencia" x="{ancho - der - 2}" y="{y_ref - 4:.1f}"'
            f' text-anchor="end">{cifra(referencia, dec_ref)}</text>'
        )
    if marcas:
        indice_fechas = {f: i for i, (f, _) in enumerate(puntos)}
        base_y = arriba + alto_util
        for f_m in marcas:
            if f_m in indice_fechas:
                idx_m = indice_fechas[f_m]
                xm = x(idx_m)
                partes.append(
                    f'<line class="marca-linea" x1="{xm:.1f}" x2="{xm:.1f}"'
                    f' y1="{arriba}" y2="{base_y:.1f}"/>'
                    f'<line class="marca-eje" x1="{xm:.1f}" x2="{xm:.1f}"'
                    f' y1="{base_y:.1f}" y2="{base_y + 4:.1f}"/>'
                )
    # Etiquetas del eje X: el día 1 de cada mes (o cada semana si el periodo es corto).
    for i, (f, _) in enumerate(puntos):
        d = date.fromisoformat(f)
        if (d.day == 1 if len(puntos) > 45 else d.weekday() == 0) and 0 < i < len(puntos) - 1:
            partes.append(
                f'<text class="eje" x="{x(i):.1f}" y="{alto - 8}" text-anchor="middle">'
                f"{fecha_corta(f)}</text>"
            )
    if banda:
        tramo = [(i, lo_, hi_) for i, (_, lo_, hi_) in enumerate(banda) if lo_ and hi_]
        if len(tramo) >= 2:
            ida = " ".join(f"{x(i):.1f},{y(h):.1f}" for i, _, h in tramo)
            vuelta = " ".join(f"{x(i):.1f},{y(lo_):.1f}" for i, lo_, _ in reversed(tramo))
            partes.append(f'<polygon class="banda" points="{ida} {vuelta}"/>')
    # La línea, cortada en los huecos (salvo `unir_huecos`).
    tramos: list[list[tuple[int, float]]] = [[]]
    anterior = None
    for i, _, v in con_valor:
        if not unir_huecos and anterior is not None and i != anterior + 1:
            tramos.append([])
        tramos[-1].append((i, v))
        anterior = i
    for t in tramos:
        if len(t) == 1:
            i, v = t[0]
            partes.append(f'<circle class="punto-suelto" cx="{x(i):.1f}" cy="{y(v):.1f}" r="2"/>')
        elif t:
            trazo = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in t)
            partes.append(f'<polyline class="linea" points="{trazo}"/>')
    ui, uf, uv = con_valor[-1]
    sufijo = f" {unidad}" if unidad else ""
    partes.append(f'<circle class="punto-final" cx="{x(ui):.1f}" cy="{y(uv):.1f}" r="4"/>')
    partes.append(
        f'<text class="valor-final" x="{x(ui) + 8:.1f}" y="{y(uv) + 4:.1f}">'
        f"{cifra(uv, decimales)}</text>"
    )
    datos = [
        [round(x(i), 1), round(y(v), 1), fecha_corta(f), f"{cifra(v, decimales)}{sufijo}"]
        for i, f, v in con_valor
    ]
    svg = Markup(
        f'<svg class="grafica" viewBox="0 0 {ancho} {alto}" role="img"'
        f' aria-label="{escape(titulo)}: último valor {cifra(uv, decimales)}{sufijo}"'
        f" data-puntos='{escape(json.dumps(datos, ensure_ascii=False))}'"
        f' data-alto="{alto}" data-arriba="{arriba}" data-abajo="{abajo}">'
        + "".join(partes)
        + '<line class="cursor" x1="0" x2="0" y1="0" y2="0" visibility="hidden"/>'
        '<circle class="cursor-punto" cx="0" cy="0" r="4" visibility="hidden"/></svg>'
    )
    filas = [(fecha_corta(f), f"{cifra(v, decimales)}{sufijo}") for _, f, v in reversed(con_valor)]
    return Grafica(svg, filas)


def columnas(
    barras: Sequence[tuple[str, float]],
    *,
    unidad: str = "",
    ancho: int = 400,
    alto: int = 180,
    titulo: str = "",
    etiquetas_cada: int = 2,
) -> Grafica | None:
    """Columnas de una serie (la carga de cada semana), con el valor en la última y la mayor.

    En el eje X sale una etiqueta cada `etiquetas_cada` columnas, contando desde la última.
    """
    if not barras or not any(v for _, v in barras):
        return None
    izq, der, arriba, abajo = 40, 12, 18, 26
    marcas = ticks(0, max(v for _, v in barras))
    hi = marcas[-1]
    ancho_util, alto_util = ancho - izq - der, alto - arriba - abajo
    hueco = ancho_util / len(barras)
    grosor = min(24.0, hueco - 2)
    base = arriba + alto_util
    maximo = max(range(len(barras)), key=lambda i: barras[i][1])
    sufijo = f" {unidad}" if unidad else ""
    partes: list[str] = []
    for m in marcas:
        ym = base - m / hi * alto_util
        partes.append(
            f'<line class="rejilla" x1="{izq}" x2="{ancho - der}" y1="{ym:.1f}" y2="{ym:.1f}"/>'
            f'<text class="eje" x="{izq - 6}" y="{ym + 4:.1f}" text-anchor="end">'
            f"{cifra(m)}</text>"
        )
    for i, (etiqueta, v) in enumerate(barras):
        cx = izq + hueco * (i + 0.5)
        h = v / hi * alto_util
        x0, x1, top = cx - grosor / 2, cx + grosor / 2, base - h
        r = min(4.0, h)
        if h > 0:
            # Esquinas redondeadas arriba y rectas en la base.
            trazo = (
                f"M{x0:.1f},{base:.1f} V{top + r:.1f} Q{x0:.1f},{top:.1f} {x0 + r:.1f},{top:.1f}"
                f" H{x1 - r:.1f} Q{x1:.1f},{top:.1f} {x1:.1f},{top + r:.1f} V{base:.1f} Z"
            )
            partes.append(
                f'<path class="columna" d="{trazo}" tabindex="0"'
                f' data-tip="{escape(etiqueta)}: {cifra(v)}{sufijo}"/>'
            )
        if i in (maximo, len(barras) - 1) and v:
            partes.append(
                f'<text class="valor-final" x="{cx:.1f}" y="{top - 5:.1f}" text-anchor="middle">'
                f"{cifra(v)}</text>"
            )
        if (len(barras) - 1 - i) % etiquetas_cada == 0:
            partes.append(
                f'<text class="eje" x="{cx:.1f}" y="{alto - 8}" text-anchor="middle">'
                f"{escape(etiqueta)}</text>"
            )
    svg = Markup(
        f'<svg class="grafica" viewBox="0 0 {ancho} {alto}" role="img"'
        f' aria-label="{escape(titulo)}">' + "".join(partes) + "</svg>"
    )
    filas = [(e, f"{cifra(v)}{sufijo}") for e, v in reversed(barras)]
    return Grafica(svg, filas)
