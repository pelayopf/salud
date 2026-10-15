from salud import graficas


def test_cifra_en_castellano():
    assert graficas.cifra(12345.678, 1) == "12.345,7"
    assert graficas.cifra(7) == "7"
    assert graficas.cifra(None) == "—"


def test_ticks_redondos():
    assert graficas.ticks(41, 58) == [40, 45, 50, 55, 60]
    assert graficas.ticks(0, 870) == [0, 250, 500, 750, 1000]
    assert graficas.ticks(80.1, 80.9) == [80.0, 80.2, 80.4, 80.6, 80.8, 81.0]
    assert graficas.ticks(5, 5)[0] <= 4
    assert graficas._decimales_eje([80.0, 80.2]) == 1
    assert graficas._decimales_eje([0.0, 2.5, 5.0]) == 1
    assert graficas._decimales_eje([40.0, 45.0]) == 0


def test_sparkline():
    assert graficas.sparkline([None, 3]) == ""
    svg = graficas.sparkline([1, None, 3, 2])
    assert "<polyline" in svg and "spark-punto" in svg


def test_linea_se_corta_en_los_huecos_salvo_que_se_unan():
    puntos = [
        ("2026-01-01", 50.0),
        ("2026-01-02", 51.0),
        ("2026-01-03", None),
        ("2026-01-04", 49.0),
        ("2026-01-05", 48.0),
    ]
    g = graficas.linea(puntos, unidad="lpm", titulo="FC en reposo")
    assert g is not None
    assert g.svg.count('class="linea"') == 2
    assert g.filas[0] == ("5 ene", "48 lpm")
    assert "data-puntos" in g.svg
    unida = graficas.linea(puntos, unir_huecos=True)
    assert unida is not None and unida.svg.count('class="linea"') == 1
    assert graficas.linea([("2026-01-01", 1.0), ("2026-01-02", None)]) is None


def test_linea_con_banda():
    puntos = [(f"2026-01-0{i}", 55.0 + i) for i in range(1, 6)]
    banda = [(f, 50.0, 70.0) for f, _ in puntos]
    g = graficas.linea(puntos, banda=banda)
    assert g is not None and 'class="banda"' in g.svg


def test_columnas():
    assert graficas.columnas([("1/1", 0), ("8/1", 0)]) is None
    g = graficas.columnas([("1/1", 300), ("8/1", 800), ("15/1", 450)])
    assert g is not None
    assert g.svg.count('class="columna"') == 3
    assert 'data-tip="8/1: 800"' in g.svg
    assert g.filas[0] == ("15/1", "450")


def test_anillo_progreso_y_recorte():
    g0 = graficas.anillo(0.0, clase="preparacion", etiqueta="Preparación 0")
    assert "<svg" in g0 and 'aria-label="Preparación 0"' in g0
    assert "anillo-fondo preparacion" in g0

    g5 = graficas.anillo(0.5, clase="preparacion", etiqueta="Preparación 50")
    assert "anillo-valor preparacion" in g5
    assert 'role="img"' in g5

    g10 = graficas.anillo(1.0, clase="preparacion", etiqueta="Preparación 100")
    g13 = graficas.anillo(1.3, clase="preparacion", etiqueta="Preparación 100")
    # 1.3 se recorta a 1.0, generando el mismo trazo de valor
    assert "anillo-valor preparacion" in g13
    assert g13.count("stroke-dasharray") == g10.count("stroke-dasharray")

    gn = graficas.anillo(None, clase="preparacion", etiqueta="Sin dato")
    assert "anillo-fondo" in gn
    assert "anillo-valor" not in gn

    oculto = graficas.anillo(0.5, clase="preparacion", etiqueta="")
    assert 'aria-hidden="true"' in oculto


def test_anillos_concentricos():
    an = graficas.anillos(
        0.6, 0.4, clase_ext="proteina", clase_int="kcal", etiqueta="Comida de hoy"
    )
    assert "<svg" in an
    assert 'role="img"' in an and 'aria-label="Comida de hoy"' in an
    assert "anillo-valor proteina" in an
    assert "anillo-valor kcal" in an


def test_fases_sueno_suman_ancho_total():
    import re

    fs = graficas.fases_sueno(45.0, 90.0, 200.0, 15.0)
    assert "<svg" in fs
    assert 'aria-hidden="true"' in fs
    anchos = [float(w) for w in re.findall(r'<rect [^>]*width="([\d.]+)"', fs)]
    assert round(sum(anchos), 1) == 100.0
    assert graficas.fases_sueno(None, None, None, None) == ""


def test_rango_dentro_y_fuera():
    r_dentro = graficas.rango(50.0, 45.0, 60.0)
    assert "<svg" in r_dentro
    assert 'aria-hidden="true"' in r_dentro or "aria-label" in r_dentro
    assert "rango-punto dentro" in r_dentro

    r_fuera_bajo = graficas.rango(39.0, 45.0, 60.0)
    assert "rango-punto fuera" in r_fuera_bajo

    r_fuera_alto = graficas.rango(72.0, 45.0, 60.0)
    assert "rango-punto fuera" in r_fuera_alto

    assert graficas.rango(50.0, None, 60.0) == ""


def test_linea_con_referencia_y_marcas():
    puntos = [
        ("2026-10-01", 90.0),
        ("2026-10-02", 89.5),
        ("2026-10-03", 89.0),
    ]
    # Referencia a 85 (menor que el mínimo de puntos 89, abre el eje para que se vea)
    g = graficas.linea(
        puntos,
        referencia=85.0,
        marcas=["2026-10-02", "2026-10-15"],  # Una dentro del periodo y otra fuera
    )
    assert g is not None
    # El SVG contiene la línea de referencia y su etiqueta
    assert 'class="linea-referencia"' in g.svg
    assert 'class="eje-referencia"' in g.svg
    assert "85" in g.svg

    # El SVG contiene marcas para la fecha dentro del periodo (2026-10-02)
    assert 'class="marca-linea"' in g.svg
    assert 'class="marca-eje"' in g.svg
    assert "<title>Tratamiento</title>" in g.svg

    # La fecha fuera de periodo (2026-10-15) no genera marcas adicionales
    assert g.svg.count('class="marca-linea"') == 1
    assert g.svg.count('class="marca-eje"') == 1
