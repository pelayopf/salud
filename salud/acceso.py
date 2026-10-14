"""Quién entra en la web y qué puede ver.

Hay dos entradas a la misma aplicación, cada una en su proceso:

- **Tailnet** (`salud serve`): detrás de Tailscale Serve, que añade
  `Tailscale-User-Login` con la identidad de quien llama. Solo pasan los de
  `SALUD_USUARIOS` (vacío, cualquiera de la tailnet).
- **Pública** (`salud serve --publica`): detrás de Cloudflare Tunnel,
  abierta a Internet. No se fía de esa cabecera, que cualquiera podría
  inventarse. Se entra con Google (OpenID Connect, solo el correo) y solo
  con los correos de `SALUD_CUENTAS_GOOGLE`. La sesión es una cookie con
  un token aleatorio del que la base de datos guarda el hash.

En las dos entradas se rechazan los formularios enviados desde otra web
(CSRF): el navegador dice de dónde viene la petición en `Sec-Fetch-Site` o
en `Origin`. Y ninguna página se deja meter dentro de otra web.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from . import db
from .config import ConfigError

URL_AUTORIZACION = "https://accounts.google.com/o/oauth2/v2/auth"
URL_TOKEN = "https://oauth2.googleapis.com/token"
COOKIE_SESION = "salud_sesion"
COOKIE_STATE = "salud_oauth_state"
RUTA_CALLBACK = "/entrar/google/vuelta"
EMISORES_GOOGLE = ("https://accounts.google.com", "accounts.google.com")

PUBLICAS = (
    "/entrar",
    "/entrar/google",
    RUTA_CALLBACK,
    "/manifest.webmanifest",
    "/sw.js",
)

CABECERAS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
HSTS = {"Strict-Transport-Security": "max-age=31536000"}
_SEGUROS = ("GET", "HEAD", "OPTIONS")


class LoginError(Exception):
    """Google no confirmó la identidad."""


@dataclass(frozen=True)
class Acceso:
    """Quién hace la petición y por qué entrada."""

    correo: str
    publica: bool


_ACTUAL: ContextVar[Acceso] = ContextVar("acceso", default=Acceso("", publica=False))


def actual() -> Acceso:
    """El acceso de la petición en curso."""
    return _ACTUAL.get()


def mismo_origen(request: Request) -> bool:
    """Si el formulario sale de esta misma web (sin cabeceras, no es un navegador)."""
    sitio = request.headers.get("sec-fetch-site")
    if sitio:
        return sitio in ("same-origin", "none")
    origen = request.headers.get("origin")
    if origen:
        return urlsplit(origen).netloc == request.headers.get("host")
    return True


def url_login(client_id: str, redirect: str, state: str) -> str:
    """Adónde mandar a quien entra para que Google diga su correo."""
    return f"{URL_AUTORIZACION}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "openid email",
            "prompt": "select_account",
            "state": state,
        }
    )


def leer_google_oauth(ruta: Path | None) -> tuple[str, str]:
    """El client_id y el client_secret del JSON `{"client_id", "client_secret"}`."""
    if ruta is None or not ruta.is_file():
        raise ConfigError(f"No existe el fichero de OAuth de Google: {ruta}")
    try:
        datos = json.loads(ruta.read_text())
        client_id, client_secret = str(datos["client_id"]), str(datos["client_secret"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"Fichero de OAuth de Google inválido: {ruta}") from exc
    if not client_id or not client_secret:
        raise ConfigError(f"Fichero de OAuth de Google sin client_id o client_secret: {ruta}")
    return client_id, client_secret


class Login(Protocol):
    """Lo que se usa de GoogleLogin (permite un doble en los tests)."""

    def correo(self, code: str, redirect: str) -> str:
        """El correo verificado de quien entró."""
        ...


class GoogleLogin:
    """Canjea el código de Google por el `id_token` y lee de él el correo."""

    def __init__(
        self, client_id: str, client_secret: str, *, http: httpx.Client | None = None
    ) -> None:
        """Prepara el cliente con el id y el secreto de la aplicación de Google."""
        self._id = client_id
        self._secreto = client_secret
        self._http = http or httpx.Client(timeout=30)

    def correo(self, code: str, redirect: str) -> str:
        """El correo verificado de quien entró."""
        r = self._http.post(
            URL_TOKEN,
            data={
                "client_id": self._id,
                "client_secret": self._secreto,
                "code": code,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            },
        )
        if r.status_code != 200:
            raise LoginError(f"Google respondió {r.status_code}: {r.text[:200]}")
        token = r.json().get("id_token")
        if not token:
            raise LoginError("Google no devolvió la identidad")
        return correo_de_id_token(token, self._id)


def correo_de_id_token(token: str, client_id: str, ahora: datetime | None = None) -> str:
    """El correo de un `id_token`.

    Llega directamente de Google por HTTPS, así que no hace falta comprobar
    la firma (OpenID Connect, 3.1.3.7); sí que es para esta aplicación, de
    Google, vigente y con el correo verificado.
    """
    try:
        partes = token.split(".")
        if len(partes) != 3:
            raise ValueError("Token JWT no tiene 3 partes")
        carga = partes[1]
        datos: dict[str, Any] = json.loads(
            base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4))
        )
    except (IndexError, ValueError) as exc:
        raise LoginError("Identidad de Google ilegible") from exc
    ahora = ahora or datetime.now(UTC)
    if datos.get("aud") != client_id or datos.get("iss") not in EMISORES_GOOGLE:
        raise LoginError("La identidad no es para esta aplicación")
    if int(datos.get("exp", 0)) < ahora.timestamp():
        raise LoginError("La identidad de Google caducó")
    if not datos.get("email") or datos.get("email_verified") is not True:
        raise LoginError("Google no confirma el correo")
    return str(datos["email"]).lower()


def _empieza(ruta: str, prefijos: tuple[str, ...]) -> bool:
    return any(ruta == p or ruta.startswith(f"{p}/") for p in prefijos)


def _es_peticion_datos(request: Request) -> bool:
    """Si la petición espera una respuesta de datos (JSON o fetch) en vez de HTML."""
    if request.url.path == "/comida/consejo/estado":
        return True
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return True
    if request.headers.get("sec-fetch-dest") == "empty":
        return True
    return False


class AccesoMiddleware(BaseHTTPMiddleware):
    """Decide quién entra y aplica seguridad para la tailnet o la web pública."""

    def __init__(
        self,
        app,
        *,
        publica: bool = False,
        usuarios: frozenset[str] = frozenset(),
        cuentas_google: frozenset[str] = frozenset(),
        abrir_db: Callable[[], db.sqlite3.Connection] | None = None,
    ) -> None:
        """Guarda la entrada (pública o tailnet) y quién puede entrar."""
        super().__init__(app)
        self._publica = publica
        self._usuarios = usuarios
        self._cuentas_google = cuentas_google
        self._abrir_db = abrir_db

    async def dispatch(self, request: Request, call_next) -> Response:
        """Comprueba el origen del formulario, cabeceras e identidad."""
        respuesta = await self._atender(request, call_next)
        respuesta.headers.update(CABECERAS)
        if self._publica:
            respuesta.headers.update(HSTS)
        return respuesta

    async def _atender(self, request: Request, call_next) -> Response:
        if request.method not in _SEGUROS and not mismo_origen(request):
            return PlainTextResponse("Formulario enviado desde otra web", status_code=403)
        ruta = request.url.path
        if self._publica:
            if _empieza(ruta, PUBLICAS) or ruta.startswith("/estaticos/"):
                return await call_next(request)
            correo = self._desde_sesion(request)
            if correo is None:
                if request.method not in _SEGUROS or _es_peticion_datos(request):
                    return PlainTextResponse("Hay que entrar de nuevo", status_code=401)
                return RedirectResponse("/entrar", status_code=303)
            acceso = Acceso(correo, publica=True)
        else:
            login = request.headers.get("tailscale-user-login", "").lower()
            if self._usuarios and login not in self._usuarios:
                return PlainTextResponse("Acceso no permitido", status_code=403)
            acceso = Acceso(login, publica=False)

        marca = _ACTUAL.set(acceso)
        try:
            return await call_next(request)
        finally:
            _ACTUAL.reset(marca)

    def _desde_sesion(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE_SESION)
        if not token or self._abrir_db is None:
            return None
        con = self._abrir_db()
        try:
            correo = db.correo_de_sesion_web(con, token)
        finally:
            con.close()
        if correo and correo in self._cuentas_google:
            return correo
        return None
