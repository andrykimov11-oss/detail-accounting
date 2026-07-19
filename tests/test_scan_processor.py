"""
Тесты обработки события сканера (docs/scan-event-spec.md).

Покрывают все статусы обработки и принципы спецификации:
  Принцип 1 — участок → набор операций
  Принцип 3 — счётчик деталей по GUID (без экземпляров)
  Принцип 4 — дубликаты и идемпотентность (окно 5 сек)
  Принцип 6 — обработка ошибок и fallback-список деталей
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import UID_BACK_10, UID_PANEL_16, UID_SHELF_16, qr, scan
from scan_processor import (
    DUPLICATE_WINDOW_SEC,
    FactStatus,
    OrderContext,
    process_scan,
    suggest_details,
)

EDGE_08 = "Облицовывание кромки 19/0,8"
EDGE_2 = "Облицовывание кромки 19/2"
CUT_16 = "Раскрой плиты 16мм"
CUT_10 = "Раскрой плиты 10мм"


@pytest.fixture
def ctx(details) -> OrderContext:
    return OrderContext.build(6564, details)


# --- Сценарий 1: нормальный скан --------------------------------------------

def test_accepted_scan(ctx, t0):
    r = process_scan(scan(UID_SHELF_16, EDGE_08, t0), ctx)
    assert r.status is FactStatus.ACCEPTED
    assert r.detail_uid == UID_SHELF_16
    assert r.scanned_count == 1
    assert r.planned_qty == 3
    assert r.suggest_detail_list is False


# --- Сценарий 2: счётчик по GUID (Принцип 3) --------------------------------

def test_counter_increments_per_guid(ctx, t0):
    """qty=3 → оператор сканирует один QR трижды, счётчик растёт 1→2→3."""
    counts = []
    for i in range(3):
        at = t0 + timedelta(seconds=30 * i)
        r = process_scan(scan(UID_SHELF_16, EDGE_08, at), ctx)
        assert r.status is FactStatus.ACCEPTED
        counts.append(r.scanned_count)
    assert counts == [1, 2, 3]


def test_counters_are_independent_per_operation(ctx, t0):
    """Счётчик ведётся по паре (операция, деталь), операции не мешают друг другу."""
    process_scan(scan(UID_PANEL_16, EDGE_2, t0), ctx)
    r = process_scan(scan(UID_PANEL_16, CUT_16, t0, area="area_cutting"), ctx)
    assert r.status is FactStatus.ACCEPTED
    assert r.scanned_count == 1          # своя операция — свой счётчик
    assert ctx.scanned[(EDGE_2, UID_PANEL_16)] == 1


# --- Сценарий 3: дубликат в окне 5 сек (Принцип 4) --------------------------

def test_duplicate_within_window_rejected(ctx, t0):
    process_scan(scan(UID_SHELF_16, EDGE_08, t0), ctx)
    r = process_scan(
        scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=DUPLICATE_WINDOW_SEC - 1)),
        ctx,
    )
    assert r.status is FactStatus.DUPLICATE
    assert ctx.scanned[(EDGE_08, UID_SHELF_16)] == 1  # счётчик не вырос


def test_scan_after_window_counts_as_new_detail(ctx, t0):
    process_scan(scan(UID_SHELF_16, EDGE_08, t0), ctx)
    r = process_scan(
        scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=DUPLICATE_WINDOW_SEC + 1)),
        ctx,
    )
    assert r.status is FactStatus.ACCEPTED
    assert r.scanned_count == 2


def test_duplicate_window_is_per_operation(ctx, t0):
    """Быстрый скан той же детали, но на другой операции — не дубликат."""
    process_scan(scan(UID_PANEL_16, EDGE_2, t0), ctx)
    r = process_scan(
        scan(UID_PANEL_16, CUT_16, t0 + timedelta(seconds=1), area="area_cutting"),
        ctx,
    )
    assert r.status is FactStatus.ACCEPTED


# --- Сценарий 4: превышение плана -------------------------------------------

def test_overplan_detected_and_not_counted(ctx, t0):
    """qty=3, четвёртый скан → OVERPLAN, в факт не идёт."""
    for i in range(3):
        process_scan(scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=30 * i)), ctx)

    r = process_scan(scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=200)), ctx)
    assert r.status is FactStatus.OVERPLAN
    assert r.scanned_count == 4
    assert r.planned_qty == 3
    assert ctx.scanned[(EDGE_08, UID_SHELF_16)] == 3  # факт остался плановым


# --- Сценарий 5: неизвестный QR ---------------------------------------------

def test_unknown_qr_suggests_detail_list(ctx, t0):
    r = process_scan(scan("нет-такого-uid", EDGE_08, t0, qr_code="deadbeef00"), ctx)
    assert r.status is FactStatus.UNKNOWN_QR
    assert r.suggest_detail_list is True


# --- Сценарий 6: операция не применима к детали -----------------------------

def test_wrong_operation_suggests_detail_list(ctx, t0):
    """Деталь с кромкой 0,8 отсканирована на операции кромления 2мм."""
    r = process_scan(scan(UID_SHELF_16, EDGE_2, t0), ctx)
    assert r.status is FactStatus.WRONG_OPERATION
    assert r.suggest_detail_list is True
    assert ctx.scanned == {}


def test_detail_without_edge_rejected_on_edging(ctx, t0):
    """Задняя стенка без кромки не должна проходить кромление."""
    r = process_scan(scan(UID_BACK_10, EDGE_08, t0), ctx)
    assert r.status is FactStatus.WRONG_OPERATION


# --- Сценарий 7: участок и его операции (Принцип 1) -------------------------

def test_unknown_area(ctx, t0):
    r = process_scan(scan(UID_SHELF_16, EDGE_08, t0, area="area_martian"), ctx)
    assert r.status is FactStatus.UNKNOWN_AREA


def test_operation_not_in_area(ctx, t0):
    """Операция раскроя выбрана на участке кромления — отбой."""
    r = process_scan(scan(UID_PANEL_16, CUT_16, t0, area="area_edging"), ctx)
    assert r.status is FactStatus.OP_NOT_IN_AREA


def test_area_ops_override_from_directory(ctx, t0):
    """
    Справочник участков приходит из БД (Этап A). Переданный список
    операций имеет приоритет над константой AREAS в коде.
    """
    r = process_scan(
        scan(UID_SHELF_16, EDGE_08, t0, area="area_custom"),
        ctx,
        area_ops=[EDGE_08],
    )
    assert r.status is FactStatus.ACCEPTED


# --- Чужой заказ: фиксация мёртвой ветки ------------------------------------

def test_foreign_order_detail_reads_as_unknown_qr(details, t0):
    """
    Известный разрыв: OrderContext.build фильтрует детали по заказу, поэтому
    деталь чужого заказа физически отсутствует в qr_to_detail и возвращает
    UNKNOWN_QR, а не WRONG_ORDER. Ветка WRONG_ORDER в process_scan
    недостижима — контроль «чужой заказ» на деле не работает.
    """
    ctx = OrderContext.build(6564, details)
    foreign_qr = qr("BBBBBBBB-9999-0000-0000-000000000009")
    r = process_scan(scan("x", EDGE_08, t0, qr_code=foreign_qr), ctx)
    assert r.status is FactStatus.UNKNOWN_QR
    assert r.status is not FactStatus.WRONG_ORDER


# --- Fallback-список деталей (Принцип 6) ------------------------------------

def test_suggest_details_filters_by_operation(ctx):
    items = suggest_details(ctx, EDGE_08)
    uids = {i["detail_uid"] for i in items}
    assert uids == {UID_SHELF_16}          # только деталь с кромкой 0,8


def test_suggest_details_payload_is_operator_friendly(ctx):
    """Список для оператора: позиция, размеры, материал, план — без GUID наизусть."""
    item = suggest_details(ctx, EDGE_08)[0]
    assert item["pos_no"] == "2"
    assert (item["length"], item["width"], item["thickness"]) == (600, 400, 16)
    assert item["qty"] == 3
    assert item["material"] == "ЛДСП Дуб"
    assert item["qr_code"] == qr(UID_SHELF_16)


def test_suggest_details_for_cutting_operation(ctx):
    items = suggest_details(ctx, CUT_10)
    assert {i["detail_uid"] for i in items} == {UID_BACK_10}
