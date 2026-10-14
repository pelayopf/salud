"""Tests del módulo de tratamiento y sus tablas en db."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import consejo, db, lectura, tratamiento
from salud.metricas import Metrica
from salud.web import crear_app


def test_pauta_defecto_y_guardar(con):
    # Por defecto sin fila
    p_def = db.pauta(con)
    assert p_def.farmaco == "Mounjaro"
    assert p_def.dosis_mg == 5.0
    assert p_def.intervalo_dias == 7
    assert p_def.margen_olvido_dias == 4
    assert p_def.separacion_min_dias == 3
    assert p_def.dosis_previstas is None

    # Guardar nueva pauta con dosis previstas
    nueva = db.Pauta(
        farmaco="Ozempic",
        dosis_mg=1.0,
        intervalo_dias=10,
        margen_olvido_dias=5,
        separacion_min_dias=4,
        dosis_previstas=8,
    )
    db.guardar_pauta(con, nueva)
    leida = db.pauta(con)
    assert leida.farmaco == "Ozempic"
    assert leida.dosis_mg == 1.0
    assert leida.intervalo_dias == 10
    assert leida.margen_olvido_dias == 5
    assert leida.separacion_min_dias == 4
    assert leida.dosis_previstas == 8

    # Guardar de nuevo con dosis_previstas = None
    db.guardar_pauta(con, db.Pauta(farmaco="Ozempic", dosis_mg=1.0, dosis_previstas=None))
    assert db.pauta(con).dosis_previstas is None


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


def test_zona_sugerida():
    assert tratamiento.zona_sugerida("muslo izquierdo") == "muslo derecho"
    assert tratamiento.zona_sugerida("brazo derecho") == "brazo izquierdo"
    assert tratamiento.zona_sugerida(None) == "muslo derecho"
    assert tratamiento.zona_sugerida("inventada") == "muslo derecho"


def test_proxima_estados():
    p = db.Pauta()
    # Sin dosis
    assert tratamiento.proxima(p, None, date(2026, 10, 13)) is None

    ultima = db.Dosis(
        id=1,
        fecha="2026-10-12",
        farmaco="Mounjaro",
        dosis_mg=5.0,
        zona="muslo izquierdo",
    )

    # hoy 13 -> esperar (adelantar_desde 15, prevista 19)
    res_13 = tratamiento.proxima(p, ultima, date(2026, 10, 13))
    assert res_13 is not None
    assert res_13.estado == "esperar"
    assert res_13.adelantar_desde == date(2026, 10, 15)
    assert res_13.prevista == date(2026, 10, 19)
    assert res_13.dias_desde_ultima == 1
    assert res_13.zona_sugerida == "muslo derecho"

    # hoy 15 -> puede_adelantar
    res_15 = tratamiento.proxima(p, ultima, date(2026, 10, 15))
    assert res_15 is not None
    assert res_15.estado == "puede_adelantar"

    # hoy 19 -> hoy
    res_19 = tratamiento.proxima(p, ultima, date(2026, 10, 19))
    assert res_19 is not None
    assert res_19.estado == "hoy"

    # hoy 22 -> retrasada (limite 23)
    res_22 = tratamiento.proxima(p, ultima, date(2026, 10, 22))
    assert res_22 is not None
    assert res_22.estado == "retrasada"
    assert res_22.limite == date(2026, 10, 23)

    # hoy 24 -> saltar con siguiente_habitual 26
    res_24 = tratamiento.proxima(p, ultima, date(2026, 10, 24))
    assert res_24 is not None
    assert res_24.estado == "saltar"
    assert res_24.siguiente_habitual == date(2026, 10, 26)

    # Con intervalo 10: prevista 22
    p10 = db.Pauta(intervalo_dias=10)
    res_p10 = tratamiento.proxima(p10, ultima, date(2026, 10, 13))
    assert res_p10 is not None
    assert res_p10.prevista == date(2026, 10, 22)


def test_pauta_desde_formulario_valido_y_errores():
    valido = {
        "farmaco": "Mounjaro",
        "dosis_mg": "2,5",
        "intervalo_dias": "7",
        "margen_olvido_dias": "4",
        "separacion_min_dias": "3",
    }
    p = tratamiento.pauta_desde_formulario(valido)
    assert p.farmaco == "Mounjaro"
    assert p.dosis_mg == 2.5
    assert p.intervalo_dias == 7
    assert p.margen_olvido_dias == 4
    assert p.separacion_min_dias == 3

    # Margen mayor que el intervalo
    with pytest.raises(ValueError, match="margen de olvido"):
        tratamiento.pauta_desde_formulario(dict(valido, margen_olvido_dias="8"))

    # Separación mayor que el intervalo
    with pytest.raises(ValueError, match="separación mínima"):
        tratamiento.pauta_desde_formulario(dict(valido, separacion_min_dias="8"))

    # Fármaco vacío
    with pytest.raises(ValueError, match="fármaco"):
        tratamiento.pauta_desde_formulario(dict(valido, farmaco=""))

    # Dosis no positiva o > 50
    with pytest.raises(ValueError, match="dosis"):
        tratamiento.pauta_desde_formulario(dict(valido, dosis_mg="0"))
    with pytest.raises(ValueError, match="dosis"):
        tratamiento.pauta_desde_formulario(dict(valido, dosis_mg="60"))

    # Dosis previstas opcional
    p_prev = tratamiento.pauta_desde_formulario(dict(valido, dosis_previstas="8"))
    assert p_prev.dosis_previstas == 8
    p_vacio = tratamiento.pauta_desde_formulario(dict(valido, dosis_previstas=""))
    assert p_vacio.dosis_previstas is None
    with pytest.raises(ValueError, match="dosis previstas"):
        tratamiento.pauta_desde_formulario(dict(valido, dosis_previstas="0"))
    with pytest.raises(ValueError, match="dosis previstas"):
        tratamiento.pauta_desde_formulario(dict(valido, dosis_previstas="abc"))


def test_dosis_desde_formulario_valido_y_errores():
    p = db.Pauta(farmaco="Mounjaro", dosis_mg=5.0)
    hoy = date(2026, 10, 14)

    # Válido con coma y campos opcionales vacíos
    datos = {
        "fecha": "",
        "dosis_mg": "",
        "zona": "muslo izquierdo",
        "nota": "inyección semanal",
    }
    args = tratamiento.dosis_desde_formulario(datos, p, hoy)
    assert args["fecha"] == "2026-10-14"
    assert args["farmaco"] == "Mounjaro"
    assert args["dosis_mg"] == 5.0
    assert args["zona"] == "muslo izquierdo"
    assert args["nota"] == "inyección semanal"

    # Con dosis explícita con coma
    args2 = tratamiento.dosis_desde_formulario(
        dict(datos, dosis_mg="7,5", fecha="2026-10-12"), p, hoy
    )
    assert args2["fecha"] == "2026-10-12"
    assert args2["dosis_mg"] == 7.5

    # Fecha futura
    with pytest.raises(ValueError, match="posterior a hoy"):
        tratamiento.dosis_desde_formulario(dict(datos, fecha="2026-10-15"), p, hoy)

    # Zona inventada
    with pytest.raises(ValueError, match="zona de inyección"):
        tratamiento.dosis_desde_formulario(dict(datos, zona="espalda"), p, hoy)


def test_resumen_tratamiento_y_contexto(con, settings):
    hoy = date(2026, 10, 14)
    # Sin dosis -> None
    assert tratamiento.resumen(con, hoy) is None

    # Añadir dosis
    db.anadir_dosis(
        con,
        fecha="2026-10-10",
        farmaco="Mounjaro",
        dosis_mg=5.0,
        zona="muslo izquierdo",
    )
    # Añadir otra dosis del mismo fármaco y mg
    db.anadir_dosis(
        con,
        fecha="2026-10-03",
        farmaco="Mounjaro",
        dosis_mg=5.0,
        zona="muslo derecho",
    )
    # Y una de mg diferente para comprobar que solo cuenta las del mismo fármaco y mg
    db.anadir_dosis(
        con,
        fecha="2026-09-26",
        farmaco="Mounjaro",
        dosis_mg=2.5,
        zona="abdomen izquierdo",
    )

    db.guardar_pauta(con, db.Pauta(farmaco="Mounjaro", dosis_mg=5.0, dosis_previstas=8))

    res = tratamiento.resumen(con, hoy)
    assert res is not None
    assert res["farmaco"] == "Mounjaro"
    assert res["dosis_mg"] == 5.0
    assert res["intervalo_dias"] == 7
    assert res["ultima_dosis"] == "2026-10-10"
    assert res["dias_desde_ultima"] == 4
    assert res["estado"] == "puede_adelantar"
    assert res["prevista"] == "2026-10-17"
    assert res["dosis_hechas"] == 2
    assert res["dosis_previstas"] == 8

    # Verificar en consejo.contexto
    ahora = datetime(2026, 10, 14, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    ctx_c = consejo.contexto(con, settings, ahora)
    assert "tratamiento" in ctx_c
    assert ctx_c["tratamiento"] is not None
    assert ctx_c["tratamiento"]["dias_desde_ultima"] == 4

    # Verificar en lectura.contexto
    ctx_l = lectura.contexto(con, hoy)
    assert "tratamiento" in ctx_l
    assert ctx_l["tratamiento"] is not None
    assert ctx_l["tratamiento"]["dias_desde_ultima"] == 4


def test_web_tratamiento_flujo_completo(settings, con):
    hoy = date(2026, 10, 14)

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=GarminFalso,
        hoy=lambda: hoy,
    )
    c = TestClient(app, follow_redirects=False)

    # 1. /salud sin nada -> enlace "Apuntar tratamiento"
    r_salud = c.get("/salud")
    assert r_salud.status_code == 200
    assert "Apuntar tratamiento" in r_salud.text
    assert '<a href="/tratamiento">Apuntar tratamiento</a>' in r_salud.text

    # 2. /tratamiento sin datos
    r_trat = c.get("/tratamiento")
    assert r_trat.status_code == 200
    assert "Apuntar dosis" in r_trat.text
    assert "No hay dosis registradas" in r_trat.text

    # 3. Fecha futura -> error con _volver
    r_futura = c.post(
        "/tratamiento/dosis",
        data={"fecha": "2026-10-20", "dosis_mg": "5", "zona": "muslo izquierdo"},
    )
    assert r_futura.status_code == 303
    assert "/tratamiento?" in r_futura.headers["location"]
    assert "posterior" in r_futura.headers["location"]

    # 4. Apuntar dosis válida (zona muslo izquierdo)
    # También guardar una métrica de peso para comprobar historial
    db.guardar_metricas(
        con, [Metrica(fecha="2026-10-14", clave="peso", fuente="garmin", valor=82.5)]
    )
    r_apuntar = c.post(
        "/tratamiento/dosis",
        data={
            "fecha": "2026-10-14",
            "dosis_mg": "5",
            "zona": "muslo izquierdo",
            "nota": "dosis normal",
        },
    )
    assert r_apuntar.status_code == 303
    assert "/tratamiento" in r_apuntar.headers["location"]

    # 5. Aparece en el historial de /tratamiento y en Salud con "Zona sugerida: muslo derecho"
    r_trat_post = c.get("/tratamiento")
    assert r_trat_post.status_code == 200
    assert "Zona sugerida: muslo derecho" in r_trat_post.text
    assert "muslo izquierdo" in r_trat_post.text
    assert "82,5 kg" in r_trat_post.text

    r_salud_post = c.get("/salud")
    assert r_salud_post.status_code == 200
    assert "Zona sugerida: muslo derecho" in r_salud_post.text
    assert "Apuntar dosis" in r_salud_post.text

    # 6. Guardar pauta
    r_pauta = c.post(
        "/tratamiento/pauta",
        data={
            "farmaco": "Mounjaro",
            "dosis_mg": "7,5",
            "intervalo_dias": "7",
            "margen_olvido_dias": "4",
            "separacion_min_dias": "3",
        },
    )
    assert r_pauta.status_code == 303
    p_actual = db.pauta(con)
    assert p_actual.dosis_mg == 7.5

    # 7. Borrar dosis
    dosis_creadas = db.dosis(con)
    assert len(dosis_creadas) == 1
    r_borrar = c.post(f"/tratamiento/dosis/{dosis_creadas[0].id}/borrar")
    assert r_borrar.status_code == 303
    assert len(db.dosis(con)) == 0
