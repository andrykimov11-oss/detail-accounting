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


def test_scan_accepts_raw_guid_from_bilka(client):
    """
    Бирка Базиса несёт в QR сам GUID детали (напр. EEFD3BD1-...-...), а не
    MD5(GUID)[:10]. Приложение приводит GUID к каноническому qr и засчитывает.
    """
    op = _login_and_shift(client)
    res = _scan(client, UID_SHELF_16.upper(), op)   # сырой GUID, как на бирке
    assert res["status"] == "accepted"
    assert res["order_num"] == 6564


def _scan_qty(client, qr_code, count, operation):
    return client.post("/api/scan-qty", json={
        "qr_code": qr_code, "count": count, "area_id": AREA,
        "operator_id": OP_ID, "operation_1c": operation,
    }).get_json()


def test_scan_qty_records_batch(client):
    """Скан пачкой: одна бирка + количество → заносится N за раз (полка qty=3)."""
    _login_and_shift(client, EDGE_08)
    res = _scan_qty(client, UID_SHELF_16, 3, EDGE_08)
    assert res["status"] == "accepted"
    assert res["scanned_count"] == 3
    assert res["planned_qty"] == 3


def test_scan_qty_overplan_when_exceeds(client):
    """Ввод больше плана → превышение (не засчитывается), сигнал технологу."""
    _login_and_shift(client, EDGE_08)
    res = _scan_qty(client, UID_SHELF_16, 5, EDGE_08)   # план 3
    assert res["status"] == "overplan"


def test_board_shows_orders_and_progress(client):
    """Монитор: страница отдаётся, /api/board возвращает заказы с прогрессом."""
    assert client.get("/board").status_code == 200
    op = _login_and_shift(client, EDGE_08)
    _scan_qty(client, UID_SHELF_16, 3, op)          # закрыли кромление 0,8

    data = client.get("/api/board").get_json()
    order = next(o for o in data["orders"] if o["order_num"] == 6564)
    assert order["total"] > 0 and order["done"] >= 3
    edging = next(x for x in order["operations"] if x["operation"] == EDGE_08)
    assert edging["scanned"] == 3 and edging["status"] == "completed"


def test_packing_dashboard(client, app_db):
    """
    Дашборд упаковки: текущий заказ с прогрессом, срок/просрочка, флаги
    маршрута и цвета деталей (зелёная — целиком, жёлтая — частично).
    """
    from storage import Storage
    s = Storage(app_db)
    s.upsert_order_link(order_num=6564, status="unique",
                        order_full_num="ЛД00-006564", order_date="2026-06-09",
                        deadline="2020-01-01",           # заведомо просрочен
                        route_flags="сдвойка", client_name="Спецторг ООО")
    s.close()

    def pack(uid, n):
        return client.post("/api/scan-qty", json={
            "qr_code": uid, "count": n, "area_id": "area_packing",
            "operator_id": OP_ID, "operation_1c": "Упаковка раскроя"}).get_json()

    pack(UID_PANEL_16, 2)   # панель 2/2 → зелёная
    pack(UID_SHELF_16, 1)   # полка 1/3 → жёлтая

    assert client.get("/packing").status_code == 200
    d = client.get("/api/packing").get_json()
    cur = d["current"]
    assert cur["order_full_num"] == "ЛД00-006564"
    assert cur["overdue"] is True
    assert "сдвойка" in cur["route_flags"]
    colors = {row["color"] for row in cur["spec"]}
    assert "green" in colors and "yellow" in colors
    assert isinstance(d["queue"], list)
    assert isinstance(d["deferred"], list)


def test_scan_confirms_before_switching_orders(client, app_db):
    """
    Защита от чужого паллета: скан детали ДРУГОГО заказа не переключает молча,
    а требует подтверждения; только с флагом switch деталь идёт в новый заказ.
    """
    from storage import Storage
    other_uid = "AAAAAAAA-0000-0000-0000-000000000001"
    s = Storage(app_db)
    s.upsert_detail({
        "detail_uid": other_uid, "order_num": 9999, "qr_code": qr(other_uid),
        "pos_no": "1", "material_name": "ЛДСП", "thickness": 16,
        "length": 500, "width": 300, "qty": 1,
        "edge_l1": 0.8, "edge_l2": 0.8, "edge_w1": 0.8, "edge_w2": 0.8,
        "edge_total_len": 0, "perimeter": 0, "area": 0, "source_file": "",
    })
    s.close()

    op = _login_and_shift(client, EDGE_08)
    r1 = _scan(client, qr(UID_SHELF_16), op)        # закрепили активный заказ 6564
    assert r1["status"] == "accepted" and r1["order_num"] == 6564

    r2 = _scan(client, qr(other_uid), op)           # деталь заказа 9999
    assert r2["status"] == "other_order"
    assert r2["switch_order_num"] == 9999

    r3 = client.post("/api/scan", json={            # подтвердили переход
        "qr_code": qr(other_uid), "switch": True, "area_id": AREA,
        "operator_id": OP_ID, "operation_1c": op}).get_json()
    assert r3["status"] == "accepted" and r3["order_num"] == 9999


def test_mark_missing_closes_order_with_shortage(client, app_db):
    """
    «Нет детали»: помеченная недостача закрывает заказ (набрано + недостача =
    план), заказ уходит с экрана текущего.
    """
    PACK = "Упаковка раскроя"

    def pack(uid, n):
        return client.post("/api/scan-qty", json={
            "qr_code": uid, "count": n, "area_id": "area_packing",
            "operator_id": OP_ID, "operation_1c": PACK}).get_json()

    pack(qr(UID_PANEL_16), 2)                        # панель 2/2 — зелёная
    d = client.get("/api/packing").get_json()
    assert d["current"]["order_num"] == 6564
    assert any(x["color"] == "red" for x in d["current"]["spec"])   # полка ещё 0

    ok = client.post("/api/mark-missing", json={
        "order_num": 6564, "detail_uid": UID_SHELF_16,
        "operation_1c": PACK, "count": 3}).get_json()
    assert ok["ok"] is True

    d2 = client.get("/api/packing").get_json()       # заказ закрыт с недостачей
    assert d2["current"] is None


# --- Опыт с камерой (OQ-96) --------------------------------------------------

def _cam(client, code, camera_id="cam1"):
    return client.post("/api/camera/scan",
                       json={"code": code, "camera_id": camera_id}).get_json()


def test_camera_dedup_multipass_and_autoclose(client, app_db):
    """
    Камера: многократный проход детали засчитывается один раз; операция по
    заказу закрывается, когда распознаны все кромлёные детали. В 1С/факт не пишет.
    """
    r1 = _cam(client, qr(UID_PANEL_16))
    assert r1["status"] == "accepted" and r1["recognized"] == 1
    r1b = _cam(client, qr(UID_PANEL_16))            # тот же код (др. проход станка)
    assert r1b["status"] == "duplicate" and r1b["recognized"] == 1

    r2 = _cam(client, qr(UID_SHELF_16))
    assert r2["status"] == "accepted"
    assert r2["order_complete"] is True             # обе кромлёные детали видны

    from storage import Storage
    s = Storage(app_db)
    try:
        assert len(s.get_facts_by_order(6564)) == 0  # изоляция: боевой факт пуст
    finally:
        s.close()


def test_camera_signals_incomplete_previous_order(client, app_db):
    """Переход на другой заказ при незакрытом текущем → сигнал о недостаче."""
    from storage import Storage
    other = "BBBBBBBB-0000-0000-0000-000000000002"
    s = Storage(app_db)
    s.upsert_detail({
        "detail_uid": other, "order_num": 9999, "qr_code": qr(other),
        "pos_no": "1", "material_name": "ЛДСП", "thickness": 16,
        "length": 500, "width": 300, "qty": 1,
        "edge_l1": 0.8, "edge_l2": 0, "edge_w1": 0, "edge_w2": 0,
        "edge_total_len": 0, "perimeter": 0, "area": 0, "source_file": "",
    })
    s.close()

    _cam(client, qr(UID_PANEL_16))                  # 6564: 1 из 2 (неполно)
    r = _cam(client, qr(other))                     # деталь заказа 9999
    assert r["signal"] is not None
    assert r["signal"]["prev_order"] == 6564
    assert r["signal"]["missing_count"] >= 1


def test_camera_report(client):
    _cam(client, qr(UID_PANEL_16))
    d = client.get("/api/camera/report").get_json()
    assert d["planned_total"] >= 2 and d["recognized_total"] >= 1
    assert "resolution_large" in d["losses"]


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
