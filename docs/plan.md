# Plan

Hablado con Pelayo el 2026-10-07. Una app propia que centraliza salud y
deporte para un solo usuario. fitness-tracker se queda como está (MCP de
Garmin y FatSecret); esta app va aparte y puede usarlo.

## Decisiones

- **Solo para Pelayo.** Abrirla a otras personas sería otro proyecto: habría
  que guardar contraseñas ajenas de Garmin (librería no oficial), Strava exige
  revisión para más de un usuario y prohíbe usar sus datos con IA, y los datos
  de salud de terceros son categoría especial del RGPD.
- **Fuente principal: Garmin** (Forerunner 570, Edge 540, báscula Index).
  Galaxy Watch 8 Classic de vez en cuando.
- **Cada métrica con su fuente**, nunca mezcladas sin decirlo. Garmin manda;
  Samsung rellena los días sin Garmin.
- **Lectura diaria con Claude Code en calamar** (`claude -p` con la
  suscripción, sin API), guardada en la base de datos y visible en la app. La
  IA no escribe en ningún sitio por su cuenta: lo que proponga lo acepta
  Pelayo con un botón.
- **Acceso**: para la primera prueba, solo tailnet (Tailscale Serve). Pelayo
  no lleva Tailscale siempre activo en el móvil, así que la entrada
  definitiva será pública con el dominio `calamar-lab.dev` (ver abajo).

## Fases

1. **Panel "cómo estoy"** (hecho): Garmin a SQLite cada día, Hoy, Tendencias,
   Actividades y la lectura diaria.
2. **Peso y calendario**: objetivo y tendencia de peso; calendario con la
   semana tipo y las sugerencias aceptadas, que se mandan a Garmin (el
   cliente de fitness-tracker ya sabe agendar entrenos).
3. **Varios dispositivos**: Galaxy Watch por Health Connect con HC Webhook en
   el móvil (código abierto; manda JSON a una URL con cabecera propia). En
   Health Connect, quitar a Garmin Connect el permiso de escritura: HC Webhook
   no dice de qué app viene cada dato. Samsung no pasa la VFC a Health
   Connect, así que esos días la disposición para entrenar será propia y con
   menos datos. Carga propia (TSS/TRIMP → fitness, fatiga y forma) y
   disposición propia (VFC y FC en reposo frente a su media, sueño y carga).
4. **Comida**: escáner de códigos de barras (Open Food Facts) y diario de
   FatSecret; objetivos y consejos según lo que toque en el calendario.
5. **Conexiones y MCP propio**: página para conectar Garmin (formulario con
   el código de verificación en un segundo paso, solo tokens), FatSecret
   (OAuth con vuelta a la app en vez del PIN) y Strava; MCP de esta app para
   usarla desde Claude, que acabaría sustituyendo al de fitness-tracker.
6. **Registro médico**: analíticas e informes en PDF, valores extraídos con IA
   y revisados a mano antes de guardar; medicación, vacunas y citas.

## Entrada pública con el dominio

Hoy no cabe en Tailscale Funnel: Funnel solo admite los puertos 443, 8443 y
10000; el 443 es de Vaultwarden, el 10000 de finanzas y el 8443 lo usa
asistwenger (Serve). Y `calamar-lab.dev` no llega desde fuera por el CG-NAT
de DIGI (se probó con finanzas el 2026-10-01). Opciones:

- **A. Salir del CG-NAT** (1 €/mes en DIGI) y abrir el 443 en los dos
  routers. DNS a la IP de casa (actualizado con el token de Cloudflare que ya
  existe) y un proxy en calamar con el certificado comodín
  `*.calamar-lab.dev` que certbot ya sabe sacar. Lo más barato, cifrado hasta
  calamar, y valdría también para finanzas (liberando Funnel). Contras: abre
  un puerto de casa a Internet (habría que activar `ufw`, hoy inactivo) y la
  IP de casa queda en el DNS.
- **B. VPS pequeño como relé** (unos 4-5 €/mes): reenvía el TCP sin
  descifrar (por SNI) hasta calamar por Tailscale. La casa sigue cerrada y su
  IP oculta; el cifrado sigue llegando hasta calamar. Contras: otra máquina
  que mantener.
- **Cloudflare Tunnel: descartado**, por lo mismo que en finanzas: Cloudflare
  vería los datos de salud y LaLiga bloquea sus IP durante los partidos.

En cualquiera de las dos, la app necesita su propio login (Google como
finanzas, o passkey) y no fiarse de la cabecera de Tailscale; y el envío de
HC Webhook, una clave propia en una cabecera.

## Pendiente de decidir

- A o B para la entrada pública.
- Horas de los timers (ahora: lectura a las 08:15, que antes trae Garmin;
  Garmin otra vez a las 14:00 y a las 22:00).
