from __future__ import annotations

import sqlite3

from conftest import hay

from salud import db, metricas
from salud.metricas import Metrica


def test_manda_la_fuente_con_mas_prioridad(con):
    db.guardar_metricas(
        con,
        [
            Metrica("2026-01-10", "peso", "samsung", 81.0),
            Metrica("2026-01-10", "peso", "garmin", 80.5),
            Metrica("2026-01-11", "peso", "samsung", 80.9),
        ],
    )
    assert db.dia(con, "2026-01-10")["peso"].fuente == "garmin"
    serie = db.serie(con, "peso", "2026-01-01", "2026-01-31")
    assert [(v.fecha, v.valor, v.fuente) for v in serie] == [
        ("2026-01-10", 80.5, "garmin"),
        ("2026-01-11", 80.9, "samsung"),
    ]
    db.guardar_metricas(con, [Metrica("2026-01-10", "peso", "manual", 80.0)])
    assert db.dia(con, "2026-01-10")["peso"].valor == 80.0


def test_ultimo_y_anterior(con):
    db.guardar_metricas(
        con,
        [
            Metrica("2026-01-03", "ftp", "garmin", 220),
            Metrica("2026-01-20", "ftp", "garmin", 230),
        ],
    )
    assert hay(db.ultimo(con, "ftp", "2026-01-19")).valor == 220
    assert hay(db.ultimo(con, "ftp", "2026-02-01")).valor == 230
    assert db.ultimo(con, "ftp", "2026-01-01") is None
    assert hay(db.anterior(con, "ftp", "2026-01-20")).valor == 220


def test_reemplazar_una_metrica_y_una_actividad_no_duplica(con):
    db.guardar_metricas(con, [Metrica("2026-01-10", "pasos", "garmin", 100)])
    db.guardar_metricas(con, [Metrica("2026-01-10", "pasos", "garmin", 9000)])
    assert db.dia(con, "2026-01-10")["pasos"].valor == 9000

    a = metricas.Actividad(
        "garmin", "1", "2026-01-10 18:00:00", "cycling", "Rodaje", "{}", carga=50
    )
    db.guardar_actividades(con, [a])
    db.guardar_actividades(con, [a])
    filas = db.actividades(con, "2026-01-10", "2026-01-10")
    assert len(filas) == 1
    assert filas[0]["carga"] == 50


def test_lecturas(con):
    db.guardar_lectura(con, "2026-01-10", error="sin conexión")
    db.guardar_lectura(con, "2026-01-11", contenido={"titular": "Todo bien"}, modelo="sonnet")
    assert hay(db.lectura(con, "2026-01-10"))["estado"] == "error"
    assert hay(db.lectura(con, "2026-01-11"))["contenido"] == {"titular": "Todo bien"}
    assert [x["fecha"] for x in db.lecturas(con)] == ["2026-01-11", "2026-01-10"]
    assert db.lectura(con, "2026-01-12") is None


def test_cache_productos(con):
    import json

    db.guardar_producto(
        con,
        codigo="2000000000008",
        nombre="Cereal inventado",
        marca="Marca Test",
        nutriscore="B",
        unidad="g",
        kcal=380.0,
        proteinas=10.0,
        hidratos=70.0,
        grasas=5.0,
        datos={"test": 123},
    )

    fila = db.leer_producto(con, "2000000000008")
    assert fila is not None
    assert fila["codigo"] == "2000000000008"
    assert fila["nombre"] == "Cereal inventado"
    assert fila["marca"] == "Marca Test"
    assert fila["nutriscore"] == "B"
    assert fila["unidad"] == "g"
    assert fila["kcal"] == 380.0
    assert fila["proteinas"] == 10.0
    assert fila["hidratos"] == 70.0
    assert fila["grasas"] == 5.0
    assert fila["consultado"] is not None
    assert json.loads(fila["datos"]) == {"test": 123}

    assert db.leer_producto(con, "20000004") is None

    # Actualizar producto
    db.guardar_producto(
        con,
        codigo="2000000000008",
        nombre="Cereal actualizado",
        marca="Marca Test",
        nutriscore="A",
        unidad="g",
        kcal=370.0,
        proteinas=11.0,
        hidratos=68.0,
        grasas=4.5,
        datos={"test": 456},
    )
    fila2 = hay(db.leer_producto(con, "2000000000008"))
    assert fila2["nombre"] == "Cereal actualizado"
    assert fila2["nutriscore"] == "A"
    assert fila2["kcal"] == 370.0


def test_diario_comidas(con):
    id1 = db.anadir_comida(
        con,
        "2026-01-10",
        "desayuno",
        "Avena",
        50.0,
        "g",
        kcal=180.0,
        proteinas=6.0,
        hidratos=30.0,
        grasas=3.0,
        codigo="2000000000008",
    )
    id2 = db.anadir_comida(
        con,
        "2026-01-10",
        "comida",
        "Arroz casero",
        200.0,
        "g",
        kcal=260.0,
        proteinas=5.0,
        hidratos=56.0,
        grasas=1.0,
    )
    id3 = db.anadir_comida(
        con,
        "2026-01-11",
        "cena",
        "Tortilla",
        150.0,
        "g",
        kcal=220.0,
    )

    lista_10 = db.comidas(con, "2026-01-10")
    assert len(lista_10) == 2
    assert [c.id for c in lista_10] == [id1, id2]
    assert lista_10[0].nombre == "Avena"
    assert lista_10[0].codigo == "2000000000008"
    assert lista_10[0].cantidad == 50.0
    assert lista_10[1].nombre == "Arroz casero"
    assert lista_10[1].codigo is None

    assert len(db.comidas(con, "2026-01-11")) == 1
    assert len(db.comidas(con, "2026-01-12")) == 0

    # Borrado
    assert db.borrar_comida(con, id1) is True
    assert db.borrar_comida(con, id1) is False
    assert len(db.comidas(con, "2026-01-10")) == 1
    assert db.borrar_comida(con, id2) is True
    assert len(db.comidas(con, "2026-01-10")) == 0
    assert db.borrar_comida(con, id3) is True


def test_anadir_comida_validaciones(con):
    import pytest

    # Nombre vacío
    with pytest.raises(ValueError, match="vacío"):
        db.anadir_comida(con, "2026-01-10", "desayuno", "   ", 100.0, "g")

    # Momento no válido
    with pytest.raises(ValueError, match="momento no válido"):
        db.anadir_comida(con, "2026-01-10", "picoteo", "Manzana", 100.0, "g")

    # Unidad no válida
    with pytest.raises(ValueError, match="unidad no válida"):
        db.anadir_comida(con, "2026-01-10", "desayuno", "Zumo", 200.0, "vasos")

    # Cantidad <= 0
    with pytest.raises(ValueError, match="mayor que 0"):
        db.anadir_comida(con, "2026-01-10", "desayuno", "Pan", 0.0, "g")
    with pytest.raises(ValueError, match="mayor que 0"):
        db.anadir_comida(con, "2026-01-10", "desayuno", "Pan", -10.0, "g")


def test_totales_dia_y_momento(con):
    # Desayuno completo
    db.anadir_comida(
        con,
        "2026-01-10",
        "desayuno",
        "Plátano",
        100.0,
        "g",
        kcal=90.0,
        proteinas=1.0,
        hidratos=22.0,
        grasas=0.3,
    )
    # Comida con proteínas y grasas None (ej. alimento sin datos)
    db.anadir_comida(
        con,
        "2026-01-10",
        "comida",
        "Pan raro",
        50.0,
        "g",
        kcal=130.0,
        proteinas=None,
        hidratos=25.0,
        grasas=None,
    )
    # Cena totalmente sin datos nutricionales
    db.anadir_comida(con, "2026-01-10", "cena", "Guiso desconocido", 200.0, "g")

    totales = db.totales_dia(con, "2026-01-10")
    assert totales.total.kcal == 220.0  # 90 + 130
    assert totales.total.sin_kcal == 1
    assert totales.total.proteinas == 1.0
    assert totales.total.sin_proteinas == 2
    assert totales.total.hidratos == 47.0
    assert totales.total.sin_hidratos == 1
    assert totales.total.grasas == 0.3
    assert totales.total.sin_grasas == 2
    assert totales.total.entradas == 3
    assert totales.total.incompletas == 2

    # Desempaquetado como (por_momento, total)
    por_momento, total = totales
    assert total.kcal == 220.0
    assert por_momento["desayuno"].kcal == 90.0
    assert por_momento["desayuno"].sin_kcal == 0
    assert por_momento["desayuno"].incompletas == 0

    assert por_momento["comida"].kcal == 130.0
    assert por_momento["comida"].sin_proteinas == 1
    assert por_momento["comida"].incompletas == 1

    assert por_momento["cena"].kcal == 0.0
    assert por_momento["cena"].sin_kcal == 1
    assert por_momento["cena"].incompletas == 1

    assert "media mañana" in db.MOMENTOS
    assert por_momento["media mañana"].kcal == 0.0
    assert por_momento["media mañana"].entradas == 0

    # Añadir comida a media mañana
    db.anadir_comida(con, "2026-01-10", "media mañana", "Frutos secos", 30.0, "g", kcal=180.0)
    totales2 = db.totales_dia(con, "2026-01-10")
    assert totales2.por_momento["media mañana"].kcal == 180.0
    assert totales2.por_momento["media mañana"].entradas == 1

    # Día vacío
    vacio = db.totales_dia(con, "2026-01-01")
    assert vacio.total.kcal == 0.0
    assert vacio.total.entradas == 0
    assert vacio.total.incompletas == 0


def test_productos_recientes(con):
    # Base vacía
    assert db.productos_recientes(con) == []

    # Guardamos productos en la caché/tabla de productos
    db.guardar_producto(
        con,
        codigo="111",
        nombre="Pan",
        marca="Bimbo",
        nutriscore=None,
        unidad="g",
        kcal=250.0,
        proteinas=8.0,
        hidratos=45.0,
        grasas=3.0,
        datos={},
    )
    db.guardar_producto(
        con,
        codigo="222",
        nombre="Leche",
        marca="Pascual",
        nutriscore=None,
        unidad="ml",
        kcal=60.0,
        proteinas=3.2,
        hidratos=4.8,
        grasas=3.0,
        datos={},
    )
    db.guardar_producto(
        con,
        codigo="333",
        nombre="Yogur",
        marca="Danone",
        nutriscore=None,
        unidad="g",
        kcal=70.0,
        proteinas=4.0,
        hidratos=5.0,
        grasas=3.5,
        datos={},
    )

    # Añadir comidas:
    # 1. Pan (111)
    db.anadir_comida(con, "2026-01-10", "desayuno", "Pan", 50.0, "g", codigo="111")
    # 2. Leche (222)
    db.anadir_comida(con, "2026-01-10", "desayuno", "Leche", 200.0, "ml", codigo="222")
    # 3. Comida a mano sin código
    db.anadir_comida(con, "2026-01-10", "comida", "Arroz casero", 150.0, "g", codigo=None)
    # 4. Pan de nuevo (111)
    db.anadir_comida(con, "2026-01-10", "merienda", "Pan", 50.0, "g", codigo="111")
    # 5. Yogur (333)
    db.anadir_comida(con, "2026-01-10", "cena", "Yogur", 125.0, "g", codigo="333")

    recientes = db.productos_recientes(con)
    # Ordenados por uso más reciente: Yogur (id 5), Pan (id 4), Leche (id 2)
    assert len(recientes) == 3
    assert [r["codigo"] for r in recientes] == ["333", "111", "222"]
    assert recientes[0]["nombre"] == "Yogur"
    assert recientes[0]["marca"] == "Danone"
    assert recientes[0]["unidad"] == "g"

    # Con límite
    recientes_limite = db.productos_recientes(con, limite=2)
    assert len(recientes_limite) == 2
    assert [r["codigo"] for r in recientes_limite] == ["333", "111"]


def test_guardar_y_leer_producto_propio(con):
    import pytest

    # 1. Alta sin código y con código
    id_sin = db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Tortilla casera",
        marca=None,
        unidad="g",
        kcal=150.0,
        proteinas=10.0,
        hidratos=2.0,
        grasas=11.0,
        origen="manual",
    )
    assert id_sin > 0
    fila_sin = db.leer_producto_propio(con, id_sin)
    assert fila_sin is not None
    assert fila_sin["nombre"] == "Tortilla casera"
    assert fila_sin["codigo"] is None
    assert fila_sin["origen"] == "manual"
    creado_sin = fila_sin["creado"]
    assert creado_sin is not None

    id_con = db.guardar_producto_propio(
        con,
        id_=None,
        codigo="8410000000001",
        nombre="Galletas de avena",
        marca="MiMarca",
        unidad="g",
        kcal=450.0,
        proteinas=8.0,
        hidratos=65.0,
        grasas=16.0,
        origen="etiqueta-ia",
    )
    assert id_con > 0
    fila_con = db.producto_propio_por_codigo(con, "8410000000001")
    assert fila_con is not None
    assert fila_con["id"] == id_con
    assert fila_con["nombre"] == "Galletas de avena"
    assert fila_con["marca"] == "MiMarca"
    assert fila_con["origen"] == "etiqueta-ia"
    creado_con = fila_con["creado"]

    # Segunda alta con el mismo código actualiza (mismo id, creado igual)
    id_actualizado = db.guardar_producto_propio(
        con,
        id_=None,
        codigo="8410000000001",
        nombre="Galletas de avena y miel",
        marca="MiMarca",
        unidad="g",
        kcal=460.0,
        proteinas=7.5,
        hidratos=68.0,
        grasas=15.0,
        origen="manual",
    )
    assert id_actualizado == id_con
    fila_act = db.leer_producto_propio(con, id_con)
    assert fila_act is not None
    assert fila_act["nombre"] == "Galletas de avena y miel"
    assert fila_act["kcal"] == 460.0
    assert fila_act["origen"] == "manual"
    assert fila_act["creado"] == creado_con

    # 2. id_ inexistente → ValueError; origen no válido → ValueError
    with pytest.raises(ValueError, match="Producto no encontrado"):
        db.guardar_producto_propio(
            con,
            id_=99999,
            codigo=None,
            nombre="Fantasma",
            marca=None,
            unidad="g",
            kcal=100.0,
            proteinas=1.0,
            hidratos=1.0,
            grasas=1.0,
            origen="manual",
        )

    with pytest.raises(ValueError, match="origen no válido"):
        db.guardar_producto_propio(
            con,
            id_=None,
            codigo=None,
            nombre="Invalido",
            marca=None,
            unidad="g",
            kcal=100.0,
            proteinas=1.0,
            hidratos=1.0,
            grasas=1.0,
            origen="inventado",
        )


def test_productos_propios_ordenados_por_actualizado_desc(con):
    id1 = db.guardar_producto_propio(
        con,
        id_=None,
        codigo="1000000000001",
        nombre="Primero",
        marca=None,
        unidad="g",
        kcal=100.0,
        proteinas=1.0,
        hidratos=1.0,
        grasas=1.0,
        origen="manual",
    )
    # Forzar actualizado diferente modificando en BD
    con.execute(
        "UPDATE productos_propios SET actualizado = '2026-01-01T10:00:00Z' WHERE id = ?", (id1,)
    )

    id2 = db.guardar_producto_propio(
        con,
        id_=None,
        codigo="1000000000002",
        nombre="Segundo",
        marca=None,
        unidad="g",
        kcal=200.0,
        proteinas=2.0,
        hidratos=2.0,
        grasas=2.0,
        origen="manual",
    )
    con.execute(
        "UPDATE productos_propios SET actualizado = '2026-01-02T10:00:00Z' WHERE id = ?", (id2,)
    )

    id3 = db.guardar_producto_propio(
        con,
        id_=None,
        codigo="1000000000003",
        nombre="Tercero",
        marca=None,
        unidad="g",
        kcal=300.0,
        proteinas=3.0,
        hidratos=3.0,
        grasas=3.0,
        origen="manual",
    )
    con.execute(
        "UPDATE productos_propios SET actualizado = '2026-01-03T10:00:00Z' WHERE id = ?", (id3,)
    )

    lista = db.productos_propios(con)
    assert [p["id"] for p in lista] == [id3, id2, id1]


def test_normalizar_titulo():
    assert db.normalizar_titulo("  Pádel  Chuen ") == "padel chuen"
    assert db.normalizar_titulo("FÚTBOL") == "futbol"


def test_reemplazar_planificado_solo_toca_su_origen_y_rango(con):
    g1 = db.Planificado(
        fecha="2026-05-01",
        inicio="19:00",
        duracion_min=60,
        titulo="Fútbol",
        tipo="soccer",
        intensidad="alta",
        origen="google",
        uid="u1@2026-05-01",
    )
    g2 = db.Planificado(
        fecha="2026-05-10",
        inicio="10:00",
        duracion_min=90,
        titulo="Pádel",
        tipo="paddelball",
        intensidad="alta",
        origen="google",
        uid="u2@2026-05-10",
    )
    st = db.Planificado(
        fecha="2026-05-02",
        inicio=None,
        duracion_min=None,
        titulo="Bici",
        tipo="cycling",
        intensidad="moderada",
        origen="semana-tipo",
        uid="",
    )
    db.reemplazar_planificado(
        con, origen="google", desde="2026-05-01", hasta="2026-05-10", filas=[g1, g2]
    )
    db.reemplazar_planificado(
        con, origen="semana-tipo", desde="2026-05-01", hasta="2026-05-10", filas=[st]
    )

    # Reemplazamos rango 05-01 a 05-05 para google con un nuevo evento
    g3 = db.Planificado(
        fecha="2026-05-01",
        inicio=None,
        duracion_min=None,
        titulo="Partido nuevo",
        tipo="soccer",
        intensidad="alta",
        origen="google",
        uid="u3@2026-05-01",
    )
    insertados = db.reemplazar_planificado(
        con, origen="google", desde="2026-05-01", hasta="2026-05-05", filas=[g3]
    )
    assert insertados == 1

    todas = db.planificado(con, "2026-05-01", "2026-05-15")
    # g1 (del 05-01) fue borrado y reemplazado por g3 (inicio None).
    # st (del 05-02, origen semana-tipo) sigue intacto.
    # g2 (del 05-10, fuera del rango 05-01 a 05-05) sigue intacto.
    assert len(todas) == 3
    assert todas[0].uid == "u3@2026-05-01"
    assert todas[0].inicio is None
    assert todas[1].origen == "semana-tipo"
    assert todas[2].uid == "u2@2026-05-10"


def test_borrar_planificado_titulo(con):
    filas = [
        db.Planificado(
            fecha="2026-05-01",
            inicio="18:00",
            duracion_min=60,
            titulo="Pádel Chuen",
            tipo="paddelball",
            intensidad="alta",
            origen="google",
            uid="u1",
        ),
        db.Planificado(
            fecha="2026-05-02",
            inicio="19:00",
            duracion_min=60,
            titulo="padel   chuen",
            tipo="paddelball",
            intensidad="alta",
            origen="google",
            uid="u2",
        ),
        db.Planificado(
            fecha="2026-05-03",
            inicio="20:00",
            duracion_min=60,
            titulo="Fútbol",
            tipo="soccer",
            intensidad="alta",
            origen="google",
            uid="u3",
        ),
        db.Planificado(
            fecha="2026-05-04",
            inicio="18:00",
            duracion_min=60,
            titulo="Pádel Chuen",
            tipo="paddelball",
            intensidad="alta",
            origen="semana-tipo",
            uid="",
        ),
    ]
    db.reemplazar_planificado(
        con, origen="google", desde="2026-05-01", hasta="2026-05-10", filas=filas[:3]
    )
    db.reemplazar_planificado(
        con, origen="semana-tipo", desde="2026-05-01", hasta="2026-05-10", filas=[filas[3]]
    )

    borrados = db.borrar_planificado_titulo(con, "  padel   CHUEN  ")
    assert borrados == 2

    restantes = db.planificado(con, "2026-05-01", "2026-05-10")
    assert len(restantes) == 2
    assert {r.titulo for r in restantes} == {"Fútbol", "Pádel Chuen"}
    assert {r.origen for r in restantes} == {"google", "semana-tipo"}


def test_marcar_titulo_upsert_y_titulos_calendario(con):
    db.marcar_titulo(con, titulo="  Pádel Chuen ", deporte=True, tipo="paddelball")
    assert db.titulos_calendario(con) == {"padel chuen": (True, "paddelball")}

    # Upsert: modificar la misma entrada
    db.marcar_titulo(con, titulo="padel chuen", deporte=False, tipo=None)
    assert db.titulos_calendario(con) == {"padel chuen": (False, None)}


def test_guardar_semana_tipo_reemplaza_todo(con):
    e1 = db.EntradaSemana(dia=1, inicio="19:00", duracion_min=60, tipo="soccer", intensidad="alta")
    e2 = db.EntradaSemana(
        dia=3, inicio="18:30", duracion_min=90, tipo="cycling", intensidad="moderada"
    )
    db.guardar_semana_tipo(con, [e1, e2])

    assert db.semana_tipo(con) == [e1, e2]

    # Reemplazar todo
    e3 = db.EntradaSemana(
        dia=5, inicio=None, duracion_min=45, tipo="running", intensidad="moderada"
    )
    db.guardar_semana_tipo(con, [e3])

    assert db.semana_tipo(con) == [e3]


def test_consejos_comida(con):
    assert db.ultimo_consejo(con, "2026-01-10") is None

    id1 = db.guardar_consejo(
        con,
        fecha="2026-01-10",
        nota="primera nota",
        contenido={"resumen": "Primero", "ahora": "Manzana"},
        modelo="gemini-flash",
        segundos=1.2,
    )
    assert id1 > 0

    id2 = db.guardar_consejo(
        con,
        fecha="2026-01-10",
        nota="segunda nota",
        contenido={"resumen": "Segundo", "ahora": "Plátano"},
        modelo="gemini-flash",
        segundos=1.5,
    )
    assert id2 > id1

    c = db.ultimo_consejo(con, "2026-01-10")
    assert c is not None
    assert c["nota"] == "segunda nota"
    assert c["resumen"] == "Segundo"
    assert c["ahora"] == "Plátano"
    assert c["modelo"] == "gemini-flash"
    assert "creado" in c

    assert db.ultimo_consejo(con, "2026-01-11") is None


def test_añadir_columnas_migracion(tmp_path):
    db_file = tmp_path / "antigua.db"
    con_raw = sqlite3.connect(db_file)
    con_raw.execute("""
    CREATE TABLE productos_propios (
        id INTEGER PRIMARY KEY,
        codigo TEXT UNIQUE,
        nombre TEXT NOT NULL,
        marca TEXT,
        unidad TEXT NOT NULL,
        kcal REAL,
        proteinas REAL,
        hidratos REAL,
        grasas REAL,
        origen TEXT NOT NULL,
        creado TEXT NOT NULL,
        actualizado TEXT NOT NULL
    );
    """)
    con_raw.close()

    con = db.conectar(db_file)
    columnas = {f["name"] for f in con.execute("PRAGMA table_info(productos_propios)").fetchall()}
    assert "racion_g" in columnas
    assert "racion_nombre" in columnas
    con.close()

    con2 = db.conectar(db_file)
    columnas2 = {f["name"] for f in con2.execute("PRAGMA table_info(productos_propios)").fetchall()}
    assert "racion_g" in columnas2
    assert "racion_nombre" in columnas2
    con2.close()


def test_guardar_producto_propio_con_raciones(con):
    id_ = db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Plátano",
        marca=None,
        unidad="g",
        kcal=89.0,
        proteinas=1.1,
        hidratos=22.8,
        grasas=0.3,
        origen="ciqual",
        racion_g=120.0,
        racion_nombre="plátano",
    )
    fila = db.leer_producto_propio(con, id_)
    assert fila is not None
    assert fila["racion_g"] == 120.0
    assert fila["racion_nombre"] == "plátano"
    assert fila["origen"] == "ciqual"


def test_buscar_productos(con):
    # Propio con código
    id_propio = db.guardar_producto_propio(
        con,
        id_=None,
        codigo="8410000000001",
        nombre="Pan de centeno",
        marca="Hacendado",
        unidad="g",
        kcal=250.0,
        proteinas=9.0,
        hidratos=48.0,
        grasas=1.5,
        origen="manual",
    )
    # OFF en caché con código distinto
    db.guardar_producto(
        con,
        codigo="8410000000002",
        nombre="Pan de centeno integral",
        marca="Bimbo",
        nutriscore="a",
        unidad="g",
        kcal=230.0,
        proteinas=8.5,
        hidratos=45.0,
        grasas=1.2,
        datos={},
    )
    # OFF en caché con el MISMO código que el propio (debe descartarse por ser duplicado)
    db.guardar_producto(
        con,
        codigo="8410000000001",
        nombre="Pan de centeno de OFF",
        marca="Hacendado",
        nutriscore="b",
        unidad="g",
        kcal=255.0,
        proteinas=9.0,
        hidratos=48.0,
        grasas=1.5,
        datos={},
    )
    # Propio sin código
    id_propio_sin_codigo = db.guardar_producto_propio(
        con,
        id_=None,
        codigo=None,
        nombre="Bizcocho casero",
        marca="Casero",
        unidad="g",
        kcal=350.0,
        proteinas=6.0,
        hidratos=50.0,
        grasas=14.0,
        origen="manual",
    )

    # Búsqueda prefijo de palabras "pan cent": encuentra el propio y el de OFF
    res = db.buscar_productos(con, "pan cent")
    assert len(res) == 2
    # Propios primero
    assert res[0]["tipo"] == "propio"
    assert res[0]["id"] == id_propio
    assert res[0]["codigo"] == "8410000000001"
    assert res[0]["nombre"] == "Pan de centeno"
    assert res[0]["marca"] == "Hacendado"
    assert res[0]["kcal"] == 250.0

    assert res[1]["tipo"] == "off"
    assert res[1]["id"] is None
    assert res[1]["codigo"] == "8410000000002"
    assert res[1]["nombre"] == "Pan de centeno integral"
    assert res[1]["marca"] == "Bimbo"

    # Búsqueda por marca
    res_marca = db.buscar_productos(con, "bimbo")
    assert len(res_marca) == 1
    assert res_marca[0]["codigo"] == "8410000000002"

    # Propio sin código
    res_bizcocho = db.buscar_productos(con, "bizcocho")
    assert len(res_bizcocho) == 1
    assert res_bizcocho[0]["tipo"] == "propio"
    assert res_bizcocho[0]["id"] == id_propio_sin_codigo
    assert res_bizcocho[0]["codigo"] is None


def test_comidas_anteriores(con):
    # Comida reciente sin código (2026-01-10)
    db.anadir_comida(
        con,
        fecha="2026-01-10",
        momento="comida",
        nombre="Tortilla de patatas",
        cantidad=150.0,
        unidad="g",
        kcal=200.0,
        codigo=None,
    )
    # Misma comida más antigua (2026-01-05): debe ignorarse (una por nombre, la más reciente)
    db.anadir_comida(
        con,
        fecha="2026-01-05",
        momento="cena",
        nombre="tortilla de patatas",
        cantidad=100.0,
        unidad="g",
        kcal=140.0,
        codigo=None,
    )
    # Otra comida distinta que encaja (2026-01-08)
    db.anadir_comida(
        con,
        fecha="2026-01-08",
        momento="cena",
        nombre="Tortilla francesa",
        cantidad=80.0,
        unidad="g",
        kcal=120.0,
        codigo=None,
    )
    # Comida con código: NO debe salir
    db.anadir_comida(
        con,
        fecha="2026-01-09",
        momento="comida",
        nombre="Tortilla envasada",
        cantidad=200.0,
        unidad="g",
        kcal=260.0,
        codigo="8410000000009",
    )
    # Comida de hace más de 120 días (2025-08-01): NO debe salir
    db.anadir_comida(
        con,
        fecha="2025-08-01",
        momento="comida",
        nombre="Tortilla campera",
        cantidad=150.0,
        unidad="g",
        kcal=210.0,
        codigo=None,
    )

    res = db.comidas_anteriores(con, "tort", hasta="2026-01-10")
    nombres = [c.nombre for c in res]
    assert nombres == ["Tortilla de patatas", "Tortilla francesa"]
    assert res[0].fecha == "2026-01-10"
    assert res[0].cantidad == 150.0


def test_leer_comida(con):
    id_ = db.anadir_comida(
        con,
        fecha="2026-01-10",
        momento="desayuno",
        nombre="Café con leche",
        cantidad=200.0,
        unidad="ml",
        kcal=80.0,
    )
    c = db.leer_comida(con, id_)
    assert c is not None
    assert c.id == id_
    assert c.nombre == "Café con leche"
    assert c.momento == "desayuno"
    assert c.cantidad == 200.0
    assert c.unidad == "ml"
    assert c.kcal == 80.0
    assert db.leer_comida(con, 99999) is None
