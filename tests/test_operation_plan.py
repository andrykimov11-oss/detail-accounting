"""
Тесты правил операций и планового состава
(src/operation_mapping.py, src/operation_plan.py).

Плановое количество — основа закрытия операции: «Выполнено» ⇔ отсканировано
= плану. Ошибка здесь тихо ломает весь учёт, поэтому правила покрыты явно.
"""
from __future__ import annotations

import pytest

from conftest import UID_BACK_10, UID_PANEL_16, UID_SHELF_16
from operation_mapping import OPERATION_RULES, find_rule
from operation_plan import build_order_plan, format_plan_report

CUT_16 = "Раскрой плиты 16мм"
CUT_10 = "Раскрой плиты 10мм"
EDGE_08 = "Облицовывание кромки 19/0,8"
EDGE_2 = "Облицовывание кромки 19/2"
PACKING = "Упаковка паллета"


# --- Реестр правил -----------------------------------------------------------

def test_find_rule_exact_match():
    assert find_rule(CUT_16) is not None
    assert find_rule("Раскрой плиты 16 мм") is None   # сопоставление точное


def test_unknown_operation_has_no_rule():
    assert find_rule("Полировка алмазная") is None


def test_rule_names_are_unique():
    names = [r.operation_1c for r in OPERATION_RULES]
    assert len(names) == len(set(names))


# --- Предикаты: какие детали попадают в операцию ----------------------------

def test_cutting_selects_by_thickness(details):
    rule = find_rule(CUT_16)
    picked = {d.detail_uid for d in details if rule.detail_predicate(d)}
    assert picked == {UID_PANEL_16, UID_SHELF_16}   # обе 16мм


def test_cutting_10mm_selects_only_thin(details):
    rule = find_rule(CUT_10)
    picked = {d.detail_uid for d in details if rule.detail_predicate(d)}
    assert picked == {UID_BACK_10}


def test_edging_selects_by_edge_thickness(details):
    picked_08 = {d.detail_uid for d in details if find_rule(EDGE_08).detail_predicate(d)}
    picked_2 = {d.detail_uid for d in details if find_rule(EDGE_2).detail_predicate(d)}
    assert picked_08 == {UID_SHELF_16}
    assert picked_2 == {UID_PANEL_16}


def test_document_operations_select_nothing(details):
    """Позаказные операции (упаковка, ОТК) не подетальные — план всегда пуст."""
    for op in (PACKING, "Операция ОТК", "Приемка на СГП", "Передача клиенту"):
        rule = find_rule(op)
        assert not any(rule.detail_predicate(d) for d in details), op


# --- Плановый состав ---------------------------------------------------------

def test_plan_counts_instances_not_unique(details):
    """
    Плановое количество считается в экземплярах (Σ qty), а не в уникальных
    GUID: панель qty=2 + полка qty=3 = 5 деталей на раскрое 16мм.
    """
    plans, _ = build_order_plan(details, [CUT_16])
    plan = plans[0]
    assert plan.planned_instances == 5
    assert plan.planned_unique == 2
    assert plan.planned_qty == 5


def test_plan_normalized_units(details):
    """Загрузка участка меряется в м² и пог.м, а не в заказах."""
    plans, _ = build_order_plan(details, [EDGE_08])
    plan = plans[0]
    # полка 600x400, кромка по всем 4 сторонам, qty=3
    assert plan.planned_edge_m == pytest.approx((0.6 * 2 + 0.4 * 2) * 3, abs=0.01)
    assert plan.planned_area_m2 == pytest.approx(0.24 * 3, abs=0.01)


def test_document_operation_is_not_per_detail(details):
    plans, _ = build_order_plan(details, [PACKING])
    assert plans[0].is_per_detail is False
    assert plans[0].planned_qty == 0


def test_unknown_operation_produces_warning(details):
    plans, warnings = build_order_plan(details, ["Полировка алмазная"])
    assert warnings == ["Полировка алмазная"]
    assert plans[0].is_per_detail is False
    assert "НЕТ ПРАВИЛА" in plans[0].note


def test_edging_35_and_19_share_predicate_and_double_count(details):
    """
    ДЕФЕКТ (найден тестом). Правила «Облицовывание кромки 19/2» и
    «Облицовывание кромки 35/2» используют один предикат _has_edge(2.0)
    и различаются только именем. Толщина ДСП (19 vs 35) в предикате
    не учитывается.

    Следствие: если в заказе обе операции, одна и та же деталь попадёт
    в плановый состав обеих — план по кромке 2мм задвоится, а закрыть
    операцию 35/2 будет нечем (её деталей физически нет).
    """
    plans, _ = build_order_plan(details, [EDGE_2, "Облицовывание кромки 35/2"])
    plan_19, plan_35 = plans

    assert plan_19.detail_uids == plan_35.detail_uids == [UID_PANEL_16]
    assert plan_19.planned_qty == plan_35.planned_qty == 2

    # Итог по участку кромления 2мм задваивается
    assert plan_19.planned_qty + plan_35.planned_qty == 4   # физически деталей 2


def test_operation_without_matching_details_marked_not_per_detail(details):
    """
    Операция с правилом, но без подходящих деталей помечается как
    позаказная — её нельзя отличить от документооборотной.
    """
    plans, _ = build_order_plan(details, ["Облицовывание кромки 19/0,4"])
    assert plans[0].is_per_detail is False
    assert plans[0].planned_qty == 0


def test_plan_report_renders(details):
    plans, warnings = build_order_plan(details, [CUT_16, EDGE_08, PACKING])
    report = format_plan_report(6564, plans, warnings)
    assert "заказ 6564" in report
    assert CUT_16 in report
    assert "ИТОГО к сканированию" in report
