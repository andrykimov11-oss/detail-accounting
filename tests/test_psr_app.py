"""
Тесты рабочих мест первого этапа: оператор участка и начальник цеха.

Проверяется не вёрстка, а поведение:
  — одно движение оператора (скан) делает то, что нужно по состоянию;
  — справочный состав заказа виден, но ничего не отмечает;
  — экран начальника показывает позаказную картину по участкам.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from flask import Flask

from src.area_measures import EDGE_METERS, AreaMeasures
from src.psr_app import psr
from src.storage import Storage


@pytest.fixture()
def client():
    st = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    st.upsert_order_link(8952, "confirmed", order_full_num="ПС00-010109",
                         order_date="2026-08-28", client_name="Хворост")
    st.upsert_detail(dict(detail_uid="D1", order_num=8952, qr_code="D1",
                          qty=4, edge_total_len=1500.0))
    st.upsert_detail(dict(detail_uid="D2", order_num=8952, qr_code="D2",
                          qty=2, edge_total_len=900.0))
    AreaMeasures(st).set_measure("kromlenie", EDGE_METERS)

    app = Flask(__name__, template_folder="../src/templates")
    app.config["STORAGE"] = st
    app.register_blueprint(psr)
    c = app.test_client()
    c.storage = st
    yield c
    st.close()


# ----------------------------------------------------------------------
# Одно движение оператора
# ----------------------------------------------------------------------
def test_первый_скан_берёт_заказ_в_работу(client):
    r = client.post("/psr/api/scan", json={
        "code": "ПС00-010109|2026-08-28", "area": "kromlenie",
        "operator": "OP-01"}).get_json()
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
