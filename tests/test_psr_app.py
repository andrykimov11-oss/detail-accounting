"""
Тесты рабочих мест первого этапа: оператор участка и начальник цеха.

Проверяется не вёрстка, а поведение:
  — одно движение оператора (скан) делает то, что нужно по состоянию;
  — справочный состав заказа виден, но ничего не отмечает;
  — экран начальника показывает позаказную картину по участкам;
  — действие без входа по бейджу не проходит вовсе (SR-96, SR-97).

ВХОД ОБЯЗАТЕЛЕН, И ЭТО ЧАСТЬ ПРОВЕРЯЕМОГО ПОВЕДЕНИЯ
    Клиент из фикстуры уже вошёл по бейджу и шлёт токен в заголовке —
    так же, как это делает экран. Отдельный тест проверяет обратное:
    те же запросы БЕЗ токена отклоняются. До введения прав `operator`
    был полем в теле запроса, и кто угодно мог назваться кем угодно.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from flask import Flask

from src.access import OPERATOR, Access, seed_default_permissions
from src.area_measures import EDGE_METERS, AreaMeasures
from src.psr_app import psr
from src.storage import Storage


@pytest.fixture()
def client():
    db = os.path.join(tempfile.mkdtemp(), "psr.db")
    st = Storage(db)
    st.upsert_order_link(8952, "confirmed", order_full_num="ПС00-010109",
                         order_date="2026-08-28", client_name="Хворост")
    st.upsert_detail(dict(detail_uid="D1", order_num=8952, qr_code="D1",
                          qty=4, edge_total_len=1500.0))
    st.upsert_detail(dict(detail_uid="D2", order_num=8952, qr_code="D2",
                          qty=2, edge_total_len=900.0))
    AreaMeasures(st).set_measure("kromlenie", EDGE_METERS)

    ac = Access(st)
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр Сергеевич", OPERATOR, badge_id="B-0001")

    st.close()
    # Приложение собирается ТАК ЖЕ, как в бою: соединение с БД
    # открывается на каждый запрос по пути DB_PATH (соглашение ядра).
    app = Flask(__name__, template_folder="../src/templates")
    app.config["DB_PATH"] = db
    app.register_blueprint(psr)
    c = app.test_client()
    c.storage = Storage(db)

    # Вход по бейджу — то же действие, что делает оператор у станка.
    token = c.post("/psr/api/login",
                   json={"badge": "B-0001", "area": "kromlenie"}
                   ).get_json()["token"]
    c.environ_base["HTTP_X_PSR_TOKEN"] = token
    c.token = token

    yield c
    c.storage.close()


# ----------------------------------------------------------------------
# Одно движение оператора
# ----------------------------------------------------------------------
def test_первый_скан_берёт_заказ_в_работу(client):
    r = client.post("/psr/api/scan", json={
        "code": "ПС00-010109|2026-08-28", "area": "kromlenie"}).get_json()
    assert r["ok"] and r["action"] == "opened"
    assert "взят в работу" in r["message"]


def test_повторный_скан_предлагает_закончить_а_не_открывает_второй(client):
    """
    Оператору не надо помнить, какую кнопку нажать: он делает то же
    движение, а решение принимает система по состоянию заказа.
    """
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    r = client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                           "area": "kromlenie"}).get_json()
    assert r["action"] == "already_open"
    assert len(client.storage.get_order_sessions(8952)) == 1


def test_чужой_бланк_объясняет_причину_а_не_говорит_ошибка(client):
    r = client.post("/psr/api/scan", json={"code": "8952",
                                           "area": "kromlenie"}).get_json()
    assert r["ok"] is False
    assert "ожидалась пара" in r["error"]


def test_закрытие_возвращает_длительность_и_измеритель(client):
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    r = client.post("/psr/api/close", json={"order": 8952,
                                            "area": "kromlenie"}).get_json()
    assert r["ok"]
    assert r["measure_name"] == "пог. м кромки"
    # 4 × 1500 + 2 × 900 = 7 800 мм = 7,8 пог. м
    assert r["measure_value"] == 7.8


def test_повторное_закрытие_отклонено_с_объяснением(client):
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    client.post("/psr/api/close", json={"order": 8952, "area": "kromlenie"})
    r = client.post("/psr/api/close", json={"order": 8952,
                                            "area": "kromlenie"}).get_json()
    assert r["ok"] is False
    assert "уже закрыт" in r["error"]


# ----------------------------------------------------------------------
# Справочный состав заказа: виден, но ничего не отмечает
# ----------------------------------------------------------------------
def test_состав_заказа_подтягивается_из_спецификации(client):
    r = client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                           "area": "kromlenie"}).get_json()
    card = r["order"]
    assert card["unique_details"] == 2
    assert card["total_items"] == 6
    assert card["client"] == "Хворост"


def test_справочный_состав_не_создаёт_подетальных_отметок(client):
    """
    На шаге 1 детали видны, но не отмечаются. Если бы просмотр состава
    порождал факты по деталям, подетальный учёт начался бы явочным
    порядком и исказил бы и нормативы, и переход шага 5.
    """
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    assert client.storage.count_scan_events() == 0


# ----------------------------------------------------------------------
# Экран начальника цеха
# ----------------------------------------------------------------------
def test_экран_начальника_показывает_взятое_в_работу(client):
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    html = client.get("/psr/chief").get_data(as_text=True)
    assert "Кромление" in html
    assert "Заказ 8952" in html
    assert "Хворост" in html


def test_экран_начальника_называет_незаполненный_справочник(client):
    """
    Участок без измерителя не молчит: начальник цеха видит, что технолог
    не заполнил пункт 0.3, а не пустое место.
    """
    html = client.get("/psr/chief").get_data(as_text=True)
    assert "не задан технологом" in html


def test_путь_заказа_по_участкам_виден_целиком(client):
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "raskroy"})
    client.post("/psr/api/close", json={"order": 8952, "area": "raskroy"})
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    r = client.get("/psr/api/order/8952").get_json()
    assert [h["area"] for h in r["history"]] == ["Раскрой", "Кромление"]
    assert r["history"][0]["closed_at"] is not None
    assert r["history"][1]["closed_at"] is None


# ----------------------------------------------------------------------
# SR-96, SR-97. Без входа действие не проходит
# ----------------------------------------------------------------------
def test_sr97_скан_без_входа_отклонён(client):
    """
    Главная проверка введения прав: тот же запрос, что проходит у
    вошедшего оператора, у неопознанного не проходит. До этого
    исполнитель был строкой в теле запроса, и отметка не значила ничего.
    """
    anon = client.application.test_client()
    r = anon.post("/psr/api/scan",
                  json={"code": "ПС00-010109|2026-08-28",
                        "area": "kromlenie"}).get_json()
    assert r["ok"] is False
    assert "не разрешено" in r["error"]
    assert client.storage.get_order_sessions(8952) == []


def test_sr97_закрытие_без_входа_отклонено(client):
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    anon = client.application.test_client()
    r = anon.post("/psr/api/close",
                  json={"order": 8952, "area": "kromlenie"}).get_json()
    assert r["ok"] is False and "не разрешено" in r["error"]
    assert client.storage.get_open_order_session(8952, "kromlenie") is not None


def test_sr100_в_сессии_участка_записан_код_а_не_фио(client):
    """
    SR-100: исполнитель обозначается кодом. Проверяется по тому, что
    фактически легло в базу, а не по ответу экрана.
    """
    client.post("/psr/api/scan", json={"code": "ПС00-010109|2026-08-28",
                                       "area": "kromlenie"})
    row = client.storage.get_open_order_session(8952, "kromlenie")
    assert row["operator_id"].startswith("ИСП-")
    assert "Иванов" not in str(dict(row))


def test_sr97_неопознанный_бейдж_не_даёт_токена(client):
    r = client.post("/psr/api/login", json={"badge": "B-9999"}).get_json()
    assert r["ok"] is False and "token" not in r


# ----------------------------------------------------------------------
# CHG-001. Навигация раскроя через рабочее место (SR-108 — SR-116)
# ----------------------------------------------------------------------
@pytest.fixture()
def raskroy(tmp_path):
    """Рабочее место раскроя с каталогом БАЗИС и сменным заданием."""
    from src.raskroy_nav import SETTING_BAZIS_ROOT, SETTING_MACHINE_DIR

    root, machine = tmp_path / "bazis", tmp_path / "station"
    for decor, files in (("Kronoshpan-1", ["Board-1.xPrg", "Board-2.xPrg"]),
                         ("Oreh-Karija-1", ["Board-1.xPrg"])):
        d = root / "Gabbiani" / "7709-Vydumkin" / decor
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("PROGRAM", encoding="utf-8")

    db = str(tmp_path / "psr.db")
    st = Storage(db)
    st.set_setting(SETTING_BAZIS_ROOT, str(root))
    st.set_setting(SETTING_MACHINE_DIR, str(machine))
    st.upsert_order_link(7709, "confirmed", order_full_num="ПС00-013166",
                         order_date="2026-08-24", client_name="Хворостов")
    st.upsert_order_link(1980, "confirmed", order_full_num="ПС00-011111",
                         order_date="2026-08-24", client_name="Петряева")
    st.upsert_shift_task("25.08/1", "2026-08-25", "ЛДСП")
    st.add_shift_task_row("25.08/1", 7, "ПС00-013166", "2026-08-24",
                          "Kronoshpan-1", 4, order_num=7709)
    st.add_shift_task_row("25.08/1", 7, "ПС00-013166", "2026-08-24",
                          "Oreh-Karija-1", 2, order_num=7709)
    ac = Access(st)
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-0001")
    st.close()

    app = Flask(__name__, template_folder="../src/templates")
    app.config["DB_PATH"] = db
    app.register_blueprint(psr)
    c = app.test_client()
    c.storage = Storage(db)
    c.machine = machine
    token = c.post("/psr/api/login",
                   json={"badge": "B-0001", "area": "raskroy"}
                   ).get_json()["token"]
    c.environ_base["HTTP_X_PSR_TOKEN"] = token
    yield c
    c.storage.close()


def test_sr108_область_поиска_задаёт_сервер_а_не_экран(raskroy):
    """
    Заказ 1980 связан с документом, но в сменное задание не входит.
    Экран не может расширить область поиска, подав своё поле: область
    берётся из задания на сервере. До правки список приходил из тела
    запроса, и пустой список означал поиск по всему потоку.
    """
    r = raskroy.post("/psr/api/scan", json={
        "code": "ПС00-011111|2026-08-24", "area": "raskroy",
        "shift_task": None}).get_json()
    assert r["ok"] is False
    assert "сменном задании" in r["error"]


def test_sr111_скан_на_раскрое_даёт_декоры_и_листы(raskroy):
    r = raskroy.post("/psr/api/scan", json={
        "code": "ПС00-013166|2026-08-24", "area": "raskroy"}).get_json()
    assert r["ok"] and r["action"] == "opened"
    assert r["task_id"] == "25.08/1"
    decors = {d["decor"]: d["sheets"] for d in r["nav"]["decors"]}
    assert decors == {"Kronoshpan-1": 4, "Oreh-Karija-1": 2}
    assert r["nav"]["confirmed"] is False
    assert r["nav"]["single_decor"] is False


def test_навигация_только_на_раскрое(raskroy):
    """На прочих участках программ станка не существует — навигации нет."""
    raskroy.storage.set_area_measure("kromlenie", "detail_items", "детали")
    r = raskroy.post("/psr/api/scan", json={
        "code": "ПС00-013166|2026-08-24", "area": "kromlenie"}).get_json()
    assert r["ok"] and r["nav"] is None


def test_sr114_программы_копируются_только_после_подтверждения(raskroy):
    raskroy.post("/psr/api/scan", json={"code": "ПС00-013166|2026-08-24",
                                        "area": "raskroy"})
    r = raskroy.post("/psr/api/prepare", json={"order": 7709}).get_json()
    assert r["ok"] is False and "подтверждено" in r["error"]

    raskroy.post("/psr/api/confirm", json={"order": 7709})
    r = raskroy.post("/psr/api/prepare", json={"order": 7709}).get_json()
    assert r["ok"] and r["programs"] == 3
    assert len(list(raskroy.machine.iterdir())) == 3


def test_sr113_подтверждение_несёт_код_вошедшего(raskroy):
    raskroy.post("/psr/api/scan", json={"code": "ПС00-013166|2026-08-24",
                                        "area": "raskroy"})
    raskroy.post("/psr/api/confirm", json={"order": 7709})
    hist = raskroy.storage.link_confirmation_history(7709)
    assert len(hist) == 1
    assert hist[0]["person_code"].startswith("ИСП-")
    assert "Иванов" not in str(dict(hist[0]))


def test_sr113_отзыв_через_экран(raskroy):
    raskroy.post("/psr/api/confirm", json={"order": 7709})
    r = raskroy.post("/psr/api/confirm",
                     json={"order": 7709, "revoke": True}).get_json()
    assert r["ok"] and r["confirmed"] is False
    assert [h["event"] for h in
            raskroy.storage.link_confirmation_history(7709)] == \
        ["confirmed", "revoked"]


def test_sr100_ответ_подготовки_не_несёт_путей(raskroy):
    """В путях каталога БАЗИС — фамилия клиента. Наружу идёт только счёт."""
    raskroy.post("/psr/api/confirm", json={"order": 7709})
    r = raskroy.post("/psr/api/prepare", json={"order": 7709}).get_json()
    assert "Vydumkin" not in str(r) and "/" not in str(r.get("message", ""))
    assert set(r) <= {"ok", "programs", "message"}


def test_подтверждение_без_входа_отклонено(raskroy):
    anon = raskroy.application.test_client()
    r = anon.post("/psr/api/confirm", json={"order": 7709}).get_json()
    assert r["ok"] is False and "не разрешено" in r["error"]
    assert raskroy.storage.link_confirmation_history(7709) == []
