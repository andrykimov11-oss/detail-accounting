"""
Сквозная проверка тестового стенда (решение владельца 20.09.2026).

ЗАЧЕМ ОТДЕЛЬНЫЕ ТЕСТЫ, ЕСЛИ МОДУЛИ УЖЕ ПОКРЫТЫ
    Модульные тесты проверяют части. Здесь проверяется, что стенд,
    собранный скриптом `tools/make_test_stand.py`, ВЕДЁТ СЕБЯ ТАК, КАК
    НАПИСАНО В ЕГО ЖЕ ИНСТРУКЦИИ: бейджи пускают, коды бланков
    опознаются, заказ вне задания отвергается, декоры показываются с
    целым числом листов.

    Иначе получится знакомая картина: скрипт отработал, на экране
    зелёные строки, а на стенде у людей ничего не сходится — и
    разбираться будут при цехе, а не здесь.

    Пять опытов ниже — ровно те пять, что названы в `SETUP-003` §7 как
    способ проверки настройки. Они идут через HTTP рабочего места, а не
    мимо него.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from flask import Flask

from src.psr_app import psr
from src.storage import Storage

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load_stand_module():
    spec = importlib.util.spec_from_file_location(
        "make_test_stand", TOOLS / "make_test_stand.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_test_stand"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def stand(tmp_path):
    """Стенд собирается тем же кодом, что и у владельца — не копией."""
    mod = _load_stand_module()
    bazis, machine = tmp_path / "bazis", tmp_path / "station"
    machine.mkdir(parents=True)
    mod.build_catalog(bazis)
    db = tmp_path / "psr.db"
    mod.seed_db(db, bazis, machine)

    app = Flask(__name__, template_folder="../src/templates")
    app.config["DB_PATH"] = str(db)
    app.register_blueprint(psr)
    c = app.test_client()
    c.storage = Storage(str(db))
    c.machine = machine
    c.mod = mod
    yield c
    c.storage.close()


def _login(client, badge="B-1001", area="raskroy"):
    r = client.post("/psr/api/login",
                    json={"badge": badge, "area": area}).get_json()
    assert r["ok"], r
    client.environ_base["HTTP_X_PSR_TOKEN"] = r["token"]
    return r


# ----------------------------------------------------------------------
# Опыт 0. Стенд собирается без персональных данных
# ----------------------------------------------------------------------
def test_на_стенде_нет_ни_одной_настоящей_фамилии(stand):
    """
    Главное свойство стенда, ради которого он и делается выдуманным:
    его можно поднять где угодно. Проверяется по тому, что записано в
    базу и в имена папок, а не по намерению автора.
    """
    names = [r["client_name"] for r in stand.storage._conn.execute(
        "SELECT client_name FROM order_links")]
    assert names and all("Тестовый заказчик" in n for n in names)

    # Имя ПАПКИ ЗАКАЗА — единственное место каталога, где в бою стоит
    # фамилия клиента (`1980-Vydumkina`). Проверяется именно оно, по
    # строгому образцу, а не по присутствию дефиса: первая редакция
    # теста довольствовалась дефисом в имени и потому считала годным
    # что угодно, включая имя станка.
    bazis = stand.machine.parent / "bazis"
    order_dirs = [d.name for machine in bazis.iterdir() if machine.is_dir()
                  for d in machine.iterdir() if d.is_dir()]
    assert order_dirs
    assert all(re.fullmatch(r"\d{4}-Test", d) for d in order_dirs), order_dirs


def test_номера_стенда_не_пересекаются_с_боевыми(stand):
    """
    Девятитысячный диапазон выбран намеренно: тестовый заказ не должен
    спутаться с боевым, если базы однажды окажутся рядом.
    """
    nums = [r["order_num"] for r in stand.storage._conn.execute(
        "SELECT order_num FROM order_links")]
    assert all(9000 <= n < 10000 for n in nums)


# ----------------------------------------------------------------------
# Опыт 1. Сменное задание совпадает с тем, что объявлено
# ----------------------------------------------------------------------
def test_опыт1_состав_задания_и_порядок_строк(stand):
    rows = stand.storage.get_shift_task_rows(stand.mod.TASK_ID)
    assert [r["row_no"] for r in rows] == [1, 1, 1, 2, 3, 3]
    assert 9003 not in {r["order_num"] for r in rows}   # намеренно вне задания


# ----------------------------------------------------------------------
# Опыт 2. Скан бланка из задания открывает заказ
# ----------------------------------------------------------------------
def test_опыт2_скан_бланка_берёт_заказ_в_работу(stand):
    _login(stand)
    r = stand.post("/psr/api/scan", json={
        "code": "ПС00-900001|2026-09-21", "area": "raskroy"}).get_json()
    assert r["ok"] and r["action"] == "opened"
    assert stand.storage.get_open_order_session(9001, "raskroy") is not None


# ----------------------------------------------------------------------
# Опыт 3. Заказ вне задания — отказ, а не подбор похожего
# ----------------------------------------------------------------------
def test_опыт3_заказ_вне_задания_отвергнут(stand):
    """
    Главный опыт настройки: он проверяет, что область поиска
    действительно ограничена заданием, а не что система «обычно
    находит». Заказ 9003 в связке есть — и именно поэтому годится.
    """
    _login(stand)
    r = stand.post("/psr/api/scan", json={
        "code": "ПС00-900003|2026-09-21", "area": "raskroy"}).get_json()
    assert r["ok"] is False
    assert "сменном задании" in r["error"]
    assert stand.storage.get_open_order_session(9003, "raskroy") is None


# ----------------------------------------------------------------------
# Опыт 4. Декоры с ЦЕЛЫМ числом листов
# ----------------------------------------------------------------------
def test_опыт4_три_декора_с_целыми_листами(stand):
    _login(stand)
    r = stand.post("/psr/api/scan", json={
        "code": "ПС00-900001|2026-09-21", "area": "raskroy"}).get_json()
    decors = {d["decor"]: d["sheets"] for d in r["nav"]["decors"]}
    assert decors == {"Kronoshpan-2500h1830-1": 4,
                      "Oreh-Karija-16-mm-1": 2,
                      "HDF-2800h2070-1": 2}
    assert all(isinstance(v, int) for v in decors.values())
    assert r["nav"]["single_decor"] is False


def test_опыт4а_один_декор_списка_не_требует(stand):
    _login(stand)
    r = stand.post("/psr/api/scan", json={
        "code": "ПС00-900002|2026-09-21", "area": "raskroy"}).get_json()
    assert r["nav"]["single_decor"] is True


# ----------------------------------------------------------------------
# Опыт 5. Подтверждение и программы в папке станка
# ----------------------------------------------------------------------
def test_опыт5_программы_появляются_после_подтверждения(stand):
    _login(stand)
    stand.post("/psr/api/scan", json={"code": "ПС00-900001|2026-09-21",
                                      "area": "raskroy"})
    отказ = stand.post("/psr/api/prepare", json={"order": 9001}).get_json()
    assert отказ["ok"] is False          # до подтверждения — нельзя

    stand.post("/psr/api/confirm", json={"order": 9001})
    r = stand.post("/psr/api/prepare", json={"order": 9001}).get_json()
    assert r["ok"] and r["programs"] == 8        # 4 + 2 + 2 листа
    assert len(list(stand.machine.iterdir())) == 8


# ----------------------------------------------------------------------
# Права и измерители на стенде
# ----------------------------------------------------------------------
def test_бейджи_стенда_пускают_и_дают_свою_роль(stand):
    for badge, role in (("B-1001", "operator"), ("B-1003", "storekeeper"),
                        ("B-1004", "chief"), ("B-1005", "technologist")):
        r = stand.post("/psr/api/login", json={"badge": badge}).get_json()
        assert r["ok"] and r["role"] == role


def test_кладовщик_заказ_на_участке_не_открывает(stand):
    """Права работают и на стенде: роль решает, а не экран."""
    _login(stand, badge="B-1003")
    r = stand.post("/psr/api/scan", json={
        "code": "ПС00-900001|2026-09-21", "area": "raskroy"}).get_json()
    assert r["ok"] is False and "не разрешено" in r["error"]


def test_измерители_участков_считаются_в_редакции_d210(stand):
    """
    Заказ 9004 собран так, что детали идут разными маршрутами — случай
    половины настоящих заказов. Измерители должны это различать.
    """
    from src.area_measures import AreaMeasures
    m = AreaMeasures(stand.storage)
    assert m.value_for_order("sklad", 9004) == 25.0        # 10 + 3 + 12 экз.
    assert m.value_for_order("sborka", 9004) == 25.0
    assert m.value_for_order("frezerovanie", 9004) == 3.0  # только с пазами
    assert m.value_for_order("prisadka", 9004) == 13.0     # 10 + 3 с отв.


def test_кромление_считает_метры_а_не_штуки(stand):
    from src.area_measures import AreaMeasures
    m = AreaMeasures(stand.storage)
    # 10 × 1800 + 3 × 1800 = 23 400 мм = 23,4 пог. м
    assert m.value_for_order("kromlenie", 9004) == 23.4


# ----------------------------------------------------------------------
# Скан по GUID — то, что будет на настоящем бланке (D-229)
# ----------------------------------------------------------------------
def test_стенд_опознаёт_заказ_по_guid(stand):
    """
    Главный опыт после находки CF-449: прежде стенд выдавал коды вида
    «ПС00-900001|2026-09-21», разборщик их принимал, всё было зелёным —
    а на настоящем бланке скан не сработал бы ни разу, потому что там
    GUID. Теперь стенд выдаёт GUID, и проверяется именно он.
    """
    _login(stand)
    guid = stand.mod._guid(9001)
    r = stand.post("/psr/api/scan", json={"code": guid,
                                          "area": "raskroy"}).get_json()
    assert r["ok"] and r["action"] == "opened"
    assert stand.storage.get_open_order_session(9001, "raskroy") is not None


def test_по_guid_заказ_вне_задания_так_же_отвергается(stand):
    """Область поиска ограничена заданием независимо от вида кода (SR-108)."""
    _login(stand)
    r = stand.post("/psr/api/scan", json={"code": stand.mod._guid(9003),
                                          "area": "raskroy"}).get_json()
    assert r["ok"] is False
    assert "сменном задании" in r["error"]


def test_чужой_guid_не_открывает_ничего(stand):
    _login(stand)
    r = stand.post("/psr/api/scan", json={
        "code": "00000000-0000-4000-8000-000000000000",
        "area": "raskroy"}).get_json()
    assert r["ok"] is False
    assert "без колонки с кодом документа" in r["error"]
