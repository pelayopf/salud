"""Tests de CIQUAL (datos y búsqueda de alimentos genéricos)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from salud.ciqual import (
    Generico,
    buscar,
    cargar,
    generar_csv,
    leer_xml,
    por_codigo,
    traducir,
    valor_teneur,
)
from salud.config import Settings
from salud.ia import RespuestaIA, Tarea


def test_valor_teneur() -> None:

    """1. Comprueba la conversión de teneur."""
    assert valor_teneur("3,6") == 3.6
    assert valor_teneur(" traces ") == 0.0
    assert valor_teneur("< 0,5") == 0.5
    assert valor_teneur("-") is None
    assert valor_teneur(None) is None


@pytest.fixture
def xml_ciqual_dir(tmp_path: Path) -> Path:
    """Crea tres XML pequeños con formato windows-1252 y valores con espacios."""
    alim_grp_xml = """<?xml version="1.0" encoding="windows-1252" ?>
<TABLE>
   <ALIM_GRP>
      <alim_grp_code> 01 </alim_grp_code>
      <alim_grp_nom_fr> lgumes </alim_grp_nom_fr>
      <alim_grp_nom_eng> vegetables </alim_grp_nom_eng>
      <alim_ssgrp_code> 0101 </alim_ssgrp_code>
   </ALIM_GRP>
   <ALIM_GRP>
      <alim_grp_code> 01 </alim_grp_code>
      <alim_grp_nom_fr> lgumes </alim_grp_nom_fr>
      <alim_grp_nom_eng> vegetables </alim_grp_nom_eng>
      <alim_ssgrp_code> 0102 </alim_ssgrp_code>
   </ALIM_GRP>
   <ALIM_GRP>
      <alim_grp_code> 02 </alim_grp_code>
      <alim_grp_nom_fr> fruits </alim_grp_nom_fr>
      <alim_grp_nom_eng> fruits </alim_grp_nom_eng>
      <alim_ssgrp_code> 0201 </alim_ssgrp_code>
   </ALIM_GRP>
   <ALIM_GRP>
      <alim_grp_code> 03 </alim_grp_code>
      <alim_grp_nom_fr> crales </alim_grp_nom_fr>
      <alim_grp_nom_eng> cereals </alim_grp_nom_eng>
      <alim_ssgrp_code> 0301 </alim_ssgrp_code>
   </ALIM_GRP>
</TABLE>
"""
    (tmp_path / "alim_grp_2020_07_07.xml").write_bytes(alim_grp_xml.encode("windows-1252"))

    alim_xml = """<?xml version="1.0" encoding="windows-1252" ?>
<TABLE>
   <ALIM>
      <alim_code> 1000 </alim_code>
      <alim_nom_fr> Cleri </alim_nom_fr>
      <alim_nom_eng> Celery </alim_nom_eng>
      <alim_grp_code> 01 </alim_grp_code>
   </ALIM>
   <ALIM>
      <alim_code> 1001 </alim_code>
      <alim_nom_fr> Pomme (<1 an) </alim_nom_fr>
      <alim_nom_eng> Apple </alim_nom_eng>
      <alim_grp_code> 02 </alim_grp_code>
   </ALIM>
   <ALIM>
      <alim_code> 1002 </alim_code>
      <alim_nom_fr> Pain & beurre </alim_nom_fr>
      <alim_nom_eng> Bread </alim_nom_eng>
      <alim_grp_code> 03 </alim_grp_code>
   </ALIM>
</TABLE>
"""
    (tmp_path / "alim_2020_07_07.xml").write_bytes(alim_xml.encode("windows-1252"))

    compo_xml = """<?xml version="1.0" encoding="windows-1252" ?>
<TABLE>
   <!-- 1000: Celery con 328 -->
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 328 </const_code>
      <teneur> 15 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 25000 </const_code>
      <teneur> 0,8 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 31000 </const_code>
      <teneur> < 2,5 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 40000 </const_code>
      <teneur> 0,2 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 34100 </const_code>
      <teneur> 1,6 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 32000 </const_code>
      <teneur> 1,3 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1000 </alim_code>
      <const_code> 10004 </const_code>
      <teneur> 0,2 </teneur>
   </COMPO>

   <!-- 1001: Apple sin 328, con 333 -->
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 333 </const_code>
      <teneur> 52 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 25000 </const_code>
      <teneur> 0,3 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 31000 </const_code>
      <teneur> 11,6 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 40000 </const_code>
      <teneur> 0,2 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 34100 </const_code>
      <teneur> 2,4 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 32000 </const_code>
      <teneur> 10,4 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1001 </alim_code>
      <const_code> 10004 </const_code>
      <teneur> traces </teneur>
   </COMPO>

   <!-- 1002: Bread -->
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 328 </const_code>
      <teneur> 265 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 25000 </const_code>
      <teneur> 9,0 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 31000 </const_code>
      <teneur> 49,0 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 40000 </const_code>
      <teneur> 1,5 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 34100 </const_code>
      <teneur> 3,0 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 32000 </const_code>
      <teneur> 2,0 </teneur>
   </COMPO>
   <COMPO>
      <alim_code> 1002 </alim_code>
      <const_code> 10004 </const_code>
      <teneur> 1,1 </teneur>
   </COMPO>
</TABLE>
"""
    (tmp_path / "compo_2020_07_07.xml").write_bytes(compo_xml.encode("windows-1252"))

    return tmp_path


def test_leer_xml(xml_ciqual_dir: Path) -> None:
    """2. Comprueba la lectura de alimentos de los XML con sus valores y grupos."""
    alimentos = leer_xml(xml_ciqual_dir)
    assert len(alimentos) == 3

    a1, a2, a3 = alimentos
    assert a1.codigo == "1000"
    assert a1.nombre == "Celery"
    assert a1.nombre_original == "Celery"
    assert a1.grupo == "vegetables"
    assert a1.kcal == 15.0
    assert a1.proteinas == 0.8
    assert a1.hidratos == 2.5
    assert a1.grasas == 0.2
    assert a1.fibra == 1.6
    assert a1.azucares == 1.3
    assert a1.sal == 0.2

    # a2 falta 328, toma 333 (52 kcal); sal es traces -> 0.0
    assert a2.codigo == "1001"
    assert a2.nombre == "Apple"
    assert a2.grupo == "fruits"
    assert a2.kcal == 52.0
    assert a2.sal == 0.0

    assert a3.codigo == "1002"
    assert a3.nombre == "Bread"
    assert a3.grupo == "cereals"
    assert a3.kcal == 265.0


def test_traducir(tmp_path: Path) -> None:
    """3. Comprueba la traducción por lotes, uso de caché e ignorar traducciones vacías."""
    llamadas = 0

    def fake_ejecutar(tarea: Tarea, _settings: Settings) -> RespuestaIA:
        nonlocal llamadas
        llamadas += 1
        lineas = tarea.instrucciones.split("\n")
        nombres = [linea[2:].strip() for linea in lineas if linea.startswith("- ")]
        trads = []
        for n in nombres:
            if n == "Empty":
                trads.append({"original": n, "es": ""})
            elif n == "Banana":
                trads.append({"original": n, "es": "Plátano"})
            elif n == "Apple":
                trads.append({"original": n, "es": "Manzana"})
            elif n == "Orange":
                trads.append({"original": n, "es": "Naranja"})
            elif n == "Bread":
                trads.append({"original": n, "es": "Pan"})
            else:
                trads.append({"original": n, "es": f"Trad_{n}"})
        return RespuestaIA(datos={"traducciones": trads}, motor="agy", modelo="m")

    settings = Settings(
        db_path=tmp_path / "test.db",
        garmin_tokens=tmp_path / "tokens.json",
        gcal_ics=None,
        ia_motor="agy",
        ia_modelo_agy="gemini-2.5-pro",
        ia_modelo_claude="claude-3-5-sonnet",
        agy="agy",
        claude="claude",
        agy_token=tmp_path / "token",
    )
    cache_path = tmp_path / "trads_cache.json"

    nombres = ["Banana", "Apple", "Orange", "Empty", "Bread"]
    # lote de 2 -> pendientes 5 -> lotes: [Banana, Apple], [Orange, Empty], [Bread] -> 3 llamadas
    res = traducir(nombres, settings, ejecutar=fake_ejecutar, lote=2, cache=cache_path)
    assert llamadas == 3
    assert res.get("Banana") == "Plátano"
    assert res.get("Apple") == "Manzana"
    assert res.get("Orange") == "Naranja"
    assert res.get("Bread") == "Pan"
    assert "Empty" not in res  # traducción vacía ignorada

    assert cache_path.exists()
    guardado = json.loads(cache_path.read_text(encoding="utf-8"))
    assert guardado.get("Banana") == "Plátano"

    # Segunda llamada con caché: no debe llamar a fake_ejecutar para los ya traducidos
    llamadas_previas = llamadas
    res2 = traducir(["Banana", "Apple"], settings, ejecutar=fake_ejecutar, lote=2, cache=cache_path)
    assert llamadas == llamadas_previas
    assert res2.get("Banana") == "Plátano"


def test_generar_csv_y_cargar(xml_ciqual_dir: Path, tmp_path: Path) -> None:
    """4. Comprueba generar_csv con ejecutar falso y lectura posterior con cargar."""
    diccionario_es = {
        "Celery": "Apio",
        "Apple": "Manzana",
        "Bread": "Pan",
        "vegetables": "Verduras",
        "fruits": "Frutas",
        "cereals": "Cereales",
    }

    def fake_ejecutar(tarea: Tarea, _settings: Settings) -> RespuestaIA:
        lineas = tarea.instrucciones.split("\n")
        nombres = [linea[2:].strip() for linea in lineas if linea.startswith("- ")]
        trads = [{"original": n, "es": diccionario_es.get(n, n)} for n in nombres]
        return RespuestaIA(
            datos={"traducciones": trads},
            motor="agy",
            modelo="m",
        )

    settings = Settings(
        db_path=tmp_path / "test.db",
        garmin_tokens=tmp_path / "tokens.json",
        gcal_ics=None,
        ia_motor="agy",
        ia_modelo_agy="gemini-2.5-pro",
        ia_modelo_claude="claude-3-5-sonnet",
        agy="agy",
        claude="claude",
        agy_token=tmp_path / "token",
    )
    destino_csv = tmp_path / "datos" / "ciqual.csv"
    filas = generar_csv(xml_ciqual_dir, settings, destino=destino_csv, ejecutar=fake_ejecutar)
    assert filas == 3
    assert destino_csv.exists()

    cargados = cargar(destino_csv)
    assert len(cargados) == 3

    assert cargados[0].codigo == "1000"
    assert cargados[0].nombre == "Apio"
    assert cargados[0].nombre_original == "Celery"
    assert cargados[0].grupo == "Verduras"
    assert cargados[0].kcal == 15.0

    assert cargados[1].codigo == "1001"
    assert cargados[1].nombre == "Manzana"
    assert cargados[1].grupo == "Frutas"
    assert cargados[1].kcal == 52.0

    assert cargados[2].codigo == "1002"
    assert cargados[2].nombre == "Pan"
    assert cargados[2].grupo == "Cereales"
    assert cargados[2].kcal == 265.0

    # Test por_codigo con la lista cargada
    p = por_codigo("1001", alimentos=cargados)
    assert p is not None
    assert p.nombre == "Manzana"
    assert por_codigo("9999", alimentos=cargados) is None


def test_buscar() -> None:
    """5. Comprueba la búsqueda por prefijos de palabras y ordenación."""
    inventada = [
        Generico(
            "1", "Plátano, crudo", "Banana, raw", "Frutas", 89.0, 1.1, 20.0, 0.3, 2.6, 12.0, 0.0
        ),
        Generico(
            "2", "Plátano, seco", "Banana, dried", "Frutas", 320.0, 3.9, 74.0, 1.8, 8.0, 47.0, 0.0
        ),
        Generico(
            "3",
            "Néctar de plátano",
            "Banana nectar",
            "Bebidas",
            55.0,
            0.3,
            13.0,
            0.1,
            0.5,
            12.0,
            0.0,
        ),
        Generico(
            "4",
            "Pan de centeno y trigo",
            "Rye and wheat bread",
            "Cereales",
            240.0,
            8.0,
            45.0,
            1.2,
            6.0,
            2.0,
            1.1,
        ),
        Generico(
            "5",
            "Pechuga de pollo, a la plancha",
            "Chicken breast, grilled",
            "Carnes",
            150.0,
            31.0,
            0.0,
            3.0,
            0.0,
            0.0,
            0.3,
        ),
    ]

    # "platano" -> los tres de plátano con "Plátano, crudo" primero
    res_platano = buscar("platano", alimentos=inventada)
    assert [a.nombre for a in res_platano] == [
        "Plátano, crudo",
        "Plátano, seco",
        "Néctar de plátano",
    ]

    # Coincidencia con prefijo "platan"
    res_prefijo = buscar("platan", alimentos=inventada)
    assert [a.nombre for a in res_prefijo] == [
        "Plátano, crudo",
        "Plátano, seco",
        "Néctar de plátano",
    ]

    # "pan cent" -> el pan
    res_pan = buscar("pan cent", alimentos=inventada)
    assert [a.nombre for a in res_pan] == ["Pan de centeno y trigo"]

    # "pollo plancha" -> la pechuga
    res_pollo = buscar("pollo plancha", alimentos=inventada)
    assert [a.nombre for a in res_pollo] == ["Pechuga de pollo, a la plancha"]

    # "xyz" -> []
    assert buscar("xyz", alimentos=inventada) == []

    # "" -> []
    assert buscar("", alimentos=inventada) == []

    # Búsqueda con palabras cortas de menos de 2 letras
    assert buscar("a", alimentos=inventada) == []


def test_kcal_desde_macros() -> None:
    from salud.ciqual import kcal_desde_macros

    assert kcal_desde_macros(1.3, 1.33, 0.2, 1.2) == 14.7
    assert kcal_desde_macros(1.0, 1.0, 1.0, None) == 17.0
    assert kcal_desde_macros(None, 1.0, 1.0, 1.0) is None
