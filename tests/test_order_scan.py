"""
Тесты фиксации по коду с бланка заказа (шаг 1 ПСР).

Код печатает 1С на форме «Заказ клиента» (SETUP-002 §7а):
«ПС00-010109|2026-08-28» — номер документа и дата.

SR-108: заказ ищется в пределах сменного задания, а не по всему потоку.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.order_scan import (
    OrderNotInShiftTask,
    OrderScanResolver,
    ScanNotRecognized,
    parse_order_code,
)
from src.storage import Storage


@pytest.fixture()
def st():
    s = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    s.upsert_order_link(8952, "confirmed", order_full_num="ПС00-010109",
                        order_date="2026-08-28")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Разбор кода
# ----------------------------------------------------------------------
def test_разбирает_номер_и_дату():
    o = parse_order_code("ПС00-010109|2026-08-28")
    assert o.doc_num == "ПС00-010109"
    assert o.doc_date == "2026-08-28"
    assert o.key == "ПС00-010109|2026-08-28"


def test_разделитель_не_обязан_быть_вертикальной_чертой():
    """Разделитель оставлен на усмотрение исполнителя 1С (SETUP-002 §7а)."""
    for raw in ("ПС00-010109;2026-08-28", "ПС00-010109,2026-08-28",
                "ПС00-010109\t2026-08-28"):
        assert parse_order_code(raw).doc_num == "ПС00-010109"


def test_принимает_разные_префиксы_документов():
    """В выгрузке встречаются ПС00, ЛД00, 0Ч00 — все допустимы."""
    for num in ("ПС00-010109", "ЛД00-012594", "0Ч00-003266"):
        assert parse_order_code(f"{num}|2026-08-28").doc_num == num


def test_отказ_объясняет_что_не_так():
    """
    Оператор у станка должен понять из экрана, что он отсканировал
    не то, а не увидеть слово «ошибка».
    """
    with pytest.raises(ScanNotRecognized) as e:
        parse_order_code("8952")
    assert "ожидалась пара" in str(e.value)

    with pytest.raises(ScanNotRecognized) as e:
        parse_order_code("ПС00-010109|28.08.2026")
    assert "ГГГГ-ММ-ДД" in str(e.value)

    with pytest.raises(ScanNotRecognized) as e:
        parse_order_code("")
    assert "пустой" in str(e.value)


# ----------------------------------------------------------------------
# Связка с заказом БАЗИС
# ----------------------------------------------------------------------
def test_находит_заказ_базис_по_бланку(st):
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie") == 8952


def test_несвязанный_документ_назван_прямо(st):
    r = OrderScanResolver(st)
    with pytest.raises(ScanNotRecognized) as e:
        r.resolve("ПС00-999999|2026-08-28", "kromlenie")
    assert "не связан ни с одним заказом" in str(e.value)


def test_дата_различает_одинаковые_номера_разных_лет(st):
    """
    Номера документов 1С повторяются между годами. Один номер без даты
    адресует несколько заказов — поэтому ключ парный.
    """
    st.upsert_order_link(7001, "confirmed", order_full_num="ПС00-010109",
                         order_date="2025-08-28")
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie") == 8952
    assert r.resolve("ПС00-010109|2025-08-28", "kromlenie") == 7001


# ----------------------------------------------------------------------
# SR-108. Поиск в пределах сменного задания
# ----------------------------------------------------------------------
def test_sr108_заказ_вне_сменного_задания_отклоняется(st):
    r = OrderScanResolver(st)
    with pytest.raises(OrderNotInShiftTask) as e:
        r.resolve("ПС00-010109|2026-08-28", "kromlenie", shift_task=[8953, 8954])
    assert "сменном задании" in str(e.value)


def test_sr108_заказ_из_сменного_задания_принимается(st):
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie",
                     shift_task=[8952, 8953]) == 8952


def test_без_сменного_задания_поиск_идёт_по_всей_связке(st):
    """
    Пока выгрузки сменного задания нет (пункт 0.8 шага 0), область
    поиска не ограничена — и это видно в вызове, а не спрятано.
    """
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie",
                     shift_task=None) == 8952
