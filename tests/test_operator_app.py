"""
Тесты веб-интерфейса оператора (src/operator_app.py) через Flask test client.

Проверяем сценарий рабочего места: вход → участок → сканы → обратная связь.
Данные синтетические (см. conftest): заказ 6564 из трёх деталей. Реальные
данные цеха содержат ПД и в тесты не попадают.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    UID_PANEL_16,
    UID_SHELF_16,
    build_xbir,
    detail_row,
    qr,
)
from operator_app import create_app
import operator_app
from pipeline import ProductionCore
from storage import Storage

EDGE_08 = "Облицовывание кромки 19/0,8"   # применима к полке (кромка 0,8), qty=3
EDGE_2 = "Облицовывание кромки 19/2"      # применима к панели (кромка 2), qty=2
AREA = "area_edging"
OP_ID = "op_ivanov"


@pytest.fixture
def app_db(tmp_path: Path):
    """
    Готовая БД под интерфейс: справочники участков, оператор и импортированный
    заказ 6564. Возвращает путь к файлу БД.
    """
    db = tmp_path / "operator.db"
    rows = [
        detail_row(UID_PANEL_16, thickness=16, length=1928, width=838, qty=2,
                   edge_l1=2, edge_l2=2, pos="1"),
        detail_row(UID_SHELF_16, thickness=16, length=600, width=400, qty=3,
                   edge_l1=0.8, edge_l2=0.8, pos="2"),
    ]
    xbir = build_xbir(tmp_path / "order6564.xbir", rows)

    core = ProductionCore(Storage(db))
    core.seed_areas()
    core.storage.upsert_operator(OP_ID, "Иванов Иван Иванович")
    core.import_xbir([xbir])
    core.storage.close()
    return db


@pytest.fixture
def client(app_db):
    app = create_app(app_db)
    app.config["TESTING"] = True
    # Активный заказ живёт в памяти модуля — чистим между тестами.
    operator_app._active_orders.clear()
    return app.test_client()


def _login_and_shift(client, operation=EDGE_08):
    """Пройти вход и открытие смены, вернуть выбранную операцию участка."""
    r = client.post("/api/login", json={"operator_id": OP_ID})
    assert r.status_code == 200
    assert r.get_json()["operator"]["full_name"].startswith("Иванов")

    r = client.post("/api/shift", json={"operator_id": OP_ID, "area_id": AREA})
    assert r.status_code == 200
    ops = r.get_json()["operations"]
    assert operation in ops
    return operation


def _scan(client, qr_code, operation):
    return client.post("/api/scan", json={
        "qr_code": qr_code, "area_id": AREA,
        "operator_id": OP_ID, "operation_1c": operation,
    }).get_json()


# --- справочники и страница --------------------------------------------------

def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200
    # мобильный интерфейс со сканированием камерой
    assert b"reader" in r.data          # контейнер камеры html5-qrcode
    assert b"Html5Qrcode" in r.data


def test_operators_list(client):
    ops = client.get("/api/operators").get_json()
    assert any(o["operator_id"] == OP_ID for o in ops)


def test_areas_list(client):
    areas = client.get("/api/areas").get_json()
    assert any(a["area_id"] == AREA for a in areas)


# --- сценарий смены ----------------------------------------------------------

def test_login_returns_areas(client):
    r = client.post("/api/login", json={"operator_id": OP_ID}).get_json()
    assert any(a["area_id"] == AREA for a in r["areas"])


def test_shift_opens_and_lists_operations(client):
    op = _login_and_shift(client)
    assert op == EDGE_08


def test_scan_accepted_and_counter_grows(client, monkeypatch):
    # Гасим окно антидубликата, чтобы проверить рост счётчика по одной детали.
    monkeypatch.setattr(operator_app, "_active_orders", {})
    import scan_processor
    monkeypatch.setattr(scan_processor, "DUPLICATE_WINDOW_SEC", 0)

    _login_and_shift(client, EDGE_08)

    r1 = _scan(client, qr(UID_SHELF_16), EDGE_08)
    assert r1["status"] == "accepted"
    assert r1["scanned_count"] == 1
    assert r1["planned_qty"] == 3
    assert r1["order_num"] == 6564

    r2 = _scan(client, qr(UID_SHELF_16), EDGE_08)
    assert r2["status"] == "accepted"
    assert r2["scanned_count"] == 2


def test_scan_duplicate_within_window(client):
    _login_and_shift(client, EDGE_08)
    first = _scan(client, qr(UID_SHELF_16), EDGE_08)
    assert first["status"] == "accepted"
    # Повтор того же QR сразу — в окне 5с отбрасывается.
    dup = _scan(client, qr(UID_SHELF_16), EDGE_08)
    assert dup["status"] == "duplicate"


def test_unknown_qr_returns_suggest(client):
    _login_and_shift(client, EDGE_08)
    # Первый успешный скан фиксирует активный заказ.
    assert _scan(client, qr(UID_SHELF_16), EDGE_08)["status"] == "accepted"
    # Неизвестный QR в рамках активного заказа → fallback-список деталей.
    res = _scan(client, "zzzzzzzzzz", EDGE_08)
    assert res["status"] == "unknown_qr"
    assert len(res["suggest"]) >= 1
    assert any(d["detail_uid"] == UID_SHELF_16 for d in res["suggest"])


def test_no_active_order_asks_to_scan(client):
    _login_and_shift(client, EDGE_08)
    # Активного заказа ещё нет и QR не резолвится — заказ определить не по чему.
    res = _scan(client, "zzzzzzzzzz", EDGE_08)
    assert res["status"] == "no_order"
    assert res["suggest"] == []


def test_pick_detail_counts(client, monkeypatch):
    import scan_processor
    monkeypatch.setattr(scan_processor, "DUPLICATE_WINDOW_SEC", 0)

    _login_and_shift(client, EDGE_08)
    # Задаём активный заказ через принятый скан.
    assert _scan(client, qr(UID_SHELF_16), EDGE_08)["status"] == "accepted"

    res = client.post("/api/pick-detail", json={
        "detail_uid": UID_SHELF_16, "area_id": AREA,
        "operator_id": OP_ID, "operation_1c": EDGE_08,
    }).get_json()
    assert res["status"] == "accepted"
    assert res["scanned_count"] == 2


def test_status_summary(client, monkeypatch):
    import scan_processor
    monkeypatch.setattr(scan_processor, "DUPLICATE_WINDOW_SEC", 0)

    _login_and_shift(client, EDGE_08)
    _scan(client, qr(UID_SHELF_16), EDGE_08)

    res = client.get("/api/status?order_num=6564").get_json()
    assert res["order_num"] == 6564
    edge = next(o for o in res["operations"] if o["operation_1c"] == EDGE_08)
    assert edge["planned_total"] == 3
    assert edge["scanned_total"] == 1


def test_new_order_resets_active(client):
    _login_and_shift(client, EDGE_08)
    assert _scan(client, qr(UID_SHELF_16), EDGE_08)["status"] == "accepted"
    assert operator_app._active_orders.get(OP_ID) == 6564

    client.post("/api/new-order", json={"operator_id": OP_ID})
    assert OP_ID not in operator_app._active_orders


# --- Админка: управление операторами ----------------------------------------

def test_admin_page_serves(client):
    r = client.get("/admin")
    assert r.status_code == 200
    assert b"PIN" in r.data


def test_admin_requires_pin(client):
    r = client.get("/api/admin/operators")   # без PIN
    assert r.status_code == 403


def test_admin_lists_operators_with_pin(client):
    # оператор OP_ID уже заведён в фикстуре app_db
    r = client.get("/api/admin/operators", headers={"X-Admin-Pin": "0000"})
    assert r.status_code == 200
    assert any(o["operator_id"] == OP_ID for o in r.get_json())


def test_admin_add_operator(client):
    r = client.post("/api/admin/add-operator",
                    headers={"X-Admin-Pin": "0000"},
                    json={"full_name": "Новиков Сергей Петрович"})
    assert r.status_code == 200
    assert r.get_json()["operator_id"] == "op_novikov_s_p"
    # появился в списке
    lst = client.get("/api/admin/operators", headers={"X-Admin-Pin": "0000"}).get_json()
    assert any(o["operator_id"] == "op_novikov_s_p" for o in lst)


def test_admin_add_operator_wrong_pin(client):
    r = client.post("/api/admin/add-operator",
                    headers={"X-Admin-Pin": "9999"},
                    json={"full_name": "Кто-то"})
    assert r.status_code == 403


def test_admin_add_operator_empty_name(client):
    r = client.post("/api/admin/add-operator",
                    headers={"X-Admin-Pin": "0000"}, json={"full_name": ""})
    assert r.status_code == 400


def test_admin_toggle_active(client):
    client.post("/api/admin/add-operator", headers={"X-Admin-Pin": "0000"},
                json={"full_name": "Тестов Пётр Иванович"})
    # выключить
    r = client.post("/api/admin/set-active", headers={"X-Admin-Pin": "0000"},
                    json={"operator_id": "op_testov_p_i", "is_active": False})
    assert r.status_code == 200
    # выключенный не показывается операторам на входе
    login_list = client.get("/api/operators").get_json()
    assert not any(o["operator_id"] == "op_testov_p_i" for o in login_list)
    # но в админке виден
    adm = client.get("/api/admin/operators", headers={"X-Admin-Pin": "0000"}).get_json()
    assert any(o["operator_id"] == "op_testov_p_i" and not o["is_active"] for o in adm)


# --- Админ-консоль: настройки, справочники, отчёты --------------------------

def test_admin_settings_roundtrip(client):
    r = client.post("/api/admin/settings", headers={"X-Admin-Pin": "0000"},
                    json={"basis_xbir": "/net/basis", "one_c_plan": "/net/plan.xlsx"})
    assert r.status_code == 200
    got = client.get("/api/admin/settings", headers={"X-Admin-Pin": "0000"}).get_json()
    assert got["basis_xbir"] == "/net/basis"
    assert got["one_c_plan"] == "/net/plan.xlsx"


def test_admin_settings_requires_pin(client):
    assert client.get("/api/admin/settings").status_code == 403


def test_admin_areas_list(client):
    r = client.get("/api/admin/areas", headers={"X-Admin-Pin": "0000"})
    assert r.status_code == 200
    areas = r.get_json()
    assert any(a["area_id"] == AREA for a in areas)


def test_admin_add_and_delete_area_operation(client):
    client.post("/api/admin/area-operation", headers={"X-Admin-Pin": "0000"},
                json={"area_id": "area_test", "area_name": "Тестовый",
                      "operation_1c": "Тестовая операция"})
    areas = client.get("/api/admin/areas", headers={"X-Admin-Pin": "0000"}).get_json()
    test_area = next(a for a in areas if a["area_id"] == "area_test")
    assert "Тестовая операция" in test_area["operations"]

    client.post("/api/admin/area-delete", headers={"X-Admin-Pin": "0000"},
                json={"area_id": "area_test"})
    areas2 = client.get("/api/admin/areas", headers={"X-Admin-Pin": "0000"}).get_json()
    assert not any(a["area_id"] == "area_test" for a in areas2)


def test_admin_delete_operator(client):
    client.post("/api/admin/add-operator", headers={"X-Admin-Pin": "0000"},
                json={"full_name": "Удаляемый Иван Иванович"})
    r = client.post("/api/admin/delete-operator", headers={"X-Admin-Pin": "0000"},
                    json={"operator_id": "op_udalyaemyy_i_i"})
    assert r.status_code == 200
    lst = client.get("/api/admin/operators", headers={"X-Admin-Pin": "0000"}).get_json()
    assert not any(o["operator_id"] == "op_udalyaemyy_i_i" for o in lst)


def test_admin_order_report(client, monkeypatch):
    import scan_processor
    monkeypatch.setattr(scan_processor, "DUPLICATE_WINDOW_SEC", 0)
    _login_and_shift(client, EDGE_08)
    _scan(client, qr(UID_SHELF_16), EDGE_08)

    r = client.get("/api/admin/report/order?order_num=6564",
                   headers={"X-Admin-Pin": "0000"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["order_num"] == 6564
    edge = next(o for o in d["operations"] if o["operation_1c"] == EDGE_08)
    assert edge["planned"] == 3 and edge["scanned"] == 1


def test_admin_shift_report(client, monkeypatch):
    import scan_processor
    monkeypatch.setattr(scan_processor, "DUPLICATE_WINDOW_SEC", 0)
    _login_and_shift(client, EDGE_08)
    _scan(client, qr(UID_SHELF_16), EDGE_08)
    _scan(client, qr(UID_SHELF_16), EDGE_08)     # ещё одна принята

    r = client.get("/api/admin/report/shift", headers={"X-Admin-Pin": "0000"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["accepted"] >= 2
    assert d["orders"] == 1
    assert any(op == OP_ID for op, _ in d["by_operator"])


def test_admin_shift_xlsx_downloads(client, monkeypatch):
    import scan_processor
    monkeypatch.setattr(scan_processor, "DUPLICATE_WINDOW_SEC", 0)
    _login_and_shift(client, EDGE_08)
    _scan(client, qr(UID_SHELF_16), EDGE_08)

    r = client.get("/api/admin/report/shift.xlsx", headers={"X-Admin-Pin": "0000"})
    assert r.status_code == 200
    assert r.data[:2] == b"PK"          # xlsx = zip, сигнатура PK


def test_admin_run_import_needs_settings(client):
    r = client.post("/api/admin/run-import", headers={"X-Admin-Pin": "0000"}, json={})
    assert r.status_code == 400          # пути не заданы
