"""
Тесты натуральных измерителей участков (SR-26а — SR-26е)
в редакции решения D-210 от 19.09.2026.

Способы проверки взяты из SRS-001 §22 дословно:
    SR-26а  «Справочник участков» → у каждого из шести участков
            ровно один измеритель
    SR-26в  измеритель присадки — детали со сверлением (экземпляры)
    SR-26г  измеритель фрезерования — детали, ПРОХОДЯЩИЕ фрезерование
            (экземпляры)
    SR-26д  «Измеритель сборки в справочнике» → детали заказа
    SR-26е  «Измеритель склада в справочнике» → детали заказа

SR-26б проверяется отдельно и по существу: значение вычисляется из
спецификации и НЕ ЗАВИСИТ от отметок оператора.

ЧТО ИЗМЕНИЛО РЕШЕНИЕ D-210
    Четыре измерителя из шести. Общее правило владельца: работа участка
    меряется тем, что через него физически проходит. Деталь подаётся в
    станок по одной независимо от уникальности — значит экземпляры, а
    не различные позиции спецификации.

    Раскрой: «карта раскроя» → «листы» (в карте может быть и 1 лист,
    и 35 — величина ненормированная).
    Фрезерование, склад: уникальные детали → экземпляры.
    Сборка: изделие → детали (тумба из 6 и кухня из 200 не могут
    нормироваться одинаково).
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.area_measures import (
    DETAIL_ITEMS,
    DRILLED_ITEMS,
    EDGE_METERS,
    MILLED_ITEMS,
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
            product="", plate=0, src="Кроношпан.xbir"):
    st.upsert_detail(dict(detail_uid=uid, order_num=order, qr_code=uid,
                          qty=qty, edge_total_len=edge, grooves=grooves,
                          drill_total=drill, product_code=product,
                          plate_no=plate, source_file=src))


# ----------------------------------------------------------------------
# SR-26а. Ровно один измеритель на участок
# ----------------------------------------------------------------------
def test_sr26а_повторное_задание_заменяет_а_не_добавляет(m):
    m.set_measure("kromlenie", EDGE_METERS)
    m.set_measure("kromlenie", DETAIL_ITEMS)
    assert len(m.all_measures()) == 1
    assert m.get_measure("kromlenie").measure_code == DETAIL_ITEMS


def test_sr26а_шесть_участков_шесть_измерителей(m):
    for a, code in zip(AREAS, [SHEETS, EDGE_METERS, MILLED_ITEMS,
                               DRILLED_ITEMS, DETAIL_ITEMS, DETAIL_ITEMS]):
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
    m.set_measure("sborka", DETAIL_ITEMS)
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
    на фрезерование идут не все детали. Отбор по пазам решение D-210
    сохранило — снята только уникальность.
    """
    m.set_measure("frezerovanie", MILLED_ITEMS)
    _detail(st, "D1", qty=1, grooves=2)
    _detail(st, "D2", qty=9, grooves=0)
    _detail(st, "D3", qty=1, grooves=1)
    assert m.value_for_order("frezerovanie", 8952) == 2.0


def test_sr26г_фрезерование_считает_экземпляры_а_не_позиции(st, m):
    """
    Решение D-210: деталь подаётся в станок по одной независимо от
    уникальности. Двадцать одинаковых фасадов — двадцать подач.

    Прежняя редакция дала бы здесь 2 (две позиции спецификации).
    """
    m.set_measure("frezerovanie", MILLED_ITEMS)
    _detail(st, "D1", qty=20, grooves=2)
    _detail(st, "D2", qty=3, grooves=1)
    assert m.value_for_order("frezerovanie", 8952) == 23.0


def test_sr26д_сборка_считает_детали_а_не_изделия(st, m):
    """
    Довод владельца за смену измерителя: тумба из шести деталей и кухня
    из двухсот не могут нормироваться одинаково, а «изделие» их
    уравнивало.
    """
    m.set_measure("sborka", DETAIL_ITEMS)
    _detail(st, "D1", qty=4, product="ШК-01")
    _detail(st, "D2", qty=2, product="ШК-01")
    assert m.value_for_order("sborka", 8952) == 6.0


def test_sr26д_сборка_не_зависит_от_обозначения_изделия(st, m):
    """
    Главное следствие D-210 для сборки: измеритель считается и тогда,
    когда «Обозначение изделия» не заполнено. А оно в реальной выгрузке
    БАЗИС не заполнено НИ РАЗУ — 0 из 4 736 строк (CF-419).

    Прежняя редакция на этих же данных поднимала MeasureNotComputable,
    то есть участок сборки не мерялся вовсе.
    """
    m.set_measure("sborka", DETAIL_ITEMS)
    _detail(st, "D1", qty=7, product="")
    assert m.value_for_order("sborka", 8952) == 7.0


def test_sr26е_склад_считает_экземпляры(st, m):
    """
    Укладывается каждый экземпляр, а не позиция спецификации: пять
    одинаковых полок кладутся пять раз.

    Прежняя редакция дала бы здесь 2.
    """
    m.set_measure("sklad", DETAIL_ITEMS)
    _detail(st, "D1", qty=5)
    _detail(st, "D2", qty=3)
    assert m.value_for_order("sklad", 8952) == 8.0


def test_sr26в_присадка_считает_экземпляры_с_отверстиями(st, m):
    m.set_measure("prisadka", DRILLED_ITEMS)
    _detail(st, "D1", qty=4, drill=6)
    _detail(st, "D2", qty=10, drill=0)
    assert m.value_for_order("prisadka", 8952) == 4.0


def test_раскрой_считает_листы_по_номерам_плит(st, m):
    m.set_measure("raskroy", SHEETS)
    _detail(st, "D1", plate=1)
    _detail(st, "D2", plate=1)
    _detail(st, "D3", plate=2)
    assert m.value_for_order("raskroy", 8952) == 2.0


def test_раскрой_плиты_разных_материалов_не_сливаются(st, m):
    """
    CF-424. Заказ режется из нескольких материалов, и на каждый БАЗИС
    выгружает свой .xbir, где нумерация плит начинается заново. Плита
    №1 «Кроношпана» и плита №1 «Ореха» — два разных физических листа.

    Первая редакция считала различные номера плиты по всему заказу и
    давала здесь 2 вместо 4. На выгрузке (238 файлов, 68 заказов) это
    занижало счёт более чем вдвое — 194 листа вместо 404, — причём во
    ВСЕХ 57 заказах, выгруженных несколькими файлами.
    """
    m.set_measure("raskroy", SHEETS)
    _detail(st, "D1", plate=1, src="Кроношпан.xbir")
    _detail(st, "D2", plate=2, src="Кроношпан.xbir")
    _detail(st, "D3", plate=1, src="Орех-Кария.xbir")
    _detail(st, "D4", plate=2, src="Орех-Кария.xbir")
    assert m.value_for_order("raskroy", 8952) == 4.0


def test_отменённый_измеритель_отличим_от_неизвестного(st, m):
    """
    У технолога в справочнике может остаться запись, сделанная до
    D-210. Сообщение обязано сказать, что измеритель ОТМЕНЁН и почему,
    иначе он пойдёт искать дефект в коде.
    """
    m.set_measure("sborka", PRODUCTS)
    _detail(st, "D1", qty=3)
    with pytest.raises(MeasureNotComputable) as e:
        m.value_for_order("sborka", 8952)
    assert "D-210" in str(e.value)

    m.set_measure("sklad", UNIQUE_DETAILS)
    with pytest.raises(MeasureNotComputable) as e:
        m.value_for_order("sklad", 8952)
    assert "D-210" in str(e.value)


def test_незагруженная_спецификация_названа_прямо(st, m):
    m.set_measure("kromlenie", EDGE_METERS)
    with pytest.raises(MeasureNotComputable) as e:
        m.value_for_order("kromlenie", 9999)
    assert "не загружена" in str(e.value)


# ----------------------------------------------------------------------
# Измерители, занижающие выработку (D-236)
# ----------------------------------------------------------------------
def test_занижающих_ровно_два_и_они_названы():
    """
    Перечень ЯВНЫЙ, как у отменённых измерителей. Заниженная величина
    выглядит обычным числом — отличить её можно только по списку.
    """
    from src.area_measures import ЗАНИЖАЮЩИЕ, DRILLED_ITEMS, MILLED_ITEMS
    assert set(ЗАНИЖАЮЩИЕ) == {MILLED_ITEMS, DRILLED_ITEMS}
    for почему in ЗАНИЖАЮЩИЕ.values():
        assert "OQ-130" in почему and "D-236" in почему


def test_норматив_по_занижающему_измерителю_не_считается():
    """
    Главный опыт решения D-236. Удельный норматив делит время на
    выработку: заниженная выработка завысит время на единицу, и
    подсветка отклонений шага 2 сработает не там. Норматив, посчитанный
    так, выглядит обычным числом — ошибку в нём обнаружить нечем,
    поэтому отказ ставится ДО расчёта.
    """
    from src.area_measures import (DRILLED_ITEMS, MILLED_ITEMS, SHEETS,
                                   ИзмерительЗанижает, assert_usable_for_norms)
    for м in (MILLED_ITEMS, DRILLED_ITEMS):
        with pytest.raises(ИзмерительЗанижает) as e:
            assert_usable_for_norms(м)
        assert "норматива не годится" in str(e.value)
    assert_usable_for_norms(SHEETS)        # лист не занижает — проходит


def test_счёт_по_занижающему_измерителю_не_запрещён(st):
    """
    Отказываться считать вовсе было бы неверно: значение по полю — не
    мусор, а нижняя граница. Деталь с пазами фрезерование проходит
    наверняка. Отказ погасил бы экран оператора на двух участках из
    шести без нужды.
    """
    from src.area_measures import AreaMeasures, MILLED_ITEMS
    _detail(st, "D1", order=8952, qty=3, grooves=2)   # с пазами — считается
    _detail(st, "D2", order=8952, qty=5, grooves=0)   # без пазов — нет
    m = AreaMeasures(st)
    m.set_measure("frezerovanie", MILLED_ITEMS)
    assert m.value_for_order("frezerovanie", 8952) == 3.0


def test_экран_узнаёт_что_величина_нижняя_граница(st):
    """
    Число, про которое известно, что оно занижено, обязано показываться
    как «не менее N». Иначе человек сравнит его с планом и примет
    решение по неполным данным, не узнав, что данные неполны.
    """
    from src.area_measures import AreaMeasures, MILLED_ITEMS, SHEETS
    m = AreaMeasures(st)
    m.set_measure("frezerovanie", MILLED_ITEMS)
    m.set_measure("raskroy", SHEETS)
    assert m.is_lower_bound("frezerovanie") is True
    assert m.is_lower_bound("raskroy") is False
    assert "OQ-130" in m.lower_bound_reason("frezerovanie")
    assert m.lower_bound_reason("raskroy") == ""
