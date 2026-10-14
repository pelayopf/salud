"""Quién entra en la web.

De momento solo hay una entrada, la de la tailnet: detrás de Tailscale
Serve, que añade `Tailscale-User-Login` con la identidad de quien llama.
Solo pasan los de `SALUD_USUARIOS` (vacío, cualquiera de la tailnet). La web
escucha solo en 127.0.0.1, así que nadie más puede inventarse esa cabecera.

Una entrada pública (dominio propio con login, como la de finanzas) no se
fiaría de la cabecera y llevaría su propio login: ver docs/plan.md.

Se rechazan los formularios enviados desde otra web (CSRF): el navegador
dice de dónde viene la petición en `Sec-Fetch-Site` o en `Origin`. Y ninguna
página se deja meter dentro de otra web.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

CABECERAS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
_SEGUROS = ("GET", "HEAD", "OPTIONS")


def mismo_origen(request: Request) -> bool:
    """Si el formulario sale de esta misma web (sin cabeceras, no es un navegador)."""
    sitio = request.headers.get("sec-fetch-site")
    if sitio:
        return sitio in ("same-origin", "none")
    origen = request.headers.get("origin")
    if origen:
        return urlsplit(origen).netloc == request.headers.get("host")
    return True


class AccesoMiddleware(BaseHTTPMiddleware):
    """Deja pasar solo a los usuarios de la tailnet permitidos."""

    def __init__(self, app, *, usuarios: frozenset[str]) -> None:
        """Guarda quién puede entrar."""
        super().__init__(app)
        self._usuarios = usuarios

    async def dispatch(self, request: Request, call_next) -> Response:
        """Comprueba el origen del formulario y quién llama."""
        respuesta = await self._atender(request, call_next)
        respuesta.headers.update(CABECERAS)
        return respuesta

    async def _atender(self, request: Request, call_next) -> Response:
        if request.method not in _SEGUROS and not mismo_origen(request):
            return PlainTextResponse("Formulario enviado desde otra web", status_code=403)
        login = request.headers.get("tailscale-user-login", "").lower()
        if self._usuarios and login not in self._usuarios:
            return PlainTextResponse("Acceso no permitido", status_code=403)
        return await call_next(request)
