# Datos de alimentos genéricos

## Fuente

[CIQUAL 2020](https://ciqual.anses.fr/) (ANSES, Francia). Tabla de composición
nutricional de alimentos.

## Licencia

[Licence Ouverte / Open Licence](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)
(Etalab). Reutilizable citando la fuente:
`CIQUAL 2020 (ANSES), Licence Ouverte`.

## Generación

El fichero `ciqual_2020_es.csv` se genera a partir de los XML originales de CIQUAL:

```bash
salud ciqual --origen ~/.cache/salud/ciqual [--cache ciqual_traducciones.json]
```
