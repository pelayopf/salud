# salud

App personal de Pelayo que centraliza su salud y deporte (Garmin ahora;
Samsung, comida y registro médico más adelante). Ver README.md y el plan en
docs/plan.md.

- Datos de salud reales: nunca al repo, ni en tests ni en fixtures. Los tests
  usan datos inventados.
- Garmin corta si se le pide mucho: nada de `salud sync --desde` largos en
  bucle al probar. La sesión de Garmin son tokens; nunca guardar la contraseña.
- `salud lectura` gasta cupo de la suscripción de Claude de Pelayo: no
  lanzarla en bucle.
- Verificación: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`.
- Commits: Conventional Commits en castellano (scopes en `.gitmessage`),
  email `pelayopf@gmail.com`.
