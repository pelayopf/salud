import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from conftest import hay

from salud import db, lectura, sync
from salud.lectura import LecturaError

BUENA = {
    "estado": "verde",
    "titular": "Recuperado y con ganas",
    "analisis": "VFC 60 ms, dentro de tu franja.",
    "entreno": {
        "titulo": "Rodaje Z2",
        "detalle": "60 min a 150-170 W",
        "duracion_min": 60,
        "intensidad": "suave",
    },
    "comida": "Hidratos antes del rodaje.",
    "alertas": [],
}


def _salida(contenido=BUENA, **extra) -> str:
    return json.dumps(
        {"type": "result", "is_error": False, "result": json.dumps(contenido)}
        | {"structured_output": contenido}
        | extra
    )


@pytest.fixture
def con_datos(con, garmin):
    sync.sincronizar(con, garmin, date(2026, 1, 8), date(2026, 1, 10), pausa=0)
    return con


def test_contexto_trae_dias_medias_actividades_y_ultimos(con_datos):
    ctx = lectura.contexto(con_datos, date(2026, 1, 10))
    assert ctx["hoy"] == "sábado 10 de enero de 2026"
    assert len(ctx["dias"]) == lectura.DIAS_CONTEXTO
    hoy = ctx["dias"][-1]
    assert hoy["fecha"] == "2026-01-10" and hoy["dia"] == "sábado"
    assert hoy["vfc_noche"] == "60 (equilibrada)"
    assert hoy["sueno_inicio"] == "23:30"
    assert hoy["estado_entreno"] == "productivo"
    assert ctx["medias_28_dias"]["fc_reposo"] == 50
    assert ctx["ultimos_valores"]["ftp"] == {"valor": 230, "fecha": "2026-01-02"}
    assert ctx["ultimos_valores"]["vfc_franja_equilibrada"] == "50-70 ms"
    assert {a["tipo"] for a in ctx["actividades"]} == {"Ciclismo", "Fútbol"}
    assert ctx["ultima_sincronizacion_garmin"]["estado"] == "ok"


def test_comando_sin_herramientas_ni_configuracion(settings):
    orden = lectura.comando(settings, None)
    assert orden[:2] == ["claude", "-p"]
    assert orden[orden.index("--tools") + 1] == ""
    assert orden[orden.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in orden and "--no-session-persistence" in orden
    assert "--mcp-config" not in orden and "--allowedTools" not in orden

    con_mcp = lectura.comando(settings, Path("/tmp/mcp.json"))
    permitidas = con_mcp[con_mcp.index("--allowedTools") + 1].split(",")
    assert permitidas == [
        "mcp__fitness__nutricion",
        "mcp__fitness__objetivos_nutricion",
        "mcp__fitness__recomendar_entreno",
    ]


def test_mcp_de_fitness_en_solo_lectura(settings, tmp_path):
    assert lectura.configuracion_mcp(settings) is None
    conf = lectura.configuracion_mcp(replace(settings, fitness_tracker=tmp_path, uv="/bin/uv"))
    servidor = hay(conf)["mcpServers"]["fitness"]
    assert servidor["command"] == "/bin/uv"
    assert servidor["args"] == ["run", "--directory", str(tmp_path), "fitness-mcp"]
    assert servidor["env"]["FITNESS_READONLY"] == "1"


def test_generar_guarda_la_lectura(con_datos, settings):
    settings.perfil_path.write_text('objetivo = "inventado: bajar a 78 kg"\n')
    recibido = {}

    def ejecutor(orden, entrada, directorio):
        recibido.update(orden=orden, entrada=entrada, directorio=directorio)
        assert directorio.is_dir() and not str(directorio).startswith(str(Path.home()))
        return _salida()

    contenido = lectura.generar(con_datos, settings, date(2026, 1, 10), ejecutor)
    assert contenido == BUENA
    assert "bajar a 78 kg" in recibido["entrada"]
    assert "No tienes herramientas" in recibido["entrada"]
    guardada = hay(db.lectura(con_datos, "2026-01-10"))
    assert guardada["estado"] == "ok" and guardada["contenido"] == BUENA
    assert guardada["modelo"] == "sonnet"


def test_sin_perfil_se_avisa():
    assert "No hay perfil" in lectura.perfil(Path("/no/existe.toml"))


@pytest.mark.parametrize(
    "salida, error",
    [
        ("no es json", "no es JSON"),
        (json.dumps({"is_error": True, "result": "Not logged in"}), "Not logged in"),
        (_salida({k: v for k, v in BUENA.items() if k != "comida"}), "falta: comida"),
        (_salida(BUENA | {"estado": "azul"}), "Estado desconocido"),
        (json.dumps({"is_error": False, "result": "texto suelto"}), "no trae la lectura"),
    ],
)
def test_interpretar_rechaza_respuestas_malas(salida, error):
    with pytest.raises(LecturaError, match=error):
        lectura.interpretar(salida)


def test_interpretar_acepta_el_json_en_result():
    assert (
        lectura.interpretar(json.dumps({"is_error": False, "result": json.dumps(BUENA)})) == BUENA
    )


def test_si_falla_se_apunta_el_error_pero_no_pisa_una_buena(con, settings):
    def falla(*_):
        raise LecturaError("Claude no respondió")

    with pytest.raises(LecturaError):
        lectura.generar(con, settings, date(2026, 1, 10), falla)
    assert hay(db.lectura(con, "2026-01-10"))["error"] == "Claude no respondió"

    lectura.generar(con, settings, date(2026, 1, 10), lambda *_: _salida())
    with pytest.raises(LecturaError):
        lectura.generar(con, settings, date(2026, 1, 10), falla)
    assert hay(db.lectura(con, "2026-01-10"))["estado"] == "ok"


def test_ejecutar_avisa_si_no_hay_claude(tmp_path):
    with pytest.raises(LecturaError, match="No se pudo lanzar"):
        lectura.ejecutar(["/no/existe/claude", "-p"], "hola", tmp_path)
