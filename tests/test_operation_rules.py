"""
Тесты построения правил из справочника операций 1С (src/operation_rules.py).

Названия операций взяты из реального справочника цеха (227 операций,
6 участков) — это регрессия на фактических данных, а не на выдуманных.
"""
from __future__ import annotations

import pytest

from operation_rules import (
    build_rules,
    find_collisions,
    format_rules_report,
    is_tariff_variant,
    parse_edge_thickness,
    parse_panel_thickness,
)
from xbir_parser import Detail


def detail(thickness: int = 16, **edges) -> Detail:
    return Detail(detail_uid="X", order_num=1, thickness=thickness,
                  length=600, width=400, qty=1, **edges)


# --- Толщина кромки из названия ---------------------------------------------

@pytest.mark.parametrize("operation,expected", [
    ("Облицовывание кромки 19/0,4", 0.4),
    ("Облицовывание кромки 19/0,8", 0.8),
    ("Облицовывание кромки 19/2", 2.0),
    ("Облицовывание кромки 35/2", 2.0),
    ("Облицовывание кромки 26,29/2мм", 2.0),
    ("Облицовывание кромки 19/1", 1.0),
    ("Облицовывание узких деталей 19/0,4", 0.4),
    ("Облицовывание прямолинейной кромки 0,4", 0.4),
    ("Облицовывание прямолинейной кромки 2", 2.0),
])
def test_parse_edge_thickness(operation, expected):
    assert parse_edge_thickness(operation) == expected


def test_edge_typo_without_comma():
    """«19/04» — потерянная запятая, реальная опечатка в справочнике."""
    assert parse_edge_thickness("Облицовывание криволинейных деталей 19/04") == 0.4


@pytest.mark.parametrize("operation", [
    "Облицовывание кромки столешницы",
    "Облицовка кромки кантом",
    "Облицовывание косого реза столешницы",
])
def test_edge_thickness_absent(operation):
    """Толщина не указана — операция останется позаказной, а не отберёт всё."""
    assert parse_edge_thickness(operation) is None


# --- Толщина ДСП из названия -------------------------------------------------

@pytest.mark.parametrize("operation,expected", [
    ("Раскрой плиты 16мм", (16,)),
    ("Раскрой плиты 10мм", (10,)),
    ("Рез ЛДСП 10, 16мм (по высоте/ширине)", (10, 16)),
    ("Контурная обработка ЛДСП 22 мм (Tech Z1)", (22,)),
])
def test_parse_panel_thickness(operation, expected):
    assert parse_panel_thickness(operation) == expected


def test_thickness_absent_in_1c_is_dropped():
    """
    В названии 26 мм, но таких деталей в .xbir не бывает (толщины 3, 4,
    10, 16, 18, 22). Несуществующая толщина отбрасывается.
    """
    assert parse_panel_thickness("Раскрой плиты 22мм и 26мм") == (22,)


def test_no_thickness_in_name():
    assert parse_panel_thickness("Раскрой ДВПО / ХДФО") == ()


# --- Тарифные надбавки -------------------------------------------------------

@pytest.mark.parametrize("operation", [
    "Раскрой плиты 16мм (свыше 30 м.п.)",
    "Раскрой ЭГГЕР 16мм сложный(свыше 30 м.п.)",
    "Раскрой плиты 16мм (ГЛЯНЕЦ)",
    "Раскрой плиты 10мм (сборка)",
    "Облицовывание плиты AGT кромкой 22/1 (матовый)",
    "НАНЕСЕНИЕ КЛЕЯ 19/0,4-2мм",
])
def test_tariff_variants_detected(operation):
    assert is_tariff_variant(operation)


@pytest.mark.parametrize("operation", [
    "Раскрой плиты 16мм",
    "Облицовывание кромки 19/0,4",
    "Облицовывание кромки 19/2",
])
def test_base_operations_are_not_tariff(operation):
    assert not is_tariff_variant(operation)


# --- Построение правил -------------------------------------------------------

AREA_MAP = {
    "Раскрой плиты 16мм": "Раскрой",
    "Раскрой плиты 16мм (свыше 30 м.п.)": "Раскрой",
    "Раскрой плиты 10мм": "Раскрой",
    "Облицовывание кромки 19/0,4": "Кромление",
    "Облицовывание кромки 19/2": "Кромление",
    "Облицовывание кромки 35/2": "Кромление",
    "Операция ОТК": "Склад готовой продукции",
    "Упаковка паллета": "Склад готовой продукции",
    "Сборка модуля (простая)": "Сборка",
    "Еврозапил": "Фрезерование",
}


def test_build_rules_counts():
    rules, report = build_rules(AREA_MAP)
    assert report.operations_total == 10
    assert report.per_detail == 5          # 2 раскроя + 3 кромления
    assert report.tariff == 1              # «свыше 30 м.п.»
    assert report.order_level == 4         # ОТК, упаковка, сборка, еврозапил


def test_warehouse_operations_are_order_level():
    rules, _ = build_rules(AREA_MAP)
    assert not rules["Операция ОТК"].is_per_detail
    assert not rules["Упаковка паллета"].is_per_detail


def test_milling_without_characteristic_is_order_level():
    """Фрезерование подетальное по природе, но «Еврозапил» не даёт признака."""
    rules, _ = build_rules(AREA_MAP)
    assert not rules["Еврозапил"].is_per_detail


def test_tariff_linked_to_primary():
    rules, _ = build_rules(AREA_MAP)
    tariff = rules["Раскрой плиты 16мм (свыше 30 м.п.)"]
    assert tariff.is_tariff
    assert tariff.primary_operation == "Раскрой плиты 16мм"


# --- Отбор деталей -----------------------------------------------------------

def test_edging_rule_matches_by_edge():
    rules, _ = build_rules(AREA_MAP)
    rule = rules["Облицовывание кромки 19/0,4"]
    assert rule.matches(detail(edge_l1=0.4))
    assert not rule.matches(detail(edge_l1=2.0))
    assert not rule.matches(detail())


def test_cutting_rule_matches_by_thickness():
    rules, _ = build_rules(AREA_MAP)
    rule = rules["Раскрой плиты 16мм"]
    assert rule.matches(detail(thickness=16))
    assert not rule.matches(detail(thickness=10))


def test_tariff_rule_takes_no_details():
    """Надбавка не забирает план — деталь считается один раз."""
    rules, _ = build_rules(AREA_MAP)
    assert not rules["Раскрой плиты 16мм (свыше 30 м.п.)"].matches(detail(thickness=16))


# --- Коллизии предикатов -----------------------------------------------------

def test_collision_detected_for_19_2_and_35_2():
    """
    ДЕФЕКТ ИСХОДНОГО КОДА, теперь обнаруживаемый.

    «Облицовывание кромки 19/2» и «35/2» отбирают одни и те же детали
    (кромка 2 мм): толщина ДСП из названия (19 vs 35) не применяется —
    таких толщин в .xbir нет. В 498 заказах эти операции встречаются
    вместе, и план по кромке задвоился бы.
    """
    rules, _ = build_rules(AREA_MAP)
    collisions = find_collisions(list(rules.values()))

    edge_2 = [ops for sig, ops in collisions.items() if sig[1] == 2.0]
    assert edge_2, "коллизия по кромке 2 мм должна быть обнаружена"
    assert set(edge_2[0]) == {"Облицовывание кромки 19/2",
                              "Облицовывание кромки 35/2"}


def test_tariff_variants_do_not_collide():
    """Надбавки исключены из коллизий — они плана не берут."""
    rules, _ = build_rules(AREA_MAP)
    collisions = find_collisions(list(rules.values()))
    for ops in collisions.values():
        assert "Раскрой плиты 16мм (свыше 30 м.п.)" not in ops


# --- Дедупликация плана внутри заказа ----------------------------------------

def test_colliding_operations_count_details_once():
    """
    Две операции с одним предикатом в одном заказе не должны задвоить план.

    Без дедупликации план по кромке 2 мм был бы вдвое больше физического
    состава, и операция НИКОГДА не закрылась бы: отсканировано всегда
    меньше планового.
    """
    from operation_plan import build_order_plan

    rules, _ = build_rules(AREA_MAP)
    details = [
        Detail(detail_uid="A", order_num=1, thickness=16, length=600,
               width=400, qty=2, edge_l1=2.0),
    ]
    plans, _ = build_order_plan(
        details,
        ["Облицовывание кромки 19/2", "Облицовывание кромки 35/2"],
        rules=rules,
    )
    per_detail = [p for p in plans if p.is_per_detail]

    assert len(per_detail) == 1
    assert per_detail[0].planned_qty == 2          # не 4

    skipped = [p for p in plans if not p.is_per_detail]
    assert "учтены там" in skipped[0].note


def test_detail_counted_once_per_cutting_stage():
    """
    На раскрое деталь режется один раз: две операции раскроя с разными
    предикатами, но пересекающимся составом, не считают её дважды.
    """
    from operation_plan import build_order_plan

    area = {"Раскрой плиты 16мм": "Раскрой",
            "Рез ЛДСП 10, 16мм (по высоте/ширине)": "Раскрой"}
    rules, _ = build_rules(area)
    details = [Detail(detail_uid="A", order_num=1, thickness=16,
                      length=600, width=400, qty=3)]

    plans, _ = build_order_plan(details, list(area), rules=rules)
    assert sum(p.planned_qty for p in plans if p.is_per_detail) == 3


def test_edging_allows_detail_in_two_operations():
    """
    На кромлении наоборот: деталь с кромками 0,4 и 2 законно проходит
    две операции — дедупликация по участку тут была бы ошибкой.
    """
    from operation_plan import build_order_plan

    rules, _ = build_rules(AREA_MAP)
    details = [Detail(detail_uid="A", order_num=1, thickness=16, length=600,
                      width=400, qty=1, edge_l1=0.4, edge_w1=2.0)]

    plans, _ = build_order_plan(
        details, ["Облицовывание кромки 19/0,4", "Облицовывание кромки 19/2"],
        rules=rules)
    assert len([p for p in plans if p.is_per_detail]) == 2


def test_tariff_without_primary_takes_the_plan():
    """
    В заказе только надбавка «свыше 30 м.п.» без базовой операции —
    участок раскроя не должен остаться без плана.
    """
    from operation_plan import build_order_plan

    rules, _ = build_rules(AREA_MAP)
    details = [Detail(detail_uid="A", order_num=1, thickness=16,
                      length=600, width=400, qty=5)]

    plans, _ = build_order_plan(
        details, ["Раскрой плиты 16мм (свыше 30 м.п.)"], rules=rules)

    per_detail = [p for p in plans if p.is_per_detail]
    assert len(per_detail) == 1
    assert per_detail[0].planned_qty == 5


def test_tariff_with_primary_takes_nothing():
    from operation_plan import build_order_plan

    rules, _ = build_rules(AREA_MAP)
    details = [Detail(detail_uid="A", order_num=1, thickness=16,
                      length=600, width=400, qty=5)]

    plans, _ = build_order_plan(
        details,
        ["Раскрой плиты 16мм", "Раскрой плиты 16мм (свыше 30 м.п.)"],
        rules=rules)

    assert sum(p.planned_qty for p in plans if p.is_per_detail) == 5


def test_report_renders_collisions():
    rules, report = build_rules(AREA_MAP)
    text = format_rules_report(report, find_collisions(list(rules.values())))
    assert "КОЛЛИЗИИ ПРЕДИКАТОВ" in text
    assert "тарифных надбавок" in text
