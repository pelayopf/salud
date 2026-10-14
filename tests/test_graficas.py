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
