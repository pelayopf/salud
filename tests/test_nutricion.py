"""Tests del cálculo dinámico de objetivos de comida y nutrición."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from salud import db, nutricion
from salud.metricas import Actividad, Metrica


def crear_actividad(
    id_fuente: str,
    fecha: str,
    tipo: str,
    kcal: float | None = None,
    inicio: str | None = None,
    nombre: str = "Test",
    duracion_s: float | None = 3600.0,
) -> Actividad:
    hora = inicio or f"{fecha} 10:00:00"
    return Actividad(
        fuente="garmin",
        id_fuente=id_fuente,
        inicio=hora,
        tipo=tipo,
        nombre=nombre,
        bruto="{}",
        duracion_s=duracion_s,
        kcal=kcal,
    )


def guardar_metrica(con, fecha: str, clave: str, valor: float) -> None:
    db.guardar_metricas(con, [Metrica(fecha=fecha, clave=clave, fuente="garmin", valor=valor)])


def test_basal_28_dias_completos(con):
    hoy = date(2026, 1, 29)
    # 28 días completos anteriores con kcal 2600 y activas 330 -> 2270
    for i in range(1, 29):
        f = (hoy - timedelta(days=i)).isoformat()
        guardar_metrica(con, f, "kcal", 2600.0)
        guardar_metrica(con, f, "kcal_activas", 330.0)

    # Hoy tiene valores distintos pero debe ignorarse
    guardar_metrica(con, hoy.isoformat(), "kcal", 5000.0)
    guardar_metrica(con, hoy.isoformat(), "kcal_activas", 1000.0)

    # Un día antiguo con solo uno de los dos valores no debe romper nada
    f_antigua = (hoy - timedelta(days=30)).isoformat()
    guardar_metrica(con, f_antigua, "kcal", 2600.0)

    assert nutricion.basal(con, hoy) == 2270


def test_basal_sin_datos_o_incompleto(con):
    hoy = date(2026, 1, 29)
    assert nutricion.basal(con, hoy) is None

    # Solo kcal pero sin kcal_activas
    f = (hoy - timedelta(days=1)).isoformat()
    guardar_metrica(con, f, "kcal", 2600.0)
    assert nutricion.basal(con, hoy) is None


def test_neat_calculo_y_no_negativo(con):
    hoy = date(2026, 1, 29)
    # Días con activas 1000 y una actividad de 800 kcal -> 200
    for i in range(1, 10):
        f = (hoy - timedelta(days=i)).isoformat()
        guardar_metrica(con, f, "kcal_activas", 1000.0)
        db.guardar_actividades(con, [crear_actividad(f"act-{i}", f, "running", kcal=800.0)])

    assert nutricion.neat(con, hoy) == 200

    # Días donde las actividades superan las calorías activas: neat nunca negativo
    for i in range(1, 15):
        f = (hoy - timedelta(days=i)).isoformat()
        guardar_metrica(con, f, "kcal_activas", 300.0)
        db.guardar_actividades(con, [crear_actividad(f"act-sup-{i}", f, "cycling", kcal=600.0)])

    assert nutricion.neat(con, hoy) >= 0


def test_neat_sin_datos(con):
    hoy = date(2026, 1, 29)
    assert nutricion.neat(con, hoy) == 0


def test_kcal_tipica_historial(con):
    hoy = date(2026, 1, 29)
    # Historial de soccer con 700, 900, 800 -> mediana 800
    db.guardar_actividades(
        con,
        [
            crear_actividad("s1", "2026-01-10", "soccer", kcal=700.0),
            crear_actividad("s2", "2026-01-15", "soccer", kcal=900.0),
            crear_actividad("s3", "2026-01-20", "soccer", kcal=800.0),
        ],
    )
    p = db.Planificado(
        fecha="2026-01-30",
        inicio="19:00",
        duracion_min=90,
        titulo="Partido fútbol",
        tipo="soccer",
        intensidad="alta",
        origen="google",
    )
    assert nutricion.kcal_tipica(con, p, hoy) == 800


def test_kcal_tipica_sin_historial(con):
    hoy = date(2026, 1, 29)
    # Sin historial, intensidad alta, 60 min -> 60 * 10 = 600
    p1 = db.Planificado(
        fecha="2026-01-30",
        inicio="19:00",
        duracion_min=60,
        titulo="Pádel",
        tipo="paddelball",
        intensidad="alta",
        origen="google",
    )
    assert nutricion.kcal_tipica(con, p1, hoy) == 600

    # Sin duración -> 60 min por defecto * 10 = 600
    p2 = db.Planificado(
        fecha="2026-01-30",
        inicio=None,
        duracion_min=None,
        titulo="Pádel",
        tipo="paddelball",
        intensidad="alta",
        origen="google",
    )
    assert nutricion.kcal_tipica(con, p2, hoy) == 600


def _preparar_historial_basal_y_neat(con, hoy: date, b_kcal: int = 2270, neat_kcal: int = 230):
    for i in range(1, 29):
        f = (hoy - timedelta(days=i)).isoformat()
        guardar_metrica(con, f, "kcal", float(b_kcal + neat_kcal))
        guardar_metrica(con, f, "kcal_activas", float(neat_kcal))


def test_objetivo_dia_futuro_descanso(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2270, neat_kcal=230)
    # Peso 88 en Garmin
    guardar_metrica(con, hoy.isoformat(), "peso", 88.0)

    futuro = date(2026, 1, 12)
    obj = nutricion.objetivo_dia(con, futuro, hoy)
    assert obj is not None
    # basal 2270 + neat 230 − 250 = 2250 kcal
    assert obj.total.kcal == 2250
    assert obj.total.proteinas == 141
    assert obj.total.grasas == 79
    assert obj.total.hidratos == 244
    assert obj.peso_kg == 88.0
    assert obj.desglose == [("Basal (Garmin)", 2270), ("Día normal", 230), ("Déficit", -250)]
    assert sum(v for _, v in obj.desglose) == 2250

    # Reparto por momento: comida es 35% de 2250 = 788 kcal
    assert "comida" in obj.por_momento
    assert obj.por_momento["comida"].kcal == round(2250 * 0.35)
    assert obj.por_momento["comida"].proteinas == round(141 * 0.35)
    assert obj.por_momento["comida"].hidratos == round(244 * 0.35)
    assert obj.por_momento["comida"].grasas == round(79 * 0.35)


def test_objetivo_dia_hoy_con_padel_no_hecho(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2270, neat_kcal=230)
    guardar_metrica(con, hoy.isoformat(), "peso", 88.0)

    # Historial de pádel con típica 710
    db.guardar_actividades(
        con,
        [crear_actividad("padel-1", "2026-01-05", "paddelball", kcal=710.0)],
    )

    # Planificado de pádel hoy a las 20:00
    db.reemplazar_planificado(
        con,
        origen="google",
        desde=hoy.isoformat(),
        hasta=hoy.isoformat(),
        filas=[
            db.Planificado(
                fecha=hoy.isoformat(),
                inicio="20:00",
                duracion_min=60,
                titulo="Pádel",
                tipo="paddelball",
                intensidad="alta",
                origen="google",
            )
        ],
    )

    obj = nutricion.objetivo_dia(con, hoy, hoy)
    assert obj is not None
    # basal 2270 + neat 230 + 710 - 250 = 2960
    assert obj.total.kcal == 2960
    desglose_dict = dict(obj.desglose)
    assert "Basal (Garmin)" in desglose_dict
    assert "Día normal" in desglose_dict
    assert "Pádel 20:00 (previsto)" in desglose_dict
    assert desglose_dict["Pádel 20:00 (previsto)"] == 710


def test_objetivo_dia_hoy_con_padel_ya_hecho(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2270, neat_kcal=230)
    guardar_metrica(con, hoy.isoformat(), "peso", 88.0)

    # Planificado de pádel
    db.reemplazar_planificado(
        con,
        origen="google",
        desde=hoy.isoformat(),
        hasta=hoy.isoformat(),
        filas=[
            db.Planificado(
                fecha=hoy.isoformat(),
                inicio="20:00",
                duracion_min=60,
                titulo="Pádel",
                tipo="paddelball",
                intensidad="alta",
                origen="google",
            )
        ],
    )

    # Actividad de pádel ya realizada hoy de 714 kcal
    db.guardar_actividades(
        con,
        [crear_actividad("act-hoy-1", f"{hoy.isoformat()} 11:00:00", "paddelball", kcal=714.0)],
    )

    obj = nutricion.objetivo_dia(con, hoy, hoy)
    assert obj is not None
    # No se cuenta dos veces (no hay previsto, solo hecho)
    desglose_conceptos = [c for c, _ in obj.desglose]
    assert any("(hecho)" in c for c in desglose_conceptos)
    assert not any("(previsto)" in c for c in desglose_conceptos)
    desglose_dict = dict(obj.desglose)
    assert desglose_dict["Pádel (hecho)"] == 714


def test_objetivo_dia_hoy_real_supera_estimado(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2270, neat_kcal=230)
    guardar_metrica(con, hoy.isoformat(), "peso", 88.0)
    # kcal_activas de hoy registradas hasta ahora: 1500 (muy por encima de NEAT 230)
    guardar_metrica(con, hoy.isoformat(), "kcal_activas", 1500.0)

    obj = nutricion.objetivo_dia(con, hoy, hoy)
    assert obj is not None
    assert ("Actividad hasta ahora (Garmin)", 1500) in obj.desglose
    conceptos = [c for c, _ in obj.desglose]
    assert "Día normal" not in conceptos


def test_objetivo_dia_pasado_con_activas(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2270, neat_kcal=230)
    guardar_metrica(con, hoy.isoformat(), "peso", 88.0)

    pasado = date(2026, 1, 5)
    guardar_metrica(con, pasado.isoformat(), "kcal_activas", 650.0)

    obj = nutricion.objetivo_dia(con, pasado, hoy)
    assert obj is not None
    assert ("Actividad (Garmin)", 650) in obj.desglose


def test_objetivo_dia_peso_ajustes_manda_y_faltantes(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2270, neat_kcal=230)
    guardar_metrica(con, hoy.isoformat(), "peso", 88.0)

    # Con peso en ajustes = 75.0, manda sobre 88.0
    ajustes = db.AjustesComida(peso_kg=75.0)
    obj = nutricion.objetivo_dia(con, hoy, hoy, ajustes=ajustes)
    assert obj is not None
    assert obj.peso_kg == 75.0

    # Sin peso en ajustes ni en base de datos -> None
    con.execute("DELETE FROM metricas WHERE clave = 'peso'")
    assert nutricion.objetivo_dia(con, hoy, hoy, ajustes=db.AjustesComida(peso_kg=None)) is None

    # Sin basal -> None
    con.execute("DELETE FROM metricas WHERE clave = 'kcal'")
    assert nutricion.objetivo_dia(con, hoy, hoy, ajustes=ajustes) is None


def test_ajustes_desde_formulario_valido_y_errores():
    datos_validos = {
        "deficit_kcal": "300",
        "proteinas_g_kg": "1.8",
        "grasas_g_kg": "1.0",
        "peso_kg": "85.5",
        "reparto-0": "20",
        "reparto-1": "10",
        "reparto-2": "35",
        "reparto-3": "10",
        "reparto-4": "25",
    }
    ajustes = nutricion.ajustes_desde_formulario(datos_validos)
    assert ajustes.deficit_kcal == 300
    assert ajustes.proteinas_g_kg == 1.8
    assert ajustes.grasas_g_kg == 1.0
    assert ajustes.peso_kg == 85.5
    assert ajustes.reparto["comida"] == 35

    # Peso vacío -> None
    datos_peso_vacio = dict(datos_validos, peso_kg="")
    ajustes_pv = nutricion.ajustes_desde_formulario(datos_peso_vacio)
    assert ajustes_pv.peso_kg is None

    # Reparto que suma 90
    datos_mal_reparto = dict(datos_validos, **{"reparto-0": "10"})
    with pytest.raises(ValueError, match="El reparto tiene que sumar 100 %"):
        nutricion.ajustes_desde_formulario(datos_mal_reparto)

    # Proteínas 5 (fuera de rango)
    datos_prot_alta = dict(datos_validos, proteinas_g_kg="5.0")
    with pytest.raises(ValueError, match="proteínas"):
        nutricion.ajustes_desde_formulario(datos_prot_alta)

    # Déficit "x"
    datos_def_invalido = dict(datos_validos, deficit_kcal="x")
    with pytest.raises(ValueError, match="déficit"):
        nutricion.ajustes_desde_formulario(datos_def_invalido)

    # Mínimo de calorías vacío -> None
    datos_min_vacio = dict(datos_validos, kcal_minimo="")
    assert nutricion.ajustes_desde_formulario(datos_min_vacio).kcal_minimo is None

    # Mínimo de calorías 1500 -> 1500
    datos_min_1500 = dict(datos_validos, kcal_minimo="1500")
    assert nutricion.ajustes_desde_formulario(datos_min_1500).kcal_minimo == 1500

    # Mínimo de calorías 500 (fuera de rango 800-4000) -> error
    datos_min_500 = dict(datos_validos, kcal_minimo="500")
    with pytest.raises(
        ValueError, match="El mínimo de calorías debe ser un número entero entre 800 y 4000"
    ):
        nutricion.ajustes_desde_formulario(datos_min_500)


def test_objetivo_dia_kcal_minimo(con):
    hoy = date(2026, 1, 10)
    _preparar_historial_basal_y_neat(con, hoy, b_kcal=2000, neat_kcal=200)
    guardar_metrica(con, hoy.isoformat(), "peso", 80.0)

    # Con kcal_minimo = None, sin mínimo
    ajustes_none = db.AjustesComida(kcal_minimo=None)
    obj_none = nutricion.objetivo_dia(con, hoy, hoy, ajustes=ajustes_none)
    assert obj_none is not None
    assert obj_none.kcal_minimo is None

    # Con kcal_minimo = 1800, usa 1800
    ajustes_1800 = db.AjustesComida(kcal_minimo=1800)
    obj_1800 = nutricion.objetivo_dia(con, hoy, hoy, ajustes=ajustes_1800)
    assert obj_1800 is not None
    assert obj_1800.kcal_minimo == 1800


def test_guardar_y_leer_kcal_minimo(con):
    ajustes = db.AjustesComida(kcal_minimo=1650)
    db.guardar_ajustes_comida(con, ajustes)
    leidos = db.ajustes_comida(con)
    assert leidos.kcal_minimo == 1650

    # Guardar None
    db.guardar_ajustes_comida(con, db.AjustesComida(kcal_minimo=None))
    assert db.ajustes_comida(con).kcal_minimo is None


def test_migracion_ajustes_comida_sin_columna(tmp_path):
    import sqlite3

    db_file = tmp_path / "antigua.db"
    con_raw = sqlite3.connect(db_file)
    con_raw.execute(
        """
        CREATE TABLE ajustes_comida (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            deficit_kcal INTEGER NOT NULL,
            proteinas_g_kg REAL NOT NULL,
            grasas_g_kg REAL NOT NULL,
            peso_kg REAL,
            reparto TEXT NOT NULL,
            actualizado TEXT NOT NULL
        )
        """
    )
    con_raw.execute(
        """
        INSERT INTO ajustes_comida (
            id, deficit_kcal, proteinas_g_kg, grasas_g_kg, reparto, actualizado
        )
        VALUES (1, 250, 1.6, 0.9, '{}', '2026-01-01T00:00:00Z')
        """
    )
    con_raw.commit()
    con_raw.close()

    con_migrada = db.conectar(db_file)
    ajustes = db.ajustes_comida(con_migrada)
    assert ajustes.kcal_minimo is None
    con_migrada.close()
