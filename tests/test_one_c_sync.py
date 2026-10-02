# -*- coding: utf-8 -*-
"""
Обмен с 1С: три вида сообщений, застрявшая очередь, расхождение за месяц.

ПОЧЕМУ ЗДЕСЬ МНОГО ОПЫТОВ НА ИСПОРЧЕННОМ, А НЕ НА ИСПРАВНОМ

    Требования SR-46 и SR-48 говорят о поведении системы, когда 1С
    НЕДОСТУПНА. Такое требование нельзя проверить на работающей 1С:
    зелёный прогон на исправном подтверждает только исправное. Поэтому
    ниже 1С намеренно «ложится», очередь намеренно застревает, а порог
    2 % проверяется с обеих сторон — и выше, и ниже.

    Урок записан находкой CF-397 и повторён трижды: проверка, объявленная
    работающей, подтверждается опытом с намеренно испорченным состоянием.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta

import pytest

from src.one_c_sync import (
    DISCREPANCY_THRESHOLD,
    MESSAGE_KINDS,
    MSG_OPERATION_STATUS,
    MSG_ORDER_DONE,
    MSG_REJECTION,
    OneCSync,
    ReconcileReport,
    RouteNotKnown,
    UnknownMessageKind,
)
from src.order_registration import OrderRegistration
from src.storage import Storage

ОБЛАСТИ = ("raskroy", "kromlenie", "sklad")


@pytest.fixture()
def st():
    with tempfile.TemporaryDirectory() as d:
        s = Storage(f"{d}/psr.db")
        s.upsert_order_link(7709, "ПС00-007709", "2026-09-21",
                            client_name="Тестовый заказчик")
        yield s
        s.close()


@pytest.fixture()
def sync(st):
    return OneCSync(st)


def _закрыть(st, order, area, closed_at=None):
    """Закрытие заказа на участке — та же дорога, что в рабочем месте."""
    reg = OrderRegistration(st, enqueue_to_1c=True)
    reg.open_order(order, area, "ИСП-0001")
    return reg.close_order(order, area, "ИСП-0001",
                           event_at=closed_at, mark_at=closed_at)


# ----------------------------------------------------------------------
# SR-45. Ровно три вида сообщений
# ----------------------------------------------------------------------
def test_видов_сообщений_ровно_три():
    """
    «Ровно три» — не «не менее трёх». Перечень закрыт, и тест держит
    именно закрытость: добавление четвёртого вида обязано сломать его,
    а не пройти незамеченным.
    """
    assert MESSAGE_KINDS == (MSG_OPERATION_STATUS, MSG_ORDER_DONE, MSG_REJECTION)
    assert len(set(MESSAGE_KINDS)) == 3


def test_четвёртый_вид_отвергается(sync):
    with pytest.raises(UnknownMessageKind) as e:
        sync.enqueue("order-cancelled", "7709|x", {})
    assert "SR-45" in str(e.value)


def test_закрытие_участка_кладётся_видом_статус_операции(st, sync):
    """Первый из трёх видов ставится существующей дорогой закрытия."""
    _закрыть(st, 7709, "raskroy")
    строки = st.get_pending_statuses(10)
    assert len(строки) == 1
    assert json.loads(строки[0]["payload"])["kind"] == MSG_OPERATION_STATUS


def test_завершение_заказа_только_когда_закрыт_весь_маршрут(st, sync):
    """
    Главный опыт вида «завершение заказа»: он проверяет МОЛЧАНИЕ.
    Сообщение «заказ выполнен» при незакрытом участке — ложь, которую
    1С примет за правду, и обнаружится она у клиента.
    """
    _закрыть(st, 7709, "raskroy")
    assert sync.enqueue_order_done(7709, ОБЛАСТИ) is None      # рано

    _закрыть(st, 7709, "kromlenie")
    assert sync.enqueue_order_done(7709, ОБЛАСТИ) is None      # всё ещё рано

    _закрыть(st, 7709, "sklad")
    assert sync.enqueue_order_done(7709, ОБЛАСТИ) is not None  # теперь можно

    виды = [json.loads(r["payload"])["kind"] for r in st.get_pending_statuses(50)]
    assert виды.count(MSG_ORDER_DONE) == 1


def test_маршрут_не_домысливается(sync):
    """
    Пустой маршрут — не «маршрут из нуля участков», а незнание. Модуль
    обязан отказать, а не решить, что заказ выполнен: маршрут приходит
    из выгрузки 1С, формат которой выясняется вопросом OQ-114.
    """
    with pytest.raises(RouteNotKnown) as e:
        sync.enqueue_order_done(7709, [])
    assert "OQ-114" in str(e.value)


def test_отклонение_без_причины_не_отправляется(sync):
    with pytest.raises(ValueError) as e:
        sync.enqueue_rejection(7709, "raskroy", "   ")
    assert "потери сообщения" in str(e.value)


def test_отклонение_несёт_причину_и_участок(st, sync):
    sync.enqueue_rejection(7709, "raskroy", "Лист повреждён при подаче",
                           person_code="ИСП-0004")
    payload = json.loads(st.get_pending_statuses(10)[0]["payload"])
    assert payload["kind"] == MSG_REJECTION
    assert payload["reason"] == "Лист повреждён при подаче"
    assert payload["area_id"] == "raskroy"
    assert payload["person_code"] == "ИСП-0004"


# ----------------------------------------------------------------------
# SR-46, SR-48. 1С лежит — цех работает
# ----------------------------------------------------------------------
def test_опыт_остановка_1с_на_смену_цех_не_останавливается(st, sync):
    """
    Контрольный опыт SR-46 из §24: «Остановка 1С на смену → очередь
    сохранена; цех не остановился; доставка состоялась».

    1С здесь не поднимается вовсе — доставки не происходит ни одной.
    Проверяется, что регистрация при этом ПРОШЛА: сессии закрыты,
    длительность посчитана. Если бы закрытие зависело от ответа 1С,
    падал бы именно этот тест, а не отчётность.
    """
    for area in ОБЛАСТИ:
        s = _закрыть(st, 7709, area)
        assert s.closed_at and s.duration_minutes is not None

    отчёт = sync.stuck_queue()
    assert отчёт.stuck == 3
    assert отчёт.by_kind == {MSG_OPERATION_STATUS: 3}
    assert st.count_delivered_statuses() == 0        # ни одна не ушла


def test_опыт_доставка_состоялась_после_восстановления(st, sync):
    """Вторая половина того же опыта: 1С поднялась — очередь ушла."""
    for area in ОБЛАСТИ:
        _закрыть(st, 7709, area)
    for r in st.get_pending_statuses(50):
        st.mark_status_sent(r["id"], delivered=True, http_status=200)

    assert sync.stuck_queue().stuck == 0
    assert st.count_delivered_statuses() == 3


def test_тревога_по_сроку_а_не_по_числу(st, sync):
    """
    Сто записей за час — это работавший цех. Одна запись суточной
    давности — это сломанная доставка. Тревога обязана различать.
    """
    _закрыть(st, 7709, "raskroy")
    свежий = sync.stuck_queue()
    assert свежий.stuck == 1 and not свежий.is_alarm

    завтра = (datetime.now() + timedelta(hours=25)).isoformat(timespec="seconds")
    сутки = sync.stuck_queue(now=завтра)
    assert сутки.is_alarm
    assert "SR-48" in сутки.as_text()
    assert "руководителю производства" in сутки.as_text()   # BR-81: адресат назван
    assert "цех при этом не останавливался" in сутки.as_text().lower()


def test_отчёт_называет_причину_отказа(st, sync):
    """«Очередь не уходит» без причины — сообщение, по которому нельзя действовать."""
    _закрыть(st, 7709, "raskroy")
    r = st.get_pending_statuses(10)[0]
    st.mark_status_sent(r["id"], delivered=False, http_status=503,
                        message="сервис 1С недоступен")
    assert "сервис 1С недоступен" in sync.stuck_queue().as_text()


def test_пустая_очередь_говорит_прямо(sync):
    assert "пуста" in sync.stuck_queue().as_text()


# ----------------------------------------------------------------------
# SR-49. Расхождение за месяц и порог 2 %
# ----------------------------------------------------------------------
def test_единица_сравнения_закрытые_операции_а_не_заказы(st, sync):
    """
    Требование называет единицу: закрытые ОПЕРАЦИИ. Один заказ на трёх
    участках — это три операции, а не одна. Подмена единицы даёт
    расхождение втрое и проходит незамеченной.
    """
    месяц = datetime.now().strftime("%Y-%m")
    for area in ОБЛАСТИ:
        _закрыть(st, 7709, area)
    assert sync.closed_operations(месяц) == 3


def test_опыт_порог_с_обеих_сторон():
    """
    Порог проверяется и ниже, и выше. Опыт с одной стороны показывает
    лишь, что число сравнивается, но не что сравнение верное.
    """
    ниже = ReconcileReport("2026-10", ours=1005, theirs=1000)   # 0,5 %
    assert ниже.may_switch_off and ниже.rate == pytest.approx(0.005)

    выше = ReconcileReport("2026-10", ours=1050, theirs=1000)   # 5 %
    assert not выше.may_switch_off

    ровно = ReconcileReport("2026-10", ours=1020, theirs=1000)  # ровно 2 %
    assert not ровно.may_switch_off, "порог строгий: 2 % — уже не «меньше 2 %»"


def test_расхождение_считается_от_числа_1с():
    """База — число 1С: старая регистрация пока считается верной."""
    r = ReconcileReport("2026-10", ours=90, theirs=100)
    assert r.diff == -10 and r.rate == pytest.approx(0.10)


def test_ноль_в_1с_не_выдаётся_за_согласие():
    """
    Самый опасный случай: в 1С ноль операций. Деление дало бы ошибку
    либо ноль процентов — и порог был бы пройден молча, при полном
    отсутствии базы сравнения.
    """
    r = ReconcileReport("2026-10", ours=500, theirs=0)
    assert r.rate == float("inf")
    assert not r.may_switch_off
    assert "выключать нельзя" in r.as_text()

    пусто = ReconcileReport("2026-10", ours=0, theirs=0)
    assert пусто.rate == 0.0


def test_порог_взят_из_решения_а_не_из_кода():
    """Критерий приёмки виден в одном месте и меняется решением (D-163)."""
    assert DISCREPANCY_THRESHOLD == 0.02
    assert ReconcileReport("2026-10", 0, 0).threshold == DISCREPANCY_THRESHOLD


def test_сверка_берёт_наше_число_из_базы(st, sync):
    месяц = datetime.now().strftime("%Y-%m")
    for area in ОБЛАСТИ:
        _закрыть(st, 7709, area)
    r = sync.reconcile(месяц, theirs=3)
    assert r.ours == 3 and r.may_switch_off
    assert "можно выключать" in r.as_text()
