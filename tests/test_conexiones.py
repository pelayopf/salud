"""Conexiones: entrar en Garmin desde la web con un cliente falso (sin red)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from salud import db, garmin
from salud.garmin import GarminNoConectado
from salud.web import BLOQUEO_S, CADUCIDAD_CODIGO_S, MAX_FALLOS, crear_app

CONTRASENA = "contrasena-inventada-xyz"
CODIGO = "123456"


class ClienteFalso:
    """Doble de `garminconnect.Garmin(..., return_on_mfa=True)`."""

    def __init__(self, correo: str, contrasena: str, *, pide_codigo: bool, rechaza: bool) -> None:
        self.correo = correo
        self.password: str | None = contrasena
        self.pide_codigo = pide_codigo
        self.rechaza = rechaza
        self.client = self  # los tokens los escribe `cliente.client.dump`

    def login(self) -> tuple[str | None, None]:
        if self.rechaza:
            # Como haría una librería descuidada: la contraseña en el error.
            raise RuntimeError(f"credenciales malas: {self.password}")
        return ("needs_mfa", None) if self.pide_codigo else (None, None)

    def resume_login(self, _estado: dict[str, Any], codigo: str) -> tuple[None, None]:
        if codigo != CODIGO:
            raise RuntimeError("código malo")
        return None, None

    def dump(self, ruta: str) -> None:
        (Path(ruta) / "garmin_tokens.json").write_text("{}")


class Reloj:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def creados() -> list[ClienteFalso]:
    return []


def _cliente_web(
    settings,
    reloj: Reloj,
    creados: list[ClienteFalso],
    *,
    pide_codigo: bool = False,
    rechaza: bool = False,
    sesion_valida: bool = True,
) -> TestClient:
    def crear(correo: str, contrasena: str) -> ClienteFalso:
        c = ClienteFalso(correo, contrasena, pide_codigo=pide_codigo, rechaza=rechaza)
        creados.append(c)
        return c

    def conectar() -> Any:
        if not sesion_valida or not garmin.fichero_tokens(settings.garmin_tokens).is_file():
            raise GarminNoConectado("No hay sesión de Garmin: entra desde Conexiones")
        return object()

    app = crear_app(
        settings,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=conectar,
        hoy=lambda: date(2026, 1, 10),
        crear_cliente_garmin=crear,
        reloj=reloj,
    )
    return TestClient(app, follow_redirects=False)


def _entrar(c: TestClient, contrasena: str = CONTRASENA):
    return c.post(
        "/conexiones/garmin/entrar", data={"correo": "yo@ejemplo.test", "contrasena": contrasena}
    )


def _id(texto: str) -> str:
    m = re.search(r'name="id" value="([^"]+)"', texto)
    assert m, "no está el formulario del código"
    return m.group(1)


def test_sin_sesion(settings, reloj, creados):
    r = _cliente_web(settings, reloj, creados).get("/conexiones")
    assert r.status_code == 200
    assert "Sin conectar" in r.text and "No hay sesión guardada" in r.text
    assert 'action="/conexiones/garmin/entrar"' in r.text
    assert 'href="/conexiones"' in r.text  # enlace de la cabecera


def test_entrar_sin_codigo(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados)
    r = _entrar(c)
    assert r.status_code == 303 and "Sesi%C3%B3n+de+Garmin+guardada" in r.headers["location"]
    fichero = settings.garmin_tokens / "garmin_tokens.json"
    assert fichero.is_file()
    assert settings.garmin_tokens.stat().st_mode & 0o777 == 0o700
    assert creados[0].password is None  # no se queda en memoria
    r = c.get("/conexiones")
    assert "Conectado" in r.text and "Probar conexión" in r.text


def test_entrar_con_codigo(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados, pide_codigo=True)
    r = _entrar(c)
    assert r.status_code == 200 and "código de verificación" in r.text
    assert creados[0].password is None
    assert not (settings.garmin_tokens / "garmin_tokens.json").exists()
    r = c.post("/conexiones/garmin/codigo", data={"id": _id(r.text), "codigo": CODIGO})
    assert r.status_code == 303
    assert (settings.garmin_tokens / "garmin_tokens.json").is_file()


def test_codigo_incorrecto_se_puede_reintentar(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados, pide_codigo=True)
    id_ = _id(_entrar(c).text)
    r = c.post("/conexiones/garmin/codigo", data={"id": id_, "codigo": "000000"})
    assert "Garmin no ha aceptado el código" in r.text
    assert _id(r.text) == id_
    r = c.post("/conexiones/garmin/codigo", data={"id": id_, "codigo": CODIGO})
    assert r.status_code == 303


def test_codigo_caducado(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados, pide_codigo=True)
    id_ = _id(_entrar(c).text)
    reloj.t += CADUCIDAD_CODIGO_S + 1
    r = c.post("/conexiones/garmin/codigo", data={"id": id_, "codigo": CODIGO})
    assert "ha caducado" in r.text
    assert not (settings.garmin_tokens / "garmin_tokens.json").exists()
    # Y ya no vale aunque se vuelva atrás en el tiempo: se ha descartado.
    reloj.t -= CADUCIDAD_CODIGO_S
    r = c.post("/conexiones/garmin/codigo", data={"id": id_, "codigo": CODIGO})
    assert "ha caducado" in r.text


def test_codigo_con_otro_id(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados, pide_codigo=True)
    _entrar(c)
    r = c.post("/conexiones/garmin/codigo", data={"id": "inventado", "codigo": CODIGO})
    assert "ha caducado" in r.text
    assert not (settings.garmin_tokens / "garmin_tokens.json").exists()


def test_bloqueo_tras_varios_fallos(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados, rechaza=True)
    for _ in range(MAX_FALLOS - 1):
        assert "no ha aceptado el correo o la contraseña" in _entrar(c).text
    assert "Demasiados intentos" in _entrar(c).text
    n = len(creados)
    assert "Demasiados intentos" in _entrar(c).text
    assert len(creados) == n  # bloqueado: ni se intenta
    assert "Podrás volver a intentarlo" in c.get("/conexiones").text
    reloj.t += BLOQUEO_S + 1
    _entrar(c)
    assert len(creados) == n + 1


def test_la_contrasena_no_sale_en_ningun_sitio(settings, reloj, creados, caplog):
    c = _cliente_web(settings, reloj, creados, rechaza=True)
    respuestas = [_entrar(c), c.get("/conexiones")]
    for r in respuestas:
        assert CONTRASENA not in r.text
    assert CONTRASENA not in caplog.text
    assert "RuntimeError" in caplog.text


def test_faltan_datos(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados)
    r = c.post("/conexiones/garmin/entrar", data={"correo": "yo@ejemplo.test", "contrasena": ""})
    assert "Escribe el correo y la contraseña" in r.text
    assert creados == []


def test_formulario_de_otra_web(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados)
    r = c.post(
        "/conexiones/garmin/entrar",
        data={"correo": "yo@ejemplo.test", "contrasena": CONTRASENA},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 403
    assert creados == []


def test_probar_conexion(settings, reloj, creados):
    c = _cliente_web(settings, reloj, creados)
    r = c.post("/conexiones/garmin/probar")
    assert "entra+desde+Conexiones" in r.headers["location"]
    _entrar(c)
    r = c.post("/conexiones/garmin/probar")
    assert "funciona" in r.headers["location"]


def test_empezar_entrada_borra_la_contrasena_aunque_falle(tmp_path):
    creado: list[ClienteFalso] = []

    def crear(correo: str, contrasena: str) -> ClienteFalso:
        creado.append(ClienteFalso(correo, contrasena, pide_codigo=False, rechaza=True))
        return creado[0]

    with pytest.raises(RuntimeError):
        garmin.empezar_entrada(tmp_path / "g", "yo@ejemplo.test", CONTRASENA, crear)
    assert creado[0].password is None
    assert not (tmp_path / "g").exists()
