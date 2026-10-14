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
        "2000000000008",
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

    # Actualizar producto pasando un diccionario
    db.guardar_producto(
        con,
        {
            "codigo": "2000000000008",
            "nombre": "Cereal actualizado",
            "marca": "Marca Test",
            "nutriscore": "A",
            "unidad": "g",
            "kcal": 370.0,
            "proteinas": 11.0,
            "hidratos": 68.0,
            "grasas": 4.5,
            "datos": {"test": 456},
        },
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
        unidad="g",
        kcal=250.0,
        proteinas=8.0,
        hidratos=45.0,
        grasas=3.0,
    )
    db.guardar_producto(
        con,
        codigo="222",
        nombre="Leche",
        marca="Pascual",
        unidad="ml",
        kcal=60.0,
        proteinas=3.2,
        hidratos=4.8,
        grasas=3.0,
    )
    db.guardar_producto(
        con,
        codigo="333",
        nombre="Yogur",
        marca="Danone",
        unidad="g",
        kcal=70.0,
        proteinas=4.0,
        hidratos=5.0,
        grasas=3.5,
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
