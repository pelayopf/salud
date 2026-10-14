from conftest import actividad, estado, ms_local, preparacion, resumen, sueno, vfc

from salud import metricas
from salud.metricas import Metrica


def _por_clave(filas: list[Metrica]) -> dict[str, Metrica]:
    return {m.clave: m for m in filas}


def test_resumen_saca_las_cifras_del_dia():
    m = _por_clave(metricas.de_resumen(resumen("2026-01-10", pasos=12000, fc=47, bb=85)))
    assert m["pasos"].valor == 12000
    assert m["fc_reposo"].valor == 47
    assert m["bb_despertar"].valor == 85
    assert m["min_intensidad"].valor == 50  # 10 moderados + 2 × 20 intensos
    assert all(x.fecha == "2026-01-10" and x.fuente == "garmin" for x in m.values())


def test_resumen_sin_datos_no_guarda_nada_y_estres_negativo_no_cuenta():
    assert metricas.de_resumen({"calendarDate": "2026-01-10"}) == []
    datos = resumen("2026-01-10") | {"averageStressLevel": -2}
    assert "estres" not in _por_clave(metricas.de_resumen(datos))


def test_sueno_en_horas_y_hora_de_dormirse_pasada_la_medianoche():
    m = _por_clave(metricas.de_sueno(sueno("2026-01-10", horas=7.5, puntuacion=81)))
    assert m["sueno_h"].valor == 7.5
    assert m["sueno_profundo_h"].valor == 1.5
    assert m["sueno_puntuacion"].valor == 81
    assert m["sueno_puntuacion"].texto == "GOOD"
    assert (m["sueno_inicio"].valor, m["sueno_inicio"].texto) == (23.5, "23:30")
    assert (m["sueno_fin"].valor, m["sueno_fin"].texto) == (7.25, "07:15")

    datos = sueno("2026-01-10")
    datos["dailySleepDTO"]["sleepStartTimestampLocal"] = ms_local("2026-01-10", "00:45")
    assert _por_clave(metricas.de_sueno(datos))["sueno_inicio"].valor == 24.75
    assert metricas.de_sueno({"dailySleepDTO": {"calendarDate": "2026-01-10"}}) == []


def test_vfc_y_preparacion():
    m = _por_clave(metricas.de_vfc(vfc("2026-01-10", noche=61)))
    assert (m["vfc_noche"].valor, m["vfc_noche"].texto) == (61, "BALANCED")
    assert (m["vfc_base_baja"].valor, m["vfc_base_alta"].valor) == (50, 70)
    assert metricas.de_vfc(None) == []

    p = _por_clave(metricas.de_preparacion(preparacion("2026-01-10", score=64)))
    assert (p["preparacion"].valor, p["preparacion"].texto) == (64, "MODERATE")
    assert p["recuperacion_h"].valor == 10
    assert p["preparacion_motivo"] == Metrica(
        "2026-01-10", "preparacion_motivo", "garmin", None, "WELL_RECOVERED"
    )


def test_estado_de_entreno_usa_el_dispositivo_principal_y_el_vo2max_va_con_su_fecha():
    filas = metricas.de_estado_entreno(estado("2026-01-10"))
    m = _por_clave(filas)
    assert m["estado_entreno"].texto == "PRODUCTIVE_3"
    assert m["carga_aguda"].valor == 500
    assert m["ratio_carga"].valor == 1.1
    assert (m["vo2max"].fecha, m["vo2max"].valor) == ("2026-01-03", 47.6)
    assert m["vo2max_ciclismo"].valor == 48.0
    assert metricas.de_estado_entreno({}) == []


def test_composicion_se_queda_con_el_ultimo_pesaje_del_dia():
    datos = {
        "dateWeightList": [
            {"calendarDate": "2026-01-10", "date": 2, "weight": 80100.0, "muscleMass": 36000},
            {"calendarDate": "2026-01-10", "date": 1, "weight": 80900.0},
            {"calendarDate": "2026-01-09", "date": 0, "weight": 81000.0, "bodyFat": 21.0},
            {"calendarDate": "2026-01-08", "date": 0, "weight": None},
        ]
    }
    filas = metricas.de_composicion(datos)
    pesos = {(m.fecha, m.valor) for m in filas if m.clave == "peso"}
    assert pesos == {("2026-01-10", 80.1), ("2026-01-09", 81.0)}
    assert ("musculo", 36.0) in {(m.clave, m.valor) for m in filas}


def test_ftp_umbral_y_actividad():
    assert metricas.de_ftp(
        {"calendarDate": "2026-01-02T10:00:00.0", "functionalThresholdPower": 230}
    ) == [Metrica("2026-01-02", "ftp", "garmin", 230.0)]
    assert metricas.de_ftp([]) == []
    assert (
        metricas.de_umbral({"speed_and_heart_rate": {"heartRateCycling": 165}}, "2026-01-10")[
            0
        ].valor
        == 165
    )

    a = metricas.de_actividad(actividad(7, "2026-01-10 18:00:00", "mountain_biking"))
    assert a is not None
    assert (a.id_fuente, a.fecha, a.tipo, a.carga) == ("7", "2026-01-10", "mountain_biking", 120.0)
    assert metricas.de_actividad({"activityName": "sin id"}) is None


def test_textos_en_castellano():
    assert metricas.texto("estado_entreno", "PRODUCTIVE_6") == "productivo"
    assert metricas.texto("preparacion", "POOR") == "mala"
    assert metricas.texto("sueno_puntuacion", "POOR") == "malo"
    assert metricas.texto("vfc_noche", "UNBALANCED") == "desequilibrada"
    assert metricas.texto("preparacion_motivo", "BOOSTED_BY_NAPPING") == "boosted by napping"
    assert metricas.texto("x", None) == ""
    assert metricas.nombre_tipo("paddelball") == "Pádel"
    assert metricas.nombre_tipo("e_bike_fitness") == "E bike fitness"
