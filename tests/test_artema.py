"""Tests del catálogo de platos preparados de Artema."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from salud import artema, cli, db
from salud.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "artema"


_RealClient = httpx.Client


class ArtemaMock:
    """MockTransport que sirve products.json y las fichas HTML de fixtures."""

    def __init__(
        self,
        products: dict[str, Any] | None = None,
        failing: set[str] | None = None,
    ) -> None:
        if products is not None:
            self.products = products
        else:
            self.products = json.loads((FIXTURES / "products.json").read_text(encoding="utf-8"))
        self.failing = failing or set()
        self.peticiones: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        url_str = str(request.url)
        if "/products.json" in url_str:
            page = int(request.url.params.get("page", 1))
            if page == 1:
                return httpx.Response(200, json=self.products)
            return httpx.Response(200, json={"products": []})
        handle = request.url.path.rsplit("/", 1)[-1]
        if handle in self.failing:
            return httpx.Response(500, text="Internal Server Error")
        fichero = FIXTURES / f"{handle}.html"
        if not fichero.exists():
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=fichero.read_text(encoding="utf-8"))

    @property
    def client(self) -> httpx.Client:
        return _RealClient(transport=httpx.MockTransport(self.handler))


# ── Parseo de etiquetas y fichas ──────────────────────────────────────────────


def test_parse_label_decimal_comma():
    label = (
        "Energía 890,90kJ, 212,93kcal, Grasas 9,46g, Grasas saturadas 1,38g, "
        "Hidratos de carbono 9,89g, Azúcares 1,69g, Proteínas 22,80g, Sal 0,68g"
    )
    val = artema.parse_label(label)
    assert val == {
        "kcal": 212.93,
        "grasas": 9.46,
        "saturadas": 1.38,
        "hidratos": 9.89,
        "azucares": 1.69,
        "fibra": None,
        "proteinas": 22.8,
        "sal": 0.68,
    }


def test_parse_label_sin_proteina_devuelve_none():
    assert artema.parse_label("Energía 618kJ, 148kcal, Grasas 2g") is None


@pytest.mark.parametrize(
    ("handle", "peso", "kcal", "proteina", "sal"),
    [
        ("merluza-a-la-gallega", 280.0, 212.93, 22.8, 0.68),
        ("macarrones-bolonesa", 300.0, 298.0, 6.0, 2.0),
        ("albongas-salsa", 300.0, 444.0, 26.1, 3.09),
    ],
)
def test_parse_product_page_fixtures(
    handle: str, peso: float, kcal: float, proteina: float, sal: float
):
    html = (FIXTURES / f"{handle}.html").read_text(encoding="utf-8")
    page = artema.parse_product_page(html)
    assert page["peso_g"] == peso
    assert page["per_serving"]["kcal"] == kcal
    assert page["per_serving"]["proteinas"] == proteina
    assert page["per_serving"]["sal"] == sal
    assert page["per_100g"] is not None
    assert page["ingredientes"]


def test_parse_product_page_sin_etiqueta():
    page = artema.parse_product_page("<html><body><h1>Tarjeta regalo</h1></body></html>")
    assert page == {"peso_g": None, "per_serving": None, "per_100g": None, "ingredientes": None}


# ── Alta inicial y refresco ───────────────────────────────────────────────────


def test_actualizar_alta_inicial(con):
    mock = ArtemaMock()
    res = artema.actualizar(con, mock.client, pausa_s=0)
    assert res.platos == 3
    assert res.nuevos == 3
    assert res.errores == []

    platos = db.platos_artema(con, solo_disponibles=True)
    assert len(platos) == 3
    handles = {p["handle"] for p in platos}
    assert handles == {"merluza-a-la-gallega", "macarrones-bolonesa", "albongas-salsa"}

    merluza = db.plato_artema(con, "merluza-a-la-gallega")
    assert merluza is not None
    assert merluza["variant_id"] == 50534896566618
    assert merluza["nombre"] == "Merluza a la gallega"
    assert merluza["kcal"] == 212.93
    assert merluza["proteinas"] == 22.8
    assert merluza["peso_g"] == 280.0
    assert merluza["disponible"] == 1


def test_actualizar_refresco_parcial_no_relee_fichas(con):
    mock1 = ArtemaMock()
    artema.actualizar(con, mock1.client, pausa_s=0)

    mock2 = ArtemaMock()
    res2 = artema.actualizar(con, mock2.client, completo=False, pausa_s=0)
    assert res2.platos == 3
    assert res2.nuevos == 0
    assert res2.errores == []

    # Solo debe haber consultado /products.json, ninguna ficha
    fichas_consultadas = [p for p in mock2.peticiones if "/products.json" not in str(p.url)]
    assert fichas_consultadas == []


def test_actualizar_plato_retirado_queda_no_disponible(con):
    mock = ArtemaMock()
    artema.actualizar(con, mock.client, pausa_s=0)

    # Insertamos un plato que ya no viene en la tienda
    db.guardar_platos_artema(
        con,
        [
            artema.Plato(
                handle="pastel-carne",
                variant_id=99999,
                nombre="Pastel de carne",
                url="https://artema.club/products/pastel-carne",
                categoria="Principal",
                precio=6.50,
                en_oferta=False,
                precio_original=None,
                peso_g=350.0,
                kcal=500.0,
                proteinas=30.0,
                hidratos=25.0,
                grasas=20.0,
                azucares=2.0,
                saturadas=5.0,
                sal=1.5,
                ingredientes="Carne picada, patata",
                disponible=True,
                actualizado="2026-09-01T10:00:00",
            )
        ],
    )
    assert len(db.platos_artema(con, solo_disponibles=True)) == 4

    # Sincronizamos de nuevo: pastel-carne debe quedar disponible=0
    artema.actualizar(con, mock.client, pausa_s=0)
    disponibles = db.platos_artema(con, solo_disponibles=True)
    assert len(disponibles) == 3

    todos = db.platos_artema(con, solo_disponibles=False)
    assert len(todos) == 4
    pastel = db.plato_artema(con, "pastel-carne")
    assert pastel is not None
    assert pastel["disponible"] == 0
    assert pastel["kcal"] == 500.0


def test_actualizar_fallo_ficha_conserva_datos_o_macros_nulos(con):
    # Ficha que falla en plato nuevo
    mock_fallo_nuevo = ArtemaMock(failing={"merluza-a-la-gallega"})
    res = artema.actualizar(con, mock_fallo_nuevo.client, pausa_s=0)
    assert len(res.errores) == 1
    merluza = db.plato_artema(con, "merluza-a-la-gallega")
    assert merluza is not None
    assert merluza["kcal"] is None

    # Ahora guardamos merluza con macros y hacemos refresco completo que falla
    db.guardar_platos_artema(
        con,
        [
            artema.Plato(
                handle="merluza-a-la-gallega",
                variant_id=50534896566618,
                nombre="Merluza a la gallega",
                url="https://artema.club/products/merluza-a-la-gallega",
                categoria="Principal",
                precio=5.99,
                en_oferta=False,
                precio_original=None,
                peso_g=280.0,
                kcal=212.93,
                proteinas=22.8,
                hidratos=9.89,
                grasas=9.46,
                azucares=1.69,
                saturadas=1.38,
                sal=0.68,
                ingredientes="Merluza",
                disponible=True,
                actualizado="2026-09-01T10:00:00",
            )
        ],
    )
    res2 = artema.actualizar(con, mock_fallo_nuevo.client, completo=True, pausa_s=0)
    assert len(res2.errores) == 1
    merluza2 = db.plato_artema(con, "merluza-a-la-gallega")
    assert merluza2 is not None
    assert merluza2["kcal"] == 212.93


def test_actualizar_tienda_vacia_error_sin_tocar_tabla(con):
    mock = ArtemaMock()
    artema.actualizar(con, mock.client, pausa_s=0)
    assert len(db.platos_artema(con)) == 3

    mock_vacio = ArtemaMock(products={"products": []})
    with pytest.raises(RuntimeError, match="artema.club no ha devuelto ningún producto"):
        artema.actualizar(con, mock_vacio.client, pausa_s=0)

    # La tabla sigue intacta con los 3 platos
    assert len(db.platos_artema(con, solo_disponibles=True)) == 3


def test_cmd_artema_cli(monkeypatch, tmp_path, capsys):
    settings = Settings(db_path=tmp_path / "salud.db")
    mock = ArtemaMock()
    monkeypatch.setattr(httpx, "Client", lambda **_kw: mock.client)
    monkeypatch.setattr(artema, "REQUEST_DELAY_S", 0)

    args = argparse.Namespace(completo=False)
    codigo = cli.cmd_artema(settings, args)
    assert codigo == 0
    salida = capsys.readouterr().out
    assert "Artema: 3 platos (3 nuevos)" in salida


def test_cmd_sync_artema_diario(monkeypatch, tmp_path, caplog):
    settings = Settings(db_path=tmp_path / "salud.db")
    mock = ArtemaMock()
    monkeypatch.setattr(httpx, "Client", lambda **_kw: mock.client)
    monkeypatch.setattr(cli, "_sincronizar", lambda _s, _d, _h: 0)
    monkeypatch.setattr(cli.ia, "modelos_agy", lambda _s: [])
    monkeypatch.setattr(artema, "REQUEST_DELAY_S", 0)

    args = argparse.Namespace(desde=None, dias=1)
    with caplog.at_level(logging.INFO):
        codigo = cli.cmd_sync(settings, args)

    assert codigo == 0
    assert "Catálogo de Artema actualizado: 3 platos (3 nuevos)" in caplog.text

    con = db.conectar(settings.db_path)
    assert db.leer_estado_ia(con, "artema_actualizado") == cli._hoy().isoformat()
    assert len(db.platos_artema(con)) == 3

    # Segunda ejecución el mismo día no vuelve a llamar
    mock.peticiones.clear()
    codigo2 = cli.cmd_sync(settings, args)
    assert codigo2 == 0
    assert mock.peticiones == []
    con.close()


# ── Recomendación de platos ───────────────────────────────────────────────────


def plato_test(
    handle: str,
    nombre: str,
    categoria: str = "Principal",
    kcal: float | None = 400.0,
    proteinas: float | None = 25.0,
    sal: float | None = 1.0,
    disponible: bool = True,
    ingredientes: str | None = None,
    precio: float = 5.99,
) -> artema.Plato:
    return artema.Plato(
        handle=handle,
        variant_id=1000,
        nombre=nombre,
        url=f"https://artema.club/products/{handle}",
        categoria=categoria,
        precio=precio,
        en_oferta=False,
        precio_original=None,
        peso_g=300.0,
        kcal=kcal,
        proteinas=proteinas,
        hidratos=20.0,
        grasas=10.0,
        azucares=2.0,
        saturadas=1.0,
        sal=sal,
        ingredientes=ingredientes,
        disponible=disponible,
        actualizado="2026-09-01T10:00:00",
    )


def test_detectar_proteina_orden_y_grupos():
    assert artema.detectar_proteina("Bacalao al pil-pil", None) == "pescado"
    assert artema.detectar_proteina("Pechuga de pavo", None) == "pollo"
    assert artema.detectar_proteina("Morcillo estofado", None) == "ternera"
    assert artema.detectar_proteina("Carrillera ibérica", None) == "cerdo"
    assert artema.detectar_proteina("Fabada asturiana", None) == "legumbre"
    assert artema.detectar_proteina("Arroz tres delicias", "Arroz, tortilla, guisante") == "otro"

    # Orden: pescado antes que pollo si ambos aparecen en el texto
    assert artema.detectar_proteina("Mar y montaña", "Merluza con pollo") == "pescado"


def test_recomendar_puntuacion_y_penalizaciones():
    p_limpio = plato_test("p1", "Pollo plancha", kcal=450.0, proteinas=30.0, sal=1.0)
    # 30.0
    p_sal = plato_test("p2", "Pavo al horno", kcal=450.0, proteinas=30.0, sal=3.0)
    # 30.0 - 5.0 = 25.0
    p_calorias = plato_test("p3", "Pollo asado", kcal=600.0, proteinas=30.0, sal=1.0)
    # 30.0 - 0.1*(100) = 20.0
    p_ambas = plato_test("p4", "Pollo frito", kcal=700.0, proteinas=30.0, sal=3.0)
    # 30.0 - 0.1*(200) - 5.0 = 5.0

    platos = [p_ambas, p_calorias, p_sal, p_limpio]
    rec = artema.recomendar(platos, n=4, max_por_proteina=4)
    assert [p.handle for p in rec] == ["p1", "p2", "p3", "p4"]


def test_recomendar_respeta_maximo_por_proteina():
    pollos = [
        plato_test(f"pollo-{i}", f"Pollo {i}", proteinas=50.0 - i, kcal=400.0) for i in range(5)
    ]
    pescados = [
        plato_test(f"pescado-{i}", f"Merluza {i}", proteinas=20.0 - i, kcal=400.0) for i in range(3)
    ]

    rec = artema.recomendar(pollos + pescados, n=8, max_por_proteina=3)
    handles = [p.handle for p in rec]
    # Solo 3 pollos aunque tengan más puntuación que los pescados
    assert handles[:3] == ["pollo-0", "pollo-1", "pollo-2"]
    assert "pollo-3" not in handles
    assert "pollo-4" not in handles
    assert "pescado-0" in handles
    assert "pescado-1" in handles
    assert "pescado-2" in handles
    assert len(rec) == 6


def test_recomendar_deja_fuera_no_principales_no_disponibles_y_sin_macros():
    valido = plato_test("valido", "Pollo rico", categoria="Principal", disponible=True)
    entrante = plato_test("entrante", "Pollo entrante", categoria="Entrante", disponible=True)
    no_disp = plato_test("no-disp", "Pollo agotado", categoria="Principal", disponible=False)
    sin_kcal = plato_test("sin-kcal", "Pollo sin kcal", kcal=None)
    sin_prot = plato_test("sin-prot", "Pollo sin proteina", proteinas=None)

    platos = [valido, entrante, no_disp, sin_kcal, sin_prot]
    rec = artema.recomendar(platos, n=8)
    assert [p.handle for p in rec] == ["valido"]


def test_recomendar_devuelve_menos_de_n_si_no_hay_mas():
    p1 = plato_test("p1", "Merluza", proteinas=30.0)
    p2 = plato_test("p2", "Pollo", proteinas=25.0)

    rec = artema.recomendar([p1, p2], n=8)
    assert len(rec) == 2
    assert [p.handle for p in rec] == ["p1", "p2"]


def test_recomendar_con_fixtures_reales(con):
    mock = ArtemaMock()
    artema.actualizar(con, mock.client, pausa_s=0)

    platos_db = db.platos_artema(con, solo_disponibles=True)
    rec = artema.recomendar(platos_db, n=8)
    assert len(rec) == 3
    # Puntuaciones: Merluza (22.8) > Albóndigas (21.1, sal 3.09 penalizada) > Macarrones (6.0)
    assert [p.handle for p in rec] == [
        "merluza-a-la-gallega",
        "albongas-salsa",
        "macarrones-bolonesa",
    ]
