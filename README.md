# salud

Estado de salud y deporte centralizado. La idea es la misma que en finanzas:
**esta app guarda los datos y cada marca solo es una fuente**. Así da igual
qué reloj se lleve, el histórico y las métricas siguen aquí. Corre en
calamar y de momento solo se abre desde la tailnet.

Plan por fases, decisiones y pendientes: [docs/plan.md](docs/plan.md).

## Estado (fase 1)

- **Garmin** (Forerunner 570, Edge 540 y báscula Index) con la librería no
  oficial `garminconnect`. Cada día se guarda: resumen (pasos, FC en reposo,
  estrés, Body Battery, minutos de intensidad), sueño (duración, fases,
  puntuación, horas), VFC (noche, semana y franja equilibrada), preparación
  para entrenar, estado de entrenamiento y carga aguda/crónica, pesajes,
  actividades, VO2max, FTP y FC de umbral.
- **Lectura diaria con Claude** (ver abajo), guardada y visible en Hoy.
- **Web** instalable en el móvil: Hoy, Tendencias, Actividades y Lecturas.

## Datos

- Base de datos SQLite en `~/.local/share/salud/salud.db` (nunca en el repo).
- `crudos`: la respuesta tal cual de cada petición a Garmin y día. Si mañana
  interesa otra cifra, se saca de aquí sin volver a pedírsela a Garmin.
- `metricas`: un valor por día, métrica y **fuente**. Si hay varias fuentes
  para lo mismo, manda `manual` > `garmin` > `samsung` > `propia`.
- `actividades`: una fila por actividad y fuente.
- Las horas de Garmin son locales; el sueño de un día es el de la noche que
  termina ese día.

## Sincronización

- `salud sync` vuelve a pedir los últimos 3 días: Garmin completa el día
  cuando el reloj sincroniza. `salud sync --desde AAAA-MM-DD` trae un
  histórico (5 peticiones por día, con pausa para que Garmin no corte).
- Un fallo de Garmin en una petición no para el resto: la sincronización
  queda "parcial" con el detalle.
- **Sesión de Garmin**: solo tokens, nunca la contraseña. Se entra desde
  **Conexiones** en la web (correo y contraseña y, si Garmin lo pide, el
  código de verificación en un segundo paso que caduca a los 10 minutos) o
  con `salud garmin-entrar`; las dos usan el mismo código y guardan la
  sesión en `SALUD_GARMIN_TOKENS`. Tras 5 fallos seguidos la web no deja
  intentarlo durante 15 minutos. Para la primera prueba se
  puede apuntar a la de fitness-tracker (`~/.garminconnect`): comparten
  sesión, igual que ya hacen sus scripts.

## Lectura diaria

`salud lectura --sincronizar` (timer de cada mañana) trae Garmin y lanza
Claude Code sin pantalla (`claude -p`) con la **suscripción** de la sesión
iniciada en calamar, no con la API:

- Le pasa los últimos 14 días, las medias de 28, los últimos valores (peso,
  VO2max, FTP), las actividades y el perfil de `~/.config/salud/perfil.toml`
  (formato en `perfil.example.toml`).
- Con `SALUD_FITNESS_TRACKER`, puede consultar en **solo lectura** el MCP de
  fitness-tracker: nutrición de FatSecret y catálogo de MyWhoosh. Ninguna
  otra herramienta: ni comandos, ni ficheros, ni web.
- Corre en un directorio temporal fuera de la carpeta personal y sin cargar
  la configuración de nadie (`--setting-sources ""`): ni CLAUDE.md ni hooks.
- Devuelve JSON con un esquema fijo (estado verde/ámbar/rojo, titular,
  análisis, entreno, comida, alertas) que se guarda en `lecturas`. Si falla,
  queda el error y Hoy sigue enseñando los datos. Un fallo no pisa una
  lectura buena del mismo día.
- Desde Hoy se puede rehacer (tarda uno o dos minutos).

## Web

- `salud serve` en `127.0.0.1:8791`, detrás de Tailscale Serve. Solo pasan
  los logins de `SALUD_USUARIOS`; formularios de otras webs rechazados.
- Comida (`/comida`): diario de comidas del día agrupado por momentos,
  con búsqueda y escaneo de productos en Open Food Facts, cálculo automático de
  nutrientes, alimentos recientes y alta manual.
- Conexiones (`/conexiones`, icono de la cabecera): estado de la sesión de
  Garmin, probarla y volver a entrar. FatSecret y Strava, más adelante.
- Gráficas en SVG hechas en el servidor, de una serie cada una: línea de
  2 px, valor final rotulado, cursor con tooltip y tabla debajo. La franja
  gris de la VFC es la equilibrada según Garmin.
- Instalable (manifest + `sw.js`): las páginas van primero a la red y, sin
  conexión, se ve la última copia.

## Acceso

Dos entradas a la misma app, cada una en su proceso (`acceso.py`):

- **Pública** (`salud serve --publica`, `salud-publica.service`):
  `https://salud.calamar-lab.dev`, abierta a Internet por Cloudflare Tunnel
  hacia `127.0.0.1:8792`. No se fía de la cabecera de Tailscale. Se entra con
  Google (OpenID Connect, solo el correo) y solo las cuentas de
  `SALUD_CUENTAS_GOOGLE`. La sesión es una cookie (`Secure`, `HttpOnly`,
  `SameSite=Lax`, 90 días, con renovación si quedan menos de 30) con un token
  aleatorio del que se guarda el hash (tabla `sesiones_web`).
- **Tailnet** (`salud serve`, `salud.service`): detrás de Tailscale Serve
  (`:8791`), que añade `Tailscale-User-Login`. Solo pasan las cuentas de
  `SALUD_USUARIOS`. Queda de respaldo.
- **Protecciones**: se rechaza cualquier formulario que llegue desde otra web
  (`Sec-Fetch-Site` u `Origin`), ninguna página se deja meter en otra
  (`X-Frame-Options: DENY`), la web pública añade `Strict-Transport-Security`,
  y los intentos de entrar abandonados se borran a la hora.

## Configuración

`.env` (formato en `.env.example`) y, fuera del repo,
`~/.config/salud/perfil.toml`.

## Desarrollo

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q
```

Los tests usan datos inventados; nunca datos de salud reales.
