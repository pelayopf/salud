"""Tests del módulo de tratamiento y sus tablas en db."""

from __future__ import annotations

from salud import db


def test_pauta_defecto_y_guardar(con):
    # Por defecto sin fila
    p_def = db.pauta(con)
    assert p_def.farmaco == "Mounjaro"
    assert p_def.dosis_mg == 5.0
    assert p_def.intervalo_dias == 7
    assert p_def.margen_olvido_dias == 4
    assert p_def.separacion_min_dias == 3

    # Guardar nueva pauta
    nueva = db.Pauta(
        farmaco="Ozempic",
        dosis_mg=1.0,
        intervalo_dias=10,
        margen_olvido_dias=5,
        separacion_min_dias=4,
    )
    db.guardar_pauta(con, nueva)
    leida = db.pauta(con)
    assert leida.farmaco == "Ozempic"
    assert leida.dosis_mg == 1.0
    assert leida.intervalo_dias == 10
    assert leida.margen_olvido_dias == 5
    assert leida.separacion_min_dias == 4


def test_dosis_anadir_listar_orden_y_borrar(con):
    # Inicialmente vacía
    assert db.dosis(con) == []

    id1 = db.anadir_dosis(
        con,
        fecha="2026-10-01",
        farmaco="Mounjaro",
        dosis_mg=5.0,
        zona="muslo derecho",
        nota="primera dosis",
    )
    id2 = db.anadir_dosis(
        con,
        fecha="2026-10-08",
        farmaco="Mounjaro",
        dosis_mg=5.0,
        zona="muslo izquierdo",
        nota="segunda dosis",
    )
    id3 = db.anadir_dosis(
        con,
        fecha="2026-10-08",
        farmaco="Mounjaro",
        dosis_mg=2.5,
        zona="abdomen derecho",
        nota="extra",
    )

    lista = db.dosis(con)
    assert len(lista) == 3
    # Orden más reciente primero: fecha DESC, id DESC -> id3, id2, id1
    assert [d.id for d in lista] == [id3, id2, id1]
    assert lista[0].zona == "abdomen derecho"
    assert lista[1].zona == "muslo izquierdo"
    assert lista[2].nota == "primera dosis"

    # Borrar
    assert db.borrar_dosis(con, id2) is True
    assert db.borrar_dosis(con, id2) is False  # ya no existe
    restantes = db.dosis(con)
    assert len(restantes) == 2
    assert [d.id for d in restantes] == [id3, id1]
