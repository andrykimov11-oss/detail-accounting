"""
Тесты натуральных измерителей участков (SR-26а — SR-26е).

Способы проверки взяты из SRS-001 §22 дословно:
    SR-26а  «Справочник участков» → у каждого из шести участков
            ровно один измеритель
    SR-26г  измеритель фрезерования — уникальная деталь, ПРОХОДЯЩАЯ
            фрезерование
    SR-26д  «Измеритель сборки в справочнике» → изделие из поля
            «Обозначение изделия»
    SR-26е  «Измеритель склада в справочнике» → уникальная деталь заказа

SR-26б проверяется отдельно и по существу: значение вычисляется из
спецификации и НЕ ЗАВИСИТ от отметок оператора.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.area_measures import (
    DETAIL_ITEMS,
    EDGE_METERS,
    PRODUCTS,
    SHEETS,
    UNIQUE_DETAILS,
    AreaMeasures,
    MeasureNotComputable,
    MeasureNotSet,
)
from src.storage import Storage

AREAS = ["raskroy", "kromlenie", "frezerovanie", "prisadka", "sborka", "sklad"]


@pytest.fixture()
def st():
    s = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    yield s
    s.close()


@pytest.fixture()
def m(st):
    return AreaMeasures(st)


def _detail(st, uid, order=8952, qty=1, edge=0.0, grooves=0, drill=0,
            product="", plate=0):
    st.upsert_detail(dict(detail_uid=uid, order_num=order, qr_code=uid,
                          qty=qty, edge_total_len=edge, grooves=grooves,
                          drill_total=drill, product_code=product,
                          plate_no=plate))


# ----------------------------------------------------------------------
# SR-26а. Ровно один измеритель на участок
# ----------------------------------------------------------------------
def test_sr26а_повторное_задание_заменяет_а_не_добавляет(m):
    m.set_measure("kromlenie", EDGE_METERS)
    m.set_measure("kromlenie", UNIQUE_DETAILS)
    assert len(m.all_measures()) == 1
    assert m.get_measure("kromlenie").measure_code == UNIQUE_DETAILS


def test_sr26а_шесть_участков_шесть_измерителей(m):
    for a, code in zip(AREAS, [SHEETS, EDGE_METERS, "unique_milled",
                               DETAIL_ITEMS, PRODUCTS, UNIQUE_DETAILS]):
        m.set_measure(a, code)
    assert len(m.all_measures()) == 6
    assert m.areas_without_measure(AREAS) == []


def test_sr26а_незаполненный_участок_отличим_от_ошибки(m):
    """
    Незаполненный справочник — это состояние пункта 0.3 шага 0, а не сбой.
    Поэтому исключение названо отдельно и сообщает, кто заполняет.
    """
    with pytest.raises(MeasureNotSet) as e:
        m.get_measure("raskroy")
    assert "технолог" in str(e.value).lower()


def test_признак_завершения_пункта_03_проверяется_списком(m):
    """Пункт 0.3 завершён, когда заполнены все шесть участков."""
    m.set_measure("kromlenie", EDGE_METERS)
    m.set_measure("sborka", PRODUCTS)
    assert m.areas_without_measure(AREAS) == [
        "raskroy", "frezerovanie", "prisadka", "sklad"]


# ----------------------------------------------------------------------
# SR-26б. Значение из спецификации, а не из отметок
# ----------------------------------------------------------------------
def test_sr26б_кромка_считается_по_экземплярам_а_не_по_позициям(st, m):
    """
    Оклеивается каждый экземпляр детали, а не строка спецификации.
    2 × 1500 мм + 3 × 900 мм = 5 700 мм = 5,7 пог. м.
    """
    m.set_measure("kromlenie", EDGE_METERS)
    _detail(st, "D1", qty=2, edge=1500.0)
    _detail(st, "D2", qty=3, edge=900.0)
    assert m.value_for_order("kromlenie", 8952) == 5.7


def test_sr26б_отметки_оператора_на_значение_не_влияют(st, m):
    """
    Ключевое свойство: выработка участка не зависит от того, КАК оператор
    отмечал. Иначе участок мог бы влиять на собственный норматив, а на
    нормативе стоит подсветка отклонения (шаг 2).

    Проверяем прямо: добавляем событие скана и убеждаемся, что значение
    измерителя не изменилось.
    """
    m.set_measure("kromlenie", EDGE_METERS)
    _detail(st, "D1", qty=2, edge=1500.0)
    before = m.value_for_order("kromlenie", 8952)

    st.log_scan_event(dict(scan_id="S1", qr_code="D1", area_id="kromlenie",
                           operator_id="OP-01", operation_1c="Кромление",
                           scanned_at="2026-09-14T10:00:00", status="accepted",
                           detail_uid="D1", scanned_count=1, planned_qty=2))

    assert m.value_for_order("kromlenie", 8952) == before


def test_sr26г_фрезерование_только_детали_с_пазами(st, m):
    """
    Считать весь заказ значило бы завысить выработку участка в разы:
    на фрезерование идут не все детали.
    """
    m.set_measure("frezerovanie", "unique_milled")
    _detail(st, "D1", grooves=2)
    _detail(st, "D2", grooves=0)
    _detail(st, "D3", grooves=1)
    assert m.value_for_order("frezerovanie", 8952) == 2.0


def test_sr26д_сборка_считает_изделия(st, m):
    m.set_measure("sborka", PRODUCTS)
    _detail(st, "D1", product="ШК-01")
    _detail(st, "D2", product="ШК-01")
    _detail(st, "D3", product="ТМБ-04")
    assert m.value_for_order("sborka", 8952) == 2.0


def test_sr26д_пустое_обозначение_изделия_названо_прямо(st, m):
    """
    Деталь без изделия не собирается. Если поле не заполнено ни у одной
    детали — это не ноль изделий, а невычислимость, и её надо назвать.
    """
    m.set_measure("sborka", PRODUCTS)
    _detail(st, "D1", product="")
    with pytest.raises(MeasureNotComputable) as e:
        m.value_for_order("sborka", 8952)
    assert "Обозначение изделия" in str(e.value)


def test_sr26е_склад_считает_уникальные_детали(st, m):
    m.set_measure("sklad", UNIQUE_DETAILS)
    _detail(st, "D1", qty=5)
    _detail(st, "D2", qty=3)
    assert m.value_for_order("sklad", 8952) == 2.0


def test_присадка_считает_экземпляры_с_отверстиями(st, m):
    m.set_measure("prisadka", DETAIL_ITEMS)
    _detail(st, "D1", qty=4, drill=6)
    _detail(st, "D2", qty=10, drill=0)
    assert m.value_for_order("prisadka", 8952) == 4.0


def test_раскрой_считает_листы_по_номерам_плит(st, m):
    m.set_measure("raskroy", SHEETS)
    _detail(st, "D1", plate=1)
    _detail(st, "D2", plate=1)
    _detail(st, "D3", plate=2)
    assert m.value_for_order("raskroy", 8952) == 2.0


def test_незагруженная_спецификация_названа_прямо(st, m):
    m.set_measure("kromlenie", EDGE_METERS)
    with pytest.raises(MeasureNotComputable) as e:
        m.value_for_order("kromlenie", 9999)
    assert "не загружена" in str(e.value)
