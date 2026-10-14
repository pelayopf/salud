from __future__ import annotations

import argparse
import base64
import dataclasses
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import GarminFalso
from starlette.testclient import TestClient

from salud import acceso, db
from salud.cli import cmd_serve
from salud.config import ConfigError
from salud.web import crear_app

PUBLICA = "https://salud.calamar-lab.dev"
AHORA = datetime(2026, 10, 1, tzinfo=UTC)


def _id_token(**datos) -> str:
    carga = {
        "aud": "id",
        "iss": "https://accounts.google.com",
        "exp": int(AHORA.timestamp()) + 600,
        "email": "Ana@Example.com",
        "email_verified": True,
        **datos,
    }
    cuerpo = base64.urlsafe_b64encode(json.dumps(carga).encode()).decode().rstrip("=")
    return f"cabecera.{cuerpo}.firma"


def test_el_correo_sale_del_id_token_si_es_para_esta_aplicacion():
    assert acceso.correo_de_id_token(_id_token(), "id", AHORA) == "ana@example.com"
    for malo in (
        _id_token(aud="otra"),
        _id_token(iss="https://evil.example"),
        _id_token(exp=int(AHORA.timestamp()) - 1),
        _id_token(email_verified=False),
        "no-es-un-token",
        "a.b",  # no tiene 3 partes
    ):
        with pytest.raises(acceso.LoginError):
            acceso.correo_de_id_token(malo, "id", AHORA)


class LoginFalso:
    def __init__(self, correo: str) -> None:
        self.correo_login = correo

    def correo(self, code: str, redirect: str) -> str:
        assert (code, redirect) == ("codigo", f"{PUBLICA}{acceso.RUTA_CALLBACK}")
        return self.correo_login


@pytest.fixture
def publica(settings):
    """La web pública con cuentas de Google autorizadas."""
    ajustes = dataclasses.replace(
        settings,
        url_publica=PUBLICA,
        cuentas_google=frozenset({"ana@example.com", "pelayo@example.com"}),
    )
    garmin = GarminFalso()
    login = LoginFalso("ana@example.com")

    def abrir_publica(correo: str | None = None) -> TestClient:
        if correo:
            login.correo_login = correo
        app = crear_app(
            ajustes,
            abrir_db=lambda: db.conectar(settings.db_path),
            conectar_garmin=lambda: garmin,
            publica=True,
            crear_login=lambda _id, _secreto: login,
        )
        return TestClient(app, base_url=PUBLICA)

    return abrir_publica, db.conectar(settings.db_path)


def _entrar(http: TestClient):
    r = http.get("/entrar/google", follow_redirects=False)
    destino = urlsplit(r.headers["location"])
    assert destino.netloc == "accounts.google.com"
    state = parse_qs(destino.query)["state"][0]
    return http.get(acceso.RUTA_CALLBACK, params={"code": "codigo", "state": state})


def test_sin_sesion_se_va_a_entrar_y_la_cabecera_de_tailscale_no_vale(publica):
    abrir, _ = publica
    http = abrir()
    r = http.get("/", headers={"Tailscale-User-Login": "ana@example.com"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/entrar")
    entrar_html = http.get("/entrar")
    assert "Entrar con Google" in entrar_html.text
    assert entrar_html.headers["x-frame-options"] == "DENY"
    assert entrar_html.headers["strict-transport-security"] == "max-age=31536000"

    # Recursos estáticos y manifiesto pasan sin sesión
    assert http.get("/estaticos/estilos.css").status_code == 200
    assert http.get("/manifest.webmanifest").status_code == 200

    # Petición JSON / fetch da 401
    assert http.get("/comida/consejo/estado").status_code == 401


def test_entrar_con_google_y_salir(publica):
    abrir, _ = publica
    http = abrir()
    inicio = _entrar(http)
    assert inicio.status_code == 200
    assert inicio.headers["x-frame-options"] == "DENY"
    assert inicio.headers["strict-transport-security"] == "max-age=31536000"

    # En conexiones se ve el usuario y el botón de salir
    conexiones = http.get("/conexiones")
    assert conexiones.status_code == 200
    assert "ana@example.com" in conexiones.text
    assert "Salir" in conexiones.text

    # Salir borra la sesión y vuelve a entrar
    r_salir = http.post("/salir", follow_redirects=False)
    assert r_salir.status_code == 303
    assert r_salir.headers["location"] == "/entrar?mensaje=Has+salido"
    assert http.get("/", follow_redirects=False).status_code == 303


def test_solo_entran_los_correos_con_acceso(publica):
    abrir, _ = publica
    http = abrir("otra@example.com")
    r = _entrar(http)
    assert r.url.path == "/entrar"
    assert "Esa cuenta no tiene acceso" in r.text
    assert http.get("/", follow_redirects=False).status_code == 303


def test_el_state_que_no_coincide_da_error(publica):
    abrir, _ = publica
    http = abrir()
    # Sin pasar por /entrar/google o con state inventado
    r = http.get(acceso.RUTA_CALLBACK, params={"code": "codigo", "state": "inventado"})
    assert r.url.path == "/entrar"
    assert "Esa entrada ya no vale" in r.text
    assert http.get("/", follow_redirects=False).status_code == 303


def test_sesion_caducada_y_renovacion(publica):
    abrir, con = publica
    http = abrir()
    _entrar(http)

    # Sesión caducada: la adelantamos al pasado
    with con:
        con.execute("UPDATE sesiones_web SET caduca = '2020-01-01T00:00:00+00:00'")
    r = http.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/entrar"

    # Renovación: creamos una sesión con 10 días restantes y la ponemos como cookie
    token = db.crear_sesion_web(con, "ana@example.com", dias=10)
    http.cookies.set(acceso.COOKIE_SESION, token)
    r_ok = http.get("/")
    assert r_ok.status_code == 200

    # Comprobamos que se renovó a ~90 días
    fila = con.execute(
        "SELECT caduca FROM sesiones_web WHERE correo = 'ana@example.com'"
    ).fetchone()
    assert fila is not None
    dt_caduca = db.datetime.fromisoformat(fila["caduca"])
    dias_restantes = (dt_caduca - db.datetime.now(db.UTC)).total_seconds() / 86400
    assert 88 < dias_restantes <= 90


def test_tailnet_sigue_igual(settings):
    """La tailnet sigue comprobando SALUD_USUARIOS y no pide login de Google."""
    ajustes = dataclasses.replace(settings, usuarios=frozenset({"yo@ejemplo.com"}))
    app = crear_app(
        ajustes,
        abrir_db=lambda: db.conectar(settings.db_path),
        conectar_garmin=lambda: GarminFalso(),
        publica=False,
    )
    http = TestClient(app)
    assert http.get("/").status_code == 403
    r = http.get("/", headers={"Tailscale-User-Login": "yo@ejemplo.com"})
    assert r.status_code == 200
    # No lleva HSTS en la tailnet
    assert "strict-transport-security" not in r.headers


def test_cmd_serve_publica_sin_configuracion_da_config_error(settings, tmp_path):
    # Sin url_publica
    args = argparse.Namespace(publica=True, host="127.0.0.1", port=8792)
    s1 = dataclasses.replace(
        settings,
        url_publica="",
        cuentas_google=frozenset({"a@b.com"}),
        google_oauth=tmp_path / "x.json",
    )
    with pytest.raises(ConfigError):
        cmd_serve(s1, args)

    # Sin cuentas_google
    s2 = dataclasses.replace(
        settings,
        url_publica="https://ejemplo.com",
        cuentas_google=frozenset(),
        google_oauth=tmp_path / "x.json",
    )
    with pytest.raises(ConfigError):
        cmd_serve(s2, args)

    # Sin fichero google_oauth
    s3 = dataclasses.replace(
        settings,
        url_publica="https://ejemplo.com",
        cuentas_google=frozenset({"a@b.com"}),
        google_oauth=tmp_path / "no-existe.json",
    )
    with pytest.raises(ConfigError):
        cmd_serve(s3, args)
