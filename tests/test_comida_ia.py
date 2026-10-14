from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any

from starlette.testclient import TestClient

from salud import db, ia
from salud.config import Settings
from salud.web import crear_app

JPEG_MINIMO = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00"


def _alimento_test(nombre: str = "Sopa", cantidad: float = 150.0, **kwargs: Any) -> ia.AlimentoIA:
    valores: dict[str, Any] = {
        "nombre": nombre,
        "cantidad": cantidad,
        "unidad": "g",
        "kcal": 100.0,
        "proteinas": 4.0,
        "hidratos": 15.0,
        "grasas": 2.0,
        "momento": "comida",
        "aviso": None,
    }
    valores.update(kwargs)
    return ia.AlimentoIA(**valores)


def test_get_comida_ia(settings: Settings):
    hoy = date(2026, 10, 14)
    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: None,  # type: ignore
        hoy=lambda: hoy,
    )
    client = TestClient(app)
    r = client.get("/comida/ia")
    assert r.status_code == 200
    assert 'name="texto"' in r.text
    assert "data-input-foto-ia" in r.text
    assert "Añadir a" in r.text
    assert "Desayuno" in r.text
    assert "Comida" in r.text


def test_flujo_comida_ia_formulario_texto_y_revisar(settings: Settings):
    hoy = date(2026, 10, 14)
    evento = threading.Event()

    def analizar_falso(_texto: str, _imagen: bytes | None) -> list[ia.AlimentoIA]:
        evento.wait(timeout=5)
        return [_alimento_test(nombre="Sopa de fideos", cantidad=150.0)]

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: None,  # type: ignore
        hoy=lambda: hoy,
        analizar=analizar_falso,
    )
    client = TestClient(app, follow_redirects=False)

    # 1. Enviar formulario con texto
    r_post = client.post(
        "/comida/ia",
        data={"fecha": "2026-10-14", "momento": "comida", "texto": "150 g de sopa de fideos"},
    )
    assert r_post.status_code == 303
    loc = r_post.headers["location"]
    assert loc.startswith("/comida/ia/")
    id_borrador = int(loc.split("/")[-1])

    # 2. Con evento bloqueado, el borrador está "preparando"
    r_prep = client.get(f"/comida/ia/{id_borrador}")
    assert r_prep.status_code == 200
    assert "Analizando" in r_prep.text
    assert "150 g de sopa de fideos" in r_prep.text

    r_estado = client.get(f"/comida/ia/{id_borrador}/estado")
    assert r_estado.status_code == 200
    assert r_estado.json() == {"estado": "preparando"}

    # 3. Liberar evento y esperar a que complete
    evento.set()
    for _ in range(50):
        if client.get(f"/comida/ia/{id_borrador}/estado").json().get("estado") == "listo":
            break
        time.sleep(0.05)

    assert client.get(f"/comida/ia/{id_borrador}/estado").json() == {"estado": "listo"}

    # 4. Página listo tiene la tarjeta con los valores
    r_listo = client.get(f"/comida/ia/{id_borrador}")
    assert r_listo.status_code == 200
    assert "Sopa de fideos" in r_listo.text
    assert "150" in r_listo.text
    assert 'name="a-0-incluir"' in r_listo.text
    assert 'name="a-0-producto"' in r_listo.text


def test_post_comida_ia_foto_jpeg(settings: Settings):
    hoy = date(2026, 10, 14)
    recibido: dict[str, Any] = {}

    def analizar_falso(texto: str, imagen: bytes | None) -> list[ia.AlimentoIA]:
        recibido["texto"] = texto
        recibido["imagen"] = imagen
        return [_alimento_test(nombre="Ensalada mixta")]

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: None,  # type: ignore
        hoy=lambda: hoy,
        analizar=analizar_falso,
    )
    client = TestClient(app)

    # Envío con cabecera image/jpeg y query params
    r = client.post(
        "/comida/ia?fecha=2026-10-14&momento=cena&texto=plato%20ligero",
        content=JPEG_MINIMO,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "url" in data
    assert data["url"].startswith("/comida/ia/")

    # Esperar al hilo
    id_borrador = int(data["url"].split("/")[-1])
    for _ in range(50):
        if client.get(f"/comida/ia/{id_borrador}/estado").json().get("estado") == "listo":
            break
        time.sleep(0.05)

    assert recibido["texto"] == "plato ligero"
    assert recibido["imagen"] == JPEG_MINIMO

    # Imagen no válida -> 415
    r_bad = client.post(
        "/comida/ia?fecha=2026-10-14&momento=cena&texto=algo",
        content=b"esto no es una imagen",
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_bad.status_code == 415

    # Sin texto ni foto -> error sin crear borrador
    r_vacio = client.post(
        "/comida/ia",
        data={"fecha": "2026-10-14", "momento": "comida", "texto": ""},
        follow_redirects=False,
    )
    assert r_vacio.status_code == 303
    assert "Escribe" in r_vacio.headers["location"]


def test_comida_ia_error_muestra_formulario(settings: Settings):
    hoy = date(2026, 10, 14)

    def analizar_con_error(_texto: str, _imagen: bytes | None) -> list[ia.AlimentoIA]:
        raise ia.ErrorIA("No he reconocido nada que se coma o se beba")

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: None,  # type: ignore
        hoy=lambda: hoy,
        analizar=analizar_con_error,
    )
    client = TestClient(app, follow_redirects=False)

    r_post = client.post(
        "/comida/ia",
        data={"fecha": "2026-10-14", "momento": "merienda", "texto": "teclado de pc"},
    )
    id_borrador = int(r_post.headers["location"].split("/")[-1])

    # Esperar al hilo
    for _ in range(50):
        if client.get(f"/comida/ia/{id_borrador}/estado").json().get("estado") == "error":
            break
        time.sleep(0.05)

    r_error = client.get(f"/comida/ia/{id_borrador}")
    assert r_error.status_code == 200
    assert "No he reconocido nada que se coma o se beba" in r_error.text
    # Conserva el texto enviado para reintentar
    assert "teclado de pc" in r_error.text


def test_anadir_comida_ia_guardar_y_producto(con, settings: Settings):
    hoy = date(2026, 10, 14)

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: None,  # type: ignore
        hoy=lambda: hoy,
    )
    client = TestClient(app, follow_redirects=False)

    # Creamos un borrador directamente en listo con 2 alimentos
    id_borrador = db.crear_borrador_comida(
        con,
        fecha="2026-10-14",
        momento="comida",
        texto="arroz y pollo",
        con_foto=False,
    )
    alimentos = [
        {
            "nombre": "Arroz blanco",
            "cantidad": 200.0,
            "unidad": "g",
            "kcal": 260.0,
            "proteinas": 5.0,
            "hidratos": 56.0,
            "grasas": 1.0,
            "momento": "comida",
            "aviso": None,
        },
        {
            "nombre": "Pechuga de pollo",
            "cantidad": 150.0,
            "unidad": "g",
            "kcal": 165.0,
            "proteinas": 35.0,
            "hidratos": 0.0,
            "grasas": 2.5,
            "momento": "comida",
            "aviso": None,
        },
    ]
    db.completar_borrador_comida(con, id_borrador, alimentos)

    # Guardamos solo el alimento 0 (arroz), marcando además "producto"
    # El alimento 1 no lleva "a-1-incluir"
    data_form = {
        "a-0-incluir": "1",
        "a-0-nombre": "Arroz blanco",
        "a-0-cantidad": "200",
        "a-0-unidad": "g",
        "a-0-momento": "comida",
        "a-0-kcal": "260",
        "a-0-proteinas": "5",
        "a-0-hidratos": "56",
        "a-0-grasas": "1",
        "a-0-producto": "1",
        "a-1-nombre": "Pechuga de pollo",
        "a-1-cantidad": "150",
        "a-1-unidad": "g",
        "a-1-momento": "comida",
    }
    r = client.post(f"/comida/ia/{id_borrador}/anadir", data=data_form)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/comida?fecha=2026-10-14")

    # Verificar que en la base de datos se añadió solo 1 comida
    comidas = db.comidas(con, "2026-10-14")
    assert len(comidas) == 1
    assert comidas[0].nombre == "Arroz blanco"
    assert comidas[0].cantidad == 200.0

    # Verificar que se creó en productos propios con valores por 100 g
    # 260 kcal / 200 g * 100 = 130 kcal/100 g
    prod = con.execute("SELECT * FROM productos_propios WHERE nombre = 'Arroz blanco'").fetchone()
    assert prod is not None
    assert prod["kcal"] == 130.0
    assert prod["proteinas"] == 2.5
    assert prod["origen"] == "comida-ia"
    assert prod["racion_g"] == 200.0

    # Verificar que el borrador fue eliminado
    assert db.leer_borrador_comida(con, id_borrador) is None

    # Probar validación con número malo
    id_b2 = db.crear_borrador_comida(
        con,
        fecha="2026-10-14",
        momento="comida",
        texto="tarta",
        con_foto=False,
    )
    db.completar_borrador_comida(
        con, id_b2, [{"nombre": "Tarta", "cantidad": 100.0, "unidad": "g"}]
    )
    r_bad_num = client.post(
        f"/comida/ia/{id_b2}/anadir",
        data={
            "a-0-incluir": "1",
            "a-0-nombre": "Tarta",
            "a-0-cantidad": "-50",
            "a-0-unidad": "g",
            "a-0-momento": "comida",
        },
    )
    assert r_bad_num.status_code == 303
    assert f"/comida/ia/{id_b2}" in r_bad_num.headers["location"]

    # Probar sin marcar ningún alimento
    r_ninguno = client.post(
        f"/comida/ia/{id_b2}/anadir",
        data={
            "a-0-nombre": "Tarta",
            "a-0-cantidad": "100",
            "a-0-unidad": "g",
            "a-0-momento": "comida",
        },
    )
    assert r_ninguno.status_code == 303
    assert (
        "No+has+marcado+ning" in r_ninguno.headers["location"]
        or "No has marcado ning" in r_ninguno.headers["location"]
    )


def test_descartar_comida_ia(con, settings: Settings):
    hoy = date(2026, 10, 14)
    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: None,  # type: ignore
        hoy=lambda: hoy,
    )
    client = TestClient(app, follow_redirects=False)

    id_borrador = db.crear_borrador_comida(
        con,
        fecha="2026-10-14",
        momento="cena",
        texto="pizza",
        con_foto=False,
    )
    r = client.post(f"/comida/ia/{id_borrador}/descartar")
    assert r.status_code == 303
    assert "/comida/ia?fecha=2026-10-14" in r.headers["location"]
    assert db.leer_borrador_comida(con, id_borrador) is None


def test_peticion_ia_valida_y_conserva_el_texto_al_volver(settings: Settings):
    from urllib.parse import parse_qs, urlparse

    from salud.web import peticion_ia

    hoy_ = date(2026, 1, 10)
    p = peticion_ia({"texto": " 5 cervezas ", "momento": "cena"}, hay_foto=False, hoy_=hoy_)
    assert (p.fecha, p.momento, p.texto) == ("2026-01-10", "cena", "5 cervezas")
    # Con foto, el texto puede ir vacío.
    assert peticion_ia({"momento": "comida"}, hay_foto=True, hoy_=hoy_).texto == ""
    for datos, error in (
        ({"texto": "x", "momento": "cena"}, "Escribe qué has comido"),
        ({"texto": "a" * 501, "momento": "cena"}, "demasiado largo"),
        ({"texto": "sopa", "momento": "cena", "fecha": "2026-01-11"}, "posterior a hoy"),
        ({"texto": "sopa", "momento": "almuerzo"}, "Momento no válido"),
    ):
        try:
            peticion_ia(datos, hay_foto=False, hoy_=hoy_)
        except ValueError as exc:
            assert error in str(exc)
        else:
            raise AssertionError(datos)

    # Al volver con un error, el texto llega entero aunque lleve & o #.
    c = TestClient(
        crear_app(
            settings,
            abrir_db=lambda: db.conectar(settings.db_path),
            conectar_garmin=lambda: None,  # type: ignore
            analizar=lambda _t, _img: [],
        )
    )
    r = c.post(
        "/comida/ia",
        data={"texto": "sopa & pan #1" + "x" * 500, "momento": "cena"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["texto"] == ["sopa & pan #1" + "x" * 500]
    assert query["momento"] == ["cena"]


def test_alimentos_desde_formulario():
    from salud.web import alimentos_desde_formulario, por_100

    datos = {
        "a-0-incluir": "1",
        "a-0-nombre": "Sopa",
        "a-0-cantidad": "150",
        "a-0-unidad": "g",
        "a-0-momento": "comida",
        "a-0-kcal": "59,5",
        "a-0-proteinas": "",
        "a-1-nombre": "Pan",  # sin marcar: no entra
        "a-1-cantidad": "40",
        "a-1-unidad": "g",
        "a-1-momento": "comida",
    }
    [sopa] = alimentos_desde_formulario(datos, 2)
    assert (sopa.nombre, sopa.cantidad, sopa.kcal, sopa.proteinas) == ("Sopa", 150, 59.5, None)
    assert not sopa.producto
    assert por_100(59.5, 150) == 39.7
    assert por_100(None, 150) is None
    for cambio, error in (
        ({"a-0-cantidad": "0"}, "Cantidad"),
        ({"a-0-unidad": "kg"}, "Unidad"),
        ({"a-0-kcal": "-3"}, "Valor no válido"),
        ({"a-0-kcal": "nan"}, "Valor no válido"),
        ({"a-0-nombre": ""}, "nombre"),
    ):
        try:
            alimentos_desde_formulario(datos | cambio, 2)
        except ValueError as exc:
            assert error in str(exc)
        else:
            raise AssertionError(cambio)
    sin_marcar = {k: v for k, v in datos.items() if k != "a-0-incluir"}
    try:
        alimentos_desde_formulario(sin_marcar, 2)
    except ValueError as exc:
        assert "ningún alimento" in str(exc)
    else:
        raise AssertionError("sin marcar")
