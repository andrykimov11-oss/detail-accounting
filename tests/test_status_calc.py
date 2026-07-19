"""
Тесты сверки план/факт и статуса операции (src/status_calc.py).

Сверка план/факт — это и есть детектор потерь деталей, ради которого
затевался проект. Ключевой сценарий — «отсканировано 294 из 295».
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import UID_PANEL_16, UID_SHELF_16, scan
from operation_plan import build_order_plan
from scan_processor import OrderContext, process_scan
from status_calc import (
    OpStatus,
    calc_operation_status,
    format_status_report,
    to_1c_payload,
)

EDGE_08 = "Облицовывание кромки 19/0,8"
EDGE_2 = "Облицовывание кромки 19/2"
CUT_16 = "Раскрой плиты 16мм"
PACKING = "Упаковка паллета"


@pytest.fixture
def ctx(details) -> OrderContext:
    return OrderContext.build(6564, details)


def plan_for(details, op: str):
    plans, _ = build_order_plan(details, [op])
    return plans[0]


def scan_n(ctx, uid: str, op: str, t0, n: int, area: str = "area_edging"):
    """Отсканировать деталь n раз с интервалом вне окна дубликатов."""
    for i in range(n):
        process_scan(scan(uid, op, t0 + timedelta(seconds=30 * i), area=area), ctx)


# --- Базовые статусы ---------------------------------------------------------

def test_not_started(details, ctx):
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    assert s.status is OpStatus.NOT_STARTED
    assert s.scanned_total == 0
    assert s.progress_pct == 0.0


def test_in_progress(details, ctx, t0):
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 2)      # план 3
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    assert s.status is OpStatus.IN_PROGRESS
    assert (s.scanned_total, s.planned_total) == (2, 3)
    assert s.progress_pct == pytest.approx(66.7, abs=0.1)


def test_completed_closes_operation(details, ctx, t0):
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 3)      # план 3
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    assert s.status is OpStatus.COMPLETED
    assert s.is_closing is True
    assert s.lost_uids == []


# --- Детектор потери детали: главный сценарий проекта -----------------------

def test_missing_one_detail_keeps_operation_open(details, ctx, t0):
    """
    «Отсканировано 294 из 295» в миниатюре: не хватает одной детали —
    операция НЕ закрывается, потерянная деталь попадает в реестр.
    """
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 2)      # план 3, отсканировано 2
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)

    assert s.status is OpStatus.IN_PROGRESS
    assert s.is_closing is False
    assert s.lost_uids == [UID_SHELF_16]

    progress = s.details_progress[0]
    assert progress.remaining == 1
    assert progress.is_done is False


def test_partial_across_several_details(details, ctx, t0):
    """Раскрой 16мм: план 5 (панель 2 + полка 3), отсканировано 4."""
    scan_n(ctx, UID_PANEL_16, CUT_16, t0, 2, area="area_cutting")
    scan_n(ctx, UID_SHELF_16, CUT_16, t0 + timedelta(minutes=5), 2,
           area="area_cutting")

    s = calc_operation_status(plan_for(details, CUT_16), ctx)
    assert (s.scanned_total, s.planned_total) == (4, 5)
    assert s.lost_uids == [UID_SHELF_16]
    assert s.is_closing is False


# --- Превышение плана --------------------------------------------------------

def test_overplan_does_not_inflate_fact(details, ctx, t0):
    """
    Сканы сверх плана не увеличивают засчитанный факт: process_scan
    не пишет их в ctx.scanned, поэтому операция закрывается ровно по плану.
    """
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 5)      # план 3, сканов 5
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    assert s.scanned_total == 3
    assert s.overplan_total == 0
    assert s.status is OpStatus.COMPLETED


def test_overplan_status_is_unreachable(details, ctx):
    """
    ДЕФЕКТ (найден тестом). Ветка OpStatus.OVERPLAN недостижима.

    scanned_total считается как Σ min(факт, план), поэтому
    «scanned_total >= planned_total» выполняется тогда и только тогда,
    когда lost_uids пуст. Условие COMPLETED срабатывает раньше и
    перехватывает все случаи, на которые рассчитан OVERPLAN.

    Следствие: превышение плана видно только в поле overplan_total,
    но статус операции показывает «Выполнено» — и она уходит в 1С
    как закрытая, хотя деталей обработано больше плановых.
    """
    ctx.scanned[(EDGE_08, UID_SHELF_16)] = 5       # план 3

    s = calc_operation_status(plan_for(details, EDGE_08), ctx)

    assert s.overplan_total == 2                   # превышение зафиксировано
    assert s.status is OpStatus.COMPLETED          # но статус этого не отражает
    assert s.is_closing is True                    # и операция уйдёт в 1С


def test_extra_uids_detected(details, ctx):
    """Факт по детали, которой нет в плане операции — аномалия."""
    ctx.scanned[(EDGE_08, "CCCCCCCC-0000-0000-0000-000000000000")] = 1
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    assert len(s.extra_uids) == 1


# --- Позаказные операции -----------------------------------------------------

def test_document_operation_never_closes_by_scanner(details, ctx):
    s = calc_operation_status(plan_for(details, PACKING), ctx)
    assert s.status is OpStatus.NOT_STARTED
    assert s.planned_total == 0
    assert s.is_closing is False


# --- Payload для 1С ----------------------------------------------------------

def test_payload_only_when_completed(details, ctx, t0):
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 3)
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    payload = to_1c_payload(s)

    assert payload["status"] == "Выполнено"
    assert payload["completed_at"] is not None
    assert payload["order_num"] == 6564
    assert payload["scanned_details"] == payload["planned_details"] == 3


def test_payload_empty_while_in_progress(details, ctx, t0):
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 1)
    payload = to_1c_payload(calc_operation_status(plan_for(details, EDGE_08), ctx))
    assert payload["status"] is None
    assert payload["completed_at"] is None


# --- Отчёт -------------------------------------------------------------------

def test_status_report_renders(details, ctx, t0):
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 3)
    plans, _ = build_order_plan(details, [EDGE_08, EDGE_2, PACKING])
    report = format_status_report([calc_operation_status(p, ctx) for p in plans])
    assert "заказ 6564" in report
    assert "READY для записи в 1С" in report


def test_lost_status_is_declared_but_never_assigned(details, ctx, t0):
    """
    Разрыв: OpStatus.LOST объявлен, но ни одна ветка расчёта его не
    присваивает — логики «конец смены → потеря» нет. Реестр потерянных
    деталей сейчас держится только на lost_uids.
    """
    scan_n(ctx, UID_SHELF_16, EDGE_08, t0, 1)
    s = calc_operation_status(plan_for(details, EDGE_08), ctx)
    assert s.status is not OpStatus.LOST
    assert s.lost_uids                     # потеря видна только так
