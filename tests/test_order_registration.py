"""
Тесты позаказной регистрации (шаг 1 ПСР).

Каждый тест назван требованием и проверяет ИМЕННО ТОТ способ проверки,
который задан в SRS-001 §22, а не то, что удобно проверить:

    SR-20   «Переход оператора на другой заказ» →
            требуется подтверждение; событие открытия создано
    SR-20а  «Уход заказа с кромления» → событие закрытия создано
    SR-11а  «Событие, отмеченное задним числом» → момент окончания
            и момент постановки отметки различны и ОБА сохранены
    SR-13   «Отметка не в момент выполнения» → признак ретроспективности
            установлен
    SR-15   «Отметка без сети и последующее восстановление» → признак
            отложенной отправки установлен; момент события и момент
            приёма различны
    SR-115  «Закрыть заказ дважды» → второе закрытие отклонено;
            выработка участка не изменилась
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.area_measures import EDGE_METERS, AreaMeasures
from src.order_registration import (
    OrderAlreadyClosed,
    OrderNotOpen,
    OrderRegistration,
    is_retro,
)
from src.storage import Storage


@pytest.fixture()
def reg():
    """Регистрация БЕЗ справочника измерителей: участок, где технолог
    ещё не заполнил пункт 0.3 шага 0."""
    st = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    yield OrderRegistration(st)
    st.close()


@pytest.fixture()
def reg_with_measures():
    """Регистрация со справочником: измеритель берётся из него."""
    st = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    m = AreaMeasures(st)
    m.set_measure("kromlenie", EDGE_METERS)
    st.upsert_detail(dict(detail_uid="D1", order_num=8952, qr_code="D1",
                          qty=2, edge_total_len=1500.0))
    yield OrderRegistration(st, measures=m)
    st.close()


# ----------------------------------------------------------------------
# SR-20. Открытие заказа на участке
# ----------------------------------------------------------------------
def test_sr20_открытие_создаёт_событие(reg):
    s = reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    assert s.is_open
    assert s.opened_at == "2026-09-14T08:00:00"
    assert s.operator_id == "OP-01"
    assert reg.get_session(s.session_id) is not None


def test_sr20_переход_на_другой_заказ_не_закрывает_прежний(reg):
    """
    Открытие нового заказа НЕ закрывает предыдущий молча.

    Иначе длительность работы над первым заказом оказалась бы вымышленной:
    система назначила бы ей окончание, которого никто не наблюдал.
    """
    a = reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    b = reg.open_order(8953, "kromlenie", "OP-01", event_at="2026-09-14T08:40:00")
    assert reg.get_session(a.session_id).is_open
    assert reg.get_session(b.session_id).is_open
    assert len(reg.open_sessions("kromlenie")) == 2


# ----------------------------------------------------------------------
# SR-20а. Закрытие заказа на участке
# ----------------------------------------------------------------------
def test_sr20а_закрытие_создаёт_событие_и_длительность(reg):
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:30:00")
    assert not s.is_open
    assert s.closed_at == "2026-09-14T09:30:00"
    assert s.duration_minutes == 90.0


def test_закрытие_без_открытия_отклоняется(reg):
    """Закрытие без открытия не даёт длительности — отметка бессмысленна."""
    with pytest.raises(OrderNotOpen):
        reg.close_order(8952, "kromlenie")


def test_sr26а_измеритель_берётся_из_справочника_а_не_из_параметра(
        reg_with_measures):
    """
    Измеритель участка НЕ передаётся вызывающим кодом: он задан
    справочником (SR-26а) и вычислен из спецификации (SR-26б).
    2 × 1500 мм = 3 000 мм = 3,0 пог. м.
    """
    r = reg_with_measures
    r.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = r.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    assert s.measure_name == "пог. м кромки"
    assert s.measure_value == 3.0


def test_незаполненный_справочник_не_ломает_регистрацию(reg):
    """
    Пока технолог не заполнил пункт 0.3, отметка всё равно ставится:
    измеритель остаётся пустым, а причина пишется в примечание.
    Пустое поле честнее выдуманного числа.
    """
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    assert s.measure_value is None
    assert s.duration_minutes == 60.0


# ----------------------------------------------------------------------
# SR-11а. Момент окончания и момент отметки — разные поля
# ----------------------------------------------------------------------
def test_sr11а_оба_момента_сохранены(reg):
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie",
                        event_at="2026-09-14T17:00:00",
                        mark_at="2026-09-15T09:00:00")
    assert s.closed_at == "2026-09-14T17:00:00"
    assert s.closed_mark_at == "2026-09-15T09:00:00"
    assert s.closed_at != s.closed_mark_at


def test_sr11а_отметка_задним_числом_не_меняет_длительность(reg):
    """
    Длительность считается по моментам СОБЫТИЙ. Отметка, поставленная
    на следующий день, не должна удлинять работу на сутки.
    """
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie",
                        event_at="2026-09-14T09:00:00",
                        mark_at="2026-09-15T09:00:00")
    assert s.duration_minutes == 60.0


# ----------------------------------------------------------------------
# SR-13. Признак ретроспективности
# ----------------------------------------------------------------------
def test_sr13_признак_установлен_при_отметке_на_следующий_день(reg):
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie",
                        event_at="2026-09-14T17:00:00",
                        mark_at="2026-09-15T09:00:00")
    assert s.retro_close is True


def test_sr13_признак_не_установлен_при_отметке_в_тот_же_день(reg):
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie",
                        event_at="2026-09-14T17:00:00",
                        mark_at="2026-09-14T17:05:00")
    assert s.retro_close is False


def test_sr13_признак_есть_отрицание_критерия_sr40():
    """
    Критерий приёмки SR-40 — «доля отметок в день выполнения».
    Признак ретроспективности обязан быть его точным отрицанием, иначе
    доля и признак разойдутся на одних и тех же данных.

    Сравниваются ДАТЫ, а не моменты: отметка через восемь часов в тот же
    день — не ретроспективная, отметка через час, но за полночь, —
    ретроспективная.
    """
    assert is_retro("2026-09-14T08:00:00", "2026-09-14T16:00:00") is False
    assert is_retro("2026-09-14T23:30:00", "2026-09-15T00:30:00") is True


# ----------------------------------------------------------------------
# SR-15. Признак отложенной отправки
# ----------------------------------------------------------------------
def test_sr15_признак_и_момент_приёма(reg):
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie",
                        event_at="2026-09-14T12:00:00",
                        deferred=True,
                        received_at="2026-09-14T15:20:00")
    assert s.deferred is True
    assert s.received_at == "2026-09-14T15:20:00"
    assert s.closed_at != s.received_at


def test_sr15_без_сети_момент_события_сохраняется(reg):
    """
    Отметка, накопленная на устройстве, приходит позже — но моментом
    события остаётся тот, когда работа действительно закончилась.
    Иначе отказ сети превратился бы в производственную задержку.
    """
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    s = reg.close_order(8952, "kromlenie",
                        event_at="2026-09-14T12:00:00",
                        deferred=True,
                        received_at="2026-09-14T18:00:00")
    assert s.duration_minutes == 240.0


# ----------------------------------------------------------------------
# SR-115. Отказ в повторном закрытии
# ----------------------------------------------------------------------
def test_sr115_второе_закрытие_отклонено(reg):
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    with pytest.raises(OrderAlreadyClosed):
        reg.close_order(8952, "kromlenie", event_at="2026-09-14T10:00:00")


def test_sr115_выработка_участка_не_изменилась(reg_with_measures):
    """
    Способ проверки SRS §22 дословно: «второе закрытие отклонено;
    выработка участка не изменилась». Проверяем именно второе — что
    измеритель не удвоился.
    """
    reg = reg_with_measures
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    before = sum(s.measure_value or 0 for s in reg.order_history(8952))
    with pytest.raises(OrderAlreadyClosed):
        reg.close_order(8952, "kromlenie", event_at="2026-09-14T10:00:00")
    after = sum(s.measure_value or 0 for s in reg.order_history(8952))
    assert before == after == 3.0


def test_sr115_другой_участок_закрывается_свободно(reg):
    """Отказ относится к паре «заказ + участок», а не к заказу целиком."""
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    reg.open_order(8952, "sborka", "OP-02", event_at="2026-09-14T14:00:00")
    s = reg.close_order(8952, "sborka", event_at="2026-09-14T16:00:00")
    assert s.duration_minutes == 120.0


# ----------------------------------------------------------------------
# Основа шага 2: карта потока
# ----------------------------------------------------------------------
def test_межучастковое_ожидание_вычислимо_из_истории(reg):
    """
    Шаг 2 вычисляет межучастковое ожидание как «открытие на следующем
    участке минус закрытие на предыдущем». Этот тест показывает, что
    позаказных отметок для этого достаточно — подетальных не требуется.
    """
    reg.open_order(8952, "raskroy", "OP-01", event_at="2026-09-14T08:00:00")
    reg.close_order(8952, "raskroy", event_at="2026-09-14T10:00:00")
    reg.open_order(8952, "kromlenie", "OP-02", event_at="2026-09-15T09:00:00")
    reg.close_order(8952, "kromlenie", event_at="2026-09-15T11:00:00")

    h = reg.order_history(8952)
    assert [s.area_id for s in h] == ["raskroy", "kromlenie"]

    from src.order_registration import _parse
    wait = (_parse(h[1].opened_at) - _parse(h[0].closed_at)).total_seconds() / 3600
    assert wait == 23.0


# ----------------------------------------------------------------------
# Отправка в 1С: той же очередью, что подетальный учёт
# ----------------------------------------------------------------------
def test_в_1с_уходит_только_закрытие(reg):
    """
    Перехват точки ввода: ввод один, потребителей два. Открытие заказа —
    внутреннее событие системы, 1С его не принимает.
    """
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    assert reg.st.get_pending_statuses(10) == []

    reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    pending = reg.st.get_pending_statuses(10)
    assert len(pending) == 1
    assert pending[0]["op_key"] == "8952|kromlenie|order-close"


def test_в_1с_уходит_момент_события_а_не_отметки(reg):
    """
    SR-11а: 1С должна знать, КОГДА работа закончилась, а не когда о ней
    сообщили. Иначе отметка задним числом сдвинет производственную дату.
    """
    import json

    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    reg.close_order(8952, "kromlenie",
                    event_at="2026-09-14T17:00:00",
                    mark_at="2026-09-15T09:00:00")
    payload = json.loads(reg.st.get_pending_statuses(10)[0]["payload"])
    assert payload["completed_at"] == "2026-09-14T17:00:00"
    assert payload["marked_at"] == "2026-09-15T09:00:00"
    assert payload["retro"] is True


def test_повторное_закрытие_не_создаёт_второй_записи_в_очереди(reg):
    """SR-115 на уровне 1С: отклонённое закрытие ничего не отправляет."""
    reg.open_order(8952, "kromlenie", "OP-01", event_at="2026-09-14T08:00:00")
    reg.close_order(8952, "kromlenie", event_at="2026-09-14T09:00:00")
    with pytest.raises(OrderAlreadyClosed):
        reg.close_order(8952, "kromlenie", event_at="2026-09-14T10:00:00")
    assert len(reg.st.get_pending_statuses(10)) == 1
