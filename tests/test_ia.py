import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from salud.config import Settings
from salud.ia import (
    MAX_BYTES_IMAGEN,
    MOTORES,
    TIEMPO_COMIDA_S,
    TIEMPO_MAXIMO_S,
    Adjunto,
    AlimentoIA,
    CupoAgotado,
    ErrorIA,
    Etiqueta,
    IAOcupada,
    Tarea,
    analizar_comida,
    ejecutar,
    etiqueta_desde_respuesta,
    interpretar_agy,
    interpretar_claude,
    leer_etiqueta,
    motor_agy,
    motor_claude,
    tipo_imagen,
)


def _datos_etiqueta(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "legible": True,
        "nombre": "Leche entera",
        "marca": "Central Lechera",
        "unidad": "ml",
        "kcal_100": None,
        "kj_100": None,
        "proteinas_100": None,
        "hidratos_100": None,
        "grasas_100": None,
        "racion_cantidad": None,
        "racion_kcal": None,
        "racion_kj": None,
        "racion_proteinas": None,
        "racion_hidratos": None,
        "racion_grasas": None,
        "aviso": None,
    }
    base.update(kwargs)
    return base


# 1. tipo_imagen: jpeg, png, webp y b"hola" -> None
def test_tipo_imagen():
    assert tipo_imagen(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"
    assert tipo_imagen(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") == "image/png"
    assert tipo_imagen(b"RIFF\x14\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert tipo_imagen(b"hola") is None
    assert tipo_imagen(b"") is None


# 2. etiqueta_desde_respuesta
def test_etiqueta_valores_completos_y_coherentes():
    # 2a: valores por 100 completos y coherentes (kcal 63, p 3.1, h 4.6, g 3.6, unidad "ml")
    datos = _datos_etiqueta(
        kcal_100=63,
        proteinas_100=3.1,
        hidratos_100=4.6,
        grasas_100=3.6,
        unidad="ml",
    )
    e = etiqueta_desde_respuesta(datos)
    assert e.kcal == 63
    assert e.proteinas == 3.1
    assert e.hidratos == 4.6
    assert e.grasas == 3.6
    assert e.unidad == "ml"
    assert e.cuadra is True
    assert e.por_racion is False
    assert e.avisos == ()


def test_etiqueta_no_legible_error():
    # 2b: legible False -> ErrorIA
    with pytest.raises(ErrorIA, match="No se ve una tabla nutricional en la foto"):
        etiqueta_desde_respuesta(_datos_etiqueta(legible=False))


def test_etiqueta_solo_kj():
    # 2c: solo kj_100 264 -> kcal 63.1
    datos = _datos_etiqueta(kj_100=264)
    e = etiqueta_desde_respuesta(datos)
    assert e.kcal == 63.1


def test_etiqueta_solo_racion():
    # 2d: solo ración (cantidad 30, kcal 120, p 3, h 20, g 3)
    # -> por 100: 400, 10, 66.7, 10 y por_racion
    datos = _datos_etiqueta(
        racion_cantidad=30,
        racion_kcal=120,
        racion_proteinas=3,
        racion_hidratos=20,
        racion_grasas=3,
    )
    e = etiqueta_desde_respuesta(datos)
    assert e.kcal == 400.0
    assert e.proteinas == 10.0
    assert e.hidratos == 66.7
    assert e.grasas == 10.0
    assert e.por_racion is True
    assert "Calculado a partir de los valores por ración" in e.avisos


def test_etiqueta_valor_fuera_de_rango():
    # 2e: un valor fuera de rango (grasas 250) -> grasas None y el aviso de rango
    datos = _datos_etiqueta(
        kcal_100=100,
        proteinas_100=5,
        hidratos_100=10,
        grasas_100=250,
    )
    e = etiqueta_desde_respuesta(datos)
    assert e.grasas is None
    assert "Se ha descartado un valor fuera de rango" in e.avisos


def test_etiqueta_todos_null_error():
    # 2f: todos null -> ErrorIA
    datos = _datos_etiqueta()
    with pytest.raises(ErrorIA, match="No se pudieron leer los valores de la etiqueta"):
        etiqueta_desde_respuesta(datos)


def test_etiqueta_no_cuadra():
    # 2g: no cuadra (kcal 500, p 1, h 1, g 1) -> cuadra False y su aviso
    datos = _datos_etiqueta(
        kcal_100=500,
        proteinas_100=1,
        hidratos_100=1,
        grasas_100=1,
    )
    e = etiqueta_desde_respuesta(datos)
    assert e.cuadra is False
    assert "Las calorías no cuadran con los macronutrientes: revísalas" in e.avisos


def test_etiqueta_bool_y_str_en_campo_numerico():
    # 2h: bool y "3,1" en un campo numérico -> None
    datos = _datos_etiqueta(
        kcal_100=True,
        proteinas_100="3,1",
        hidratos_100=10,
    )
    e = etiqueta_desde_respuesta(datos)
    assert e.kcal is None
    assert e.proteinas is None
    assert e.hidratos == 10


def test_etiqueta_aviso_del_modelo_largo():
    # 2i: aviso del modelo largo (300 caracteres) -> recortado a 200 y primero en avisos
    aviso_300 = "A" * 300
    datos = _datos_etiqueta(
        kcal_100=500,
        proteinas_100=1,
        hidratos_100=1,
        grasas_100=1,
        aviso=aviso_300,
    )
    e = etiqueta_desde_respuesta(datos)
    assert len(e.avisos) >= 2
    assert e.avisos[0] == "A" * 200
    assert "Las calorías no cuadran con los macronutrientes: revísalas" in e.avisos[1:]


# 3. interpretar_agy
def test_interpretar_agy_exito():
    salida = json.dumps({"status": "SUCCESS", "structured_output": {"kcal_100": 100}})
    assert interpretar_agy(salida) == {"kcal_100": 100}


def test_interpretar_agy_exito_con_denied_actions(caplog):
    salida = json.dumps(
        {
            "status": "SUCCESS",
            "structured_output": {"kcal_100": 100},
            "denied_actions": [{"action": "bash"}],
        }
    )
    res = interpretar_agy(salida)
    assert res == {"kcal_100": 100}
    assert "agy intentó acciones denegadas: ['bash']" in caplog.text


def test_interpretar_agy_status_error():
    salida = json.dumps({"status": "ERROR", "error": "Algo falló en agy"})
    with pytest.raises(ErrorIA, match="agy dio un error: Algo falló en agy"):
        interpretar_agy(salida)


def test_interpretar_agy_quota():
    salida = json.dumps({"status": "ERROR", "error": "Individual quota reached for Gemini"})
    with pytest.raises(
        CupoAgotado, match="Se ha agotado el cupo de la IA en agy; prueba más tarde"
    ):
        interpretar_agy(salida)


def test_interpretar_agy_no_json():
    with pytest.raises(ErrorIA, match="agy no devolvió una respuesta válida"):
        interpretar_agy("esto no es json")


def test_interpretar_agy_sin_structured_output():
    salida = json.dumps({"status": "SUCCESS", "structured_output": "no-dict"})
    with pytest.raises(ErrorIA, match="agy no devolvió los datos pedidos"):
        interpretar_agy(salida)


def test_interpretar_agy_salida_vacia_con_stderr():
    with pytest.raises(ErrorIA, match="Fallo crítico en stderr"):
        interpretar_agy("", stderr="Fallo crítico en stderr")


# 4. interpretar_claude
def test_interpretar_claude_exito_varias_lineas():
    salida = "\n".join(
        [
            json.dumps({"type": "progress", "text": "pensando..."}),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "structured_output": {"proteinas_100": 15.0},
                }
            ),
        ]
    )
    assert interpretar_claude(salida) == {"proteinas_100": 15.0}


def test_interpretar_claude_is_error():
    salida = json.dumps({"type": "result", "is_error": True, "result": "Error en el modelo"})
    with pytest.raises(ErrorIA, match="Claude Code dio un error: Error en el modelo"):
        interpretar_claude(salida)


def test_interpretar_claude_sin_linea_result():
    salida = "\n".join(
        [
            json.dumps({"type": "progress", "text": "iniciando"}),
            json.dumps({"type": "progress", "text": "trabajando"}),
        ]
    )
    with pytest.raises(ErrorIA, match="Claude Code no devolvió una respuesta válida"):
        interpretar_claude(salida)


def test_interpretar_claude_sin_structured_output():
    salida = json.dumps({"type": "result", "is_error": False, "structured_output": None})
    with pytest.raises(ErrorIA, match="Claude Code no devolvió los datos pedidos"):
        interpretar_claude(salida)


# 5. motor_agy con subprocess.run falso
def test_motor_agy_subprocess(tmp_path, monkeypatch):
    token_falso = tmp_path / "fake-agy-token"
    token_falso.write_text("token-secret")
    settings = Settings(
        db_path=tmp_path / "salud.db",
        agy="agy",
        agy_token=token_falso,
        ia_modelo_agy="gemini-3.8-flash-medium",
    )
    datos_adjunto = b"\xff\xd8\xff\xe0imagen-bytes"
    tarea = Tarea(
        nombre="etiqueta",
        instrucciones="Lee la etiqueta de la foto",
        esquema={"type": "object"},
        adjuntos=(Adjunto("image/jpeg", datos_adjunto),),
    )

    carpetas_temporales: list[Path] = []

    def fake_run(orden, cwd, env, capture_output, text, timeout, stdin):
        assert set(env.keys()) == {"HOME", "PATH", "LANG"}
        assert env["LANG"] == "C.UTF-8"
        assert capture_output is True
        assert text is True
        assert timeout > 0
        assert stdin == subprocess.DEVNULL

        cwd_path = Path(cwd)
        assert cwd_path.name == "trabajo"
        assert str(cwd_path).startswith("/tmp")
        carpetas_temporales.append(cwd_path.parent)

        adjunto_fichero = cwd_path / "adjunto-1.jpg"
        assert adjunto_fichero.is_file()
        assert adjunto_fichero.read_bytes() == datos_adjunto

        home = Path(env["HOME"])
        gemini_dir = home / ".gemini" / "antigravity-cli"
        settings_json = gemini_dir / "settings.json"
        assert settings_json.is_file()
        assert json.loads(settings_json.read_text()) == {
            "permissions": {"allow": [], "deny": ["command(regex:.*)"]}
        }

        token_symlink = gemini_dir / "antigravity-oauth-token"
        assert token_symlink.is_symlink()
        assert token_symlink.resolve() == token_falso.resolve()

        prompt_idx = orden.index("-p")
        prompt = orden[prompt_idx + 1]
        assert "@adjunto-1.jpg" in prompt

        assert "--json-schema" in orden
        model_idx = orden.index("--model")
        assert orden[model_idx + 1] == "gemini-3.8-flash-medium"

        salida = {
            "status": "SUCCESS",
            "structured_output": {"kcal_100": 75},
        }
        return subprocess.CompletedProcess(orden, 0, stdout=json.dumps(salida), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resultado = motor_agy(tarea, settings)
    assert resultado == {"kcal_100": 75}

    assert len(carpetas_temporales) == 1
    assert not carpetas_temporales[0].exists()

    # TimeoutExpired -> ErrorIA
    def fake_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["agy"], timeout=90)

    monkeypatch.setattr(subprocess, "run", fake_timeout)
    with pytest.raises(ErrorIA, match="La IA tardó más de 90 s"):
        motor_agy(tarea, settings)

    # Token inexistente -> ErrorIA sin lanzar subprocess.run
    llamado = False

    def fake_no_llamar(*_args, **_kwargs):
        nonlocal llamado
        llamado = True
        return subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_no_llamar)
    settings_sin_token = Settings(
        db_path=tmp_path / "salud.db",
        agy="agy",
        agy_token=tmp_path / "no-existe",
    )
    with pytest.raises(ErrorIA, match="agy no tiene sesión iniciada en calamar"):
        motor_agy(tarea, settings_sin_token)
    assert not llamado


# 6. motor_claude con subprocess.run falso
def test_motor_claude_subprocess(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "salud.db",
        claude="claude",
        ia_modelo_claude="haiku",
    )
    datos_adjunto = b"\xff\xd8\xff\xe0imagen-claude"
    tarea = Tarea(
        nombre="etiqueta",
        instrucciones="Lee la etiqueta con claude",
        esquema={"type": "object"},
        adjuntos=(Adjunto("image/jpeg", datos_adjunto),),
    )

    def fake_run(orden, input, cwd, capture_output, text, timeout):
        assert capture_output is True
        assert text is True
        assert timeout > 0
        assert Path(cwd).is_dir()

        lineas = input.strip().splitlines()
        assert len(lineas) == 1
        mensaje = json.loads(lineas[0])
        assert mensaje["type"] == "user"
        bloques = mensaje["message"]["content"]
        assert len(bloques) == 2

        bloque_img = bloques[0]
        assert bloque_img["type"] == "image"
        assert bloque_img["source"]["type"] == "base64"
        assert bloque_img["source"]["media_type"] == "image/jpeg"
        assert base64.b64decode(bloque_img["source"]["data"]) == datos_adjunto

        bloque_txt = bloques[1]
        assert bloque_txt == {"type": "text", "text": "Lee la etiqueta con claude"}

        tools_idx = orden.index("--tools")
        assert orden[tools_idx + 1] == ""

        model_idx = orden.index("--model")
        assert orden[model_idx + 1] == "haiku"

        salida_lineas = [
            json.dumps({"type": "progress", "text": "pensando"}),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "structured_output": {"grasas_100": 8.0},
                }
            ),
        ]
        return subprocess.CompletedProcess(orden, 0, stdout="\n".join(salida_lineas), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resultado = motor_claude(tarea, settings)
    assert resultado == {"grasas_100": 8.0}


# 7. ejecutar: concurrencia y motor por defecto
def test_ejecutar_concurrencia_y_motor_por_defecto(tmp_path, monkeypatch):
    settings_agy = Settings(db_path=tmp_path / "salud.db", ia_motor="agy")
    settings_claude = Settings(db_path=tmp_path / "salud.db", ia_motor="claude")
    tarea = Tarea(nombre="test", instrucciones="instrucciones", esquema={})

    # Concurrencia: una segunda llamada lanza IAOcupada
    def motor_falso(t, s):
        with pytest.raises(IAOcupada, match="Ya hay otra petición a la IA en marcha"):
            ejecutar(t, s, motor=motor_falso)
        return {"resultado": "primera"}

    res = ejecutar(tarea, settings_agy, motor=motor_falso)
    assert res == {"resultado": "primera"}

    # Tras terminar, el lock está libre
    res2 = ejecutar(tarea, settings_agy, motor=lambda _t, _s: {"resultado": "segunda"})
    assert res2 == {"resultado": "segunda"}

    # Motor por defecto sale de settings.ia_motor
    llamadas = []
    monkeypatch.setitem(MOTORES, "agy", lambda _t, _s: llamadas.append("agy") or {"motor": "agy"})
    monkeypatch.setitem(
        MOTORES, "claude", lambda _t, _s: llamadas.append("claude") or {"motor": "claude"}
    )

    res_agy = ejecutar(tarea, settings_agy)
    assert res_agy == {"motor": "agy"}
    assert llamadas == ["agy"]

    res_claude = ejecutar(tarea, settings_claude)
    assert res_claude == {"motor": "claude"}
    assert llamadas == ["agy", "claude"]


# 8. leer_etiqueta con motor falso
def test_leer_etiqueta_con_motor_falso(tmp_path):
    settings = Settings(db_path=tmp_path / "salud.db")
    llamadas_motor = []

    def motor_falso(tarea, _settings):
        llamadas_motor.append(tarea)
        return {
            "legible": True,
            "nombre": "Yogur Natural",
            "marca": "Danone",
            "unidad": "g",
            "kcal_100": 60.0,
            "kj_100": None,
            "proteinas_100": 3.5,
            "hidratos_100": 4.5,
            "grasas_100": 3.0,
            "racion_cantidad": None,
            "racion_kcal": None,
            "racion_kj": None,
            "racion_proteinas": None,
            "racion_hidratos": None,
            "racion_grasas": None,
            "aviso": None,
        }

    # Imagen no válida: ErrorIA sin llamar al motor
    with pytest.raises(ErrorIA, match="El fichero no es una imagen JPEG, PNG o WebP"):
        leer_etiqueta(settings, b"no-es-imagen", motor=motor_falso)
    assert len(llamadas_motor) == 0

    # Imagen demasiado grande
    with pytest.raises(ErrorIA, match="La imagen es demasiado grande"):
        leer_etiqueta(settings, b"\xff\xd8\xff" + b"0" * (MAX_BYTES_IMAGEN + 1), motor=motor_falso)
    assert len(llamadas_motor) == 0

    # Imagen JPEG pequeña
    img_valida = b"\xff\xd8\xff" + b"0" * 10
    etiqueta = leer_etiqueta(settings, img_valida, motor=motor_falso)
    assert isinstance(etiqueta, Etiqueta)
    assert etiqueta.nombre == "Yogur Natural"
    assert etiqueta.marca == "Danone"
    assert etiqueta.kcal == 60.0
    assert len(llamadas_motor) == 1
    tarea_recibida = llamadas_motor[0]
    assert tarea_recibida.nombre == "etiqueta"
    assert len(tarea_recibida.adjuntos) == 1
    assert tarea_recibida.adjuntos[0].tipo == "image/jpeg"
    assert tarea_recibida.adjuntos[0].datos == img_valida


def test_motor_agy_modelo_override(tmp_path, monkeypatch):
    token_falso = tmp_path / "fake-agy-token"
    token_falso.write_text("token-secret")
    settings = Settings(
        db_path=tmp_path / "salud.db",
        agy="agy",
        agy_token=token_falso,
        ia_modelo_agy="gemini-3.8-flash-low",
    )
    tarea = Tarea(
        nombre="consejo",
        instrucciones="Qué como",
        esquema={"type": "object"},
        modelo_agy="gemini-3.8-flash-high",
    )

    def fake_run(orden, **_kwargs):
        model_idx = orden.index("--model")
        assert orden[model_idx + 1] == "gemini-3.8-flash-high"
        salida = {"status": "SUCCESS", "structured_output": {"ok": True}}
        return subprocess.CompletedProcess(orden, 0, stdout=json.dumps(salida), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resultado = motor_agy(tarea, settings)
    assert resultado == {"ok": True}


def test_motor_claude_modelo_override(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "salud.db",
        claude="claude",
        ia_modelo_claude="haiku",
    )
    tarea = Tarea(
        nombre="consejo",
        instrucciones="Qué como",
        esquema={"type": "object"},
        modelo_claude="sonnet",
    )

    def fake_run(orden, **_kwargs):
        model_idx = orden.index("--model")
        assert orden[model_idx + 1] == "sonnet"
        salida = json.dumps(
            {"type": "result", "is_error": False, "structured_output": {"ok": True}}
        )
        return subprocess.CompletedProcess(orden, 0, stdout=salida, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resultado = motor_claude(tarea, settings)
    assert resultado == {"ok": True}


def test_motor_agy_reintenta_una_vez_sin_datos(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("x")
    settings = Settings(db_path=tmp_path / "s.db", agy="agy", agy_token=token)
    tarea = Tarea(nombre="t", instrucciones="hola", esquema={"type": "object"})
    respuestas = [
        json.dumps({"status": "SUCCESS", "response": ""}),
        json.dumps({"status": "SUCCESS", "structured_output": {"ok": 1}}),
    ]
    prompts: list[str] = []

    def falso(orden, **_kw):
        prompts.append(orden[orden.index("-p") + 1])
        return subprocess.CompletedProcess(orden, 0, stdout=respuestas.pop(0), stderr="")

    monkeypatch.setattr(subprocess, "run", falso)
    assert motor_agy(tarea, settings) == {"ok": 1}
    assert len(prompts) == 2
    assert prompts[0].startswith("Responde directamente")


def test_motor_agy_sin_datos_dos_veces_falla(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("x")
    settings = Settings(db_path=tmp_path / "s.db", agy="agy", agy_token=token)
    tarea = Tarea(nombre="t", instrucciones="hola", esquema={"type": "object"})
    vacia = json.dumps({"status": "SUCCESS", "response": ""})
    llamadas: list[int] = []

    def falso(orden, **_kw):
        llamadas.append(1)
        return subprocess.CompletedProcess(orden, 0, stdout=vacia, stderr="")

    monkeypatch.setattr(subprocess, "run", falso)
    with pytest.raises(ErrorIA, match="no devolvió los datos"):
        motor_agy(tarea, settings)
    assert len(llamadas) == 2


def test_el_tiempo_maximo_es_el_de_la_tarea(monkeypatch, tmp_path):
    """El consejo espera más que una etiqueta: agy y el subprocess usan el de la tarea."""
    token = tmp_path / "token"
    token.write_text("x")
    settings = cast(Settings, SimpleNamespace(agy="agy", agy_token=token, ia_modelo_agy="m"))
    llamadas = []

    def falso_run(orden, **kwargs):
        llamadas.append((orden[orden.index("--print-timeout") + 1], kwargs["timeout"]))
        salida = {"status": "SUCCESS", "structured_output": {"ok": True}}
        return SimpleNamespace(stdout=json.dumps(salida), stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", falso_run)
    tarea = Tarea("consejo", "hola", {"type": "object"}, tiempo_maximo_s=300)
    assert motor_agy(tarea, settings) == {"ok": True}
    assert motor_agy(Tarea("etiqueta", "hola", {"type": "object"}), settings) == {"ok": True}
    assert llamadas == [("300s", 315), (f"{TIEMPO_MAXIMO_S}s", TIEMPO_MAXIMO_S + 15)]


def test_analizar_comida_exito(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")

    def motor_falso(_tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        return {
            "alimentos": [
                {
                    "nombre": "Sopa de fideos",
                    "cantidad": 150.0,
                    "unidad": "g",
                    "kcal": 95.0,
                    "proteinas": 3.5,
                    "hidratos": 14.0,
                    "grasas": 2.5,
                    "momento": "comida",
                    "aviso": "Valores estimados",
                }
            ]
        }

    resultado = analizar_comida(settings, "150 g de sopa de fideos", None, motor=motor_falso)
    assert len(resultado) == 1
    al = resultado[0]
    assert isinstance(al, AlimentoIA)
    assert al.nombre == "Sopa de fideos"
    assert al.cantidad == 150.0
    assert al.unidad == "g"
    assert al.kcal == 95.0
    assert al.proteinas == 3.5
    assert al.hidratos == 14.0
    assert al.grasas == 2.5
    assert al.momento == "comida"
    assert al.aviso == "Valores estimados"


def test_analizar_comida_descarte_invalidos(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")

    def motor_falso(_tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        return {
            "alimentos": [
                {
                    "nombre": "Válido",
                    "cantidad": 100.0,
                    "unidad": "g",
                    "kcal": 100.0,
                    "proteinas": 5.0,
                    "hidratos": 10.0,
                    "grasas": 2.0,
                    "momento": None,
                    "aviso": None,
                },
                {
                    "nombre": "   ",
                    "cantidad": 50.0,
                    "unidad": "g",
                    "kcal": 50.0,
                    "proteinas": 1.0,
                    "hidratos": 5.0,
                    "grasas": 1.0,
                    "momento": None,
                    "aviso": None,
                },
                {
                    "nombre": "Cantidad cero",
                    "cantidad": 0.0,
                    "unidad": "g",
                    "kcal": 50.0,
                    "proteinas": 1.0,
                    "hidratos": 5.0,
                    "grasas": 1.0,
                    "momento": None,
                    "aviso": None,
                },
                {
                    "nombre": "Cantidad gigante",
                    "cantidad": 9000.0,
                    "unidad": "g",
                    "kcal": 50.0,
                    "proteinas": 1.0,
                    "hidratos": 5.0,
                    "grasas": 1.0,
                    "momento": None,
                    "aviso": None,
                },
                {
                    "nombre": "Unidad incorrecta",
                    "cantidad": 1.0,
                    "unidad": "kg",
                    "kcal": 50.0,
                    "proteinas": 1.0,
                    "hidratos": 5.0,
                    "grasas": 1.0,
                    "momento": None,
                    "aviso": None,
                },
            ]
        }

    resultado = analizar_comida(settings, "plato variado", None, motor=motor_falso)
    assert len(resultado) == 1
    assert resultado[0].nombre == "Válido"


def test_analizar_comida_rangos_kcal_macros_y_momento(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")

    def motor_falso(_tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        return {
            "alimentos": [
                {
                    "nombre": "Extremo",
                    "cantidad": 100.0,
                    "unidad": "g",
                    "kcal": 6000.0,  # fuera de rango (>5000) -> None
                    "proteinas": 1500.0,  # fuera de rango (>1000) -> None
                    "hidratos": 50.0,
                    "grasas": -5.0,  # negativo -> None
                    "momento": "medianoche",  # no válido -> None
                    "aviso": None,
                }
            ]
        }

    resultado = analizar_comida(settings, "comida rara", None, motor=motor_falso)
    assert len(resultado) == 1
    al = resultado[0]
    assert al.kcal is None
    assert al.proteinas is None
    assert al.hidratos == 50.0
    assert al.grasas is None
    assert al.momento is None


def test_analizar_comida_lista_vacia_error(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")

    def motor_vacio(_tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        return {"alimentos": []}

    with pytest.raises(ErrorIA, match="No he reconocido nada que se coma o se beba"):
        analizar_comida(settings, "un ratón de ordenador", None, motor=motor_vacio)


def test_analizar_comida_validaciones_entrada(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")

    def motor_no_llamar(_tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        pytest.fail("No debería llamarse al motor")

    # Sin texto ni imagen
    with pytest.raises(ErrorIA, match="Escribe qué has comido o añade una foto"):
        analizar_comida(settings, "", None, motor=motor_no_llamar)

    with pytest.raises(ErrorIA, match="Escribe qué has comido o añade una foto"):
        analizar_comida(settings, " ", None, motor=motor_no_llamar)

    with pytest.raises(ErrorIA, match="Escribe qué has comido o añade una foto"):
        analizar_comida(settings, "x", None, motor=motor_no_llamar)

    # Texto > 500 caracteres
    with pytest.raises(ErrorIA, match="El texto es demasiado largo"):
        analizar_comida(settings, "a" * 501, None, motor=motor_no_llamar)

    # Imagen corrupta
    with pytest.raises(ErrorIA, match="El fichero no es una imagen JPEG, PNG o WebP"):
        analizar_comida(settings, "plato", b"esto no es una imagen", motor=motor_no_llamar)

    # Imagen demasiado grande
    with pytest.raises(ErrorIA, match="La imagen es demasiado grande"):
        analizar_comida(
            settings,
            "plato",
            b"\xff\xd8\xff" + b"0" * (MAX_BYTES_IMAGEN + 1),
            motor=motor_no_llamar,
        )


def test_analizar_comida_tarea_atributos_y_foto(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")
    tarea_capturada: list[Tarea] = []

    def motor_capturador(tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        tarea_capturada.append(tarea)
        return {
            "alimentos": [
                {
                    "nombre": "Foto alimento",
                    "cantidad": 100.0,
                    "unidad": "g",
                    "kcal": 100.0,
                    "proteinas": 5.0,
                    "hidratos": 10.0,
                    "grasas": 2.0,
                    "momento": None,
                    "aviso": None,
                }
            ]
        }

    foto_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    analizar_comida(settings, "", foto_bytes, motor=motor_capturador)
    assert len(tarea_capturada) == 1
    t = tarea_capturada[0]
    assert t.nombre == "comida"
    assert t.modelo_agy == settings.ia_modelo_comida
    assert t.tiempo_maximo_s == TIEMPO_COMIDA_S
    assert len(t.adjuntos) == 1
    assert t.adjuntos[0].tipo == "image/jpeg"
    assert t.adjuntos[0].datos == foto_bytes
    assert "(no ha escrito nada: mira la foto)" in t.instrucciones


def test_analizar_comida_maximo_20_alimentos(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db")

    def motor_muchos(_tarea: Tarea, _settings: Settings) -> dict[str, Any]:
        return {
            "alimentos": [
                {
                    "nombre": f"Alimento {i}",
                    "cantidad": 10.0,
                    "unidad": "g",
                    "kcal": 10.0,
                    "proteinas": 1.0,
                    "hidratos": 1.0,
                    "grasas": 1.0,
                    "momento": None,
                    "aviso": None,
                }
                for i in range(25)
            ]
        }

    resultado = analizar_comida(settings, "buffet libre", None, motor=motor_muchos)
    assert len(resultado) == 20
