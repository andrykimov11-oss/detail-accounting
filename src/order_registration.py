"""
Позаказная регистрация: открытие и закрытие заказа на участке.

ЗАЧЕМ ЭТОТ МОДУЛЬ
    Первый этап ПСР начинается не с подетального учёта, а с позаказного:
    оператор отмечает, что взял заказ в работу и что закончил его.
    Две отметки на заказ вместо скана каждой детали — около 150 действий
    в день против 1 010 — 2 293 при подетальном учёте.

    Смысл не в экономии действий. Позаказная отметка даёт то, чего сегодня
    нет ни в каком виде: длительность работы над заказом, межучастковое
    ожидание и загрузку участка. Подетальный учёт этого не добавляет —
    он уточняет предмет, а не появляется вместо.

ЧТО ЗДЕСЬ РЕАЛИЗОВАНО
    SR-20   открытие заказа на участке; оно же подтверждение перехода
            на другой заказ. Задаёт границу партии
    SR-20а  закрытие заказа на участке
    SR-11а  момент окончания и момент постановки отметки — РАЗНЫЕ поля
    SR-13   признак ретроспективности: отметка поставлена не в момент
            выполнения
    SR-15   признак отложенной отправки: отметка накоплена на устройстве
            без сети; момент события и момент приёма — разные поля
    SR-115  попытка закрыть заказ, уже закрытый на этом участке,
            отклоняется; вторая отметка не создаётся

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ
    Единица регистрации участка НЕ проверяется здесь. SR-20а говорит, что
    закрытие ставится только на участках с единицей «заказ». Но на шаге 5
    участки переходят на подетальный учёт ПО ОДНОМУ (PLAN-001 §8.2), и
    в один период сосуществуют обе единицы. Поэтому решение «ставится ли
    на этом участке позаказная отметка» принимает вызывающий код по
    справочнику участков, а не этот модуль по зашитому списку.

    Отправка в 1С — не отдельным путём, а ТОЙ ЖЕ очередью status_outbox,
    которой пользуется подетальный учёт. Ключ операции в 1С один и тот же:
    тройка «полный номер заказа + дата заказа + операция цеха». Для 1С
    разницы нет вовсе: она принимает закрытие операции, а чем оно
    получено — двумя отметками или сорока сканами — её не касается.

    Доставку ведёт one_c_writer. Очередь переживает недоступность
    сервиса и помечает запись доставленной только после ответа 1С.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


ISO = "%Y-%m-%dT%H:%M:%S"


def _fmt(dt: datetime) -> str:
    return dt.strftime(ISO)


def _parse(s: str) -> datetime:
    return datetime.strptime(s[:19], ISO)


def is_retro(event_at: str, mark_at: str) -> bool:
    """
    Признак ретроспективности (SR-13).

    Отметка ретроспективна, если поставлена НЕ В ТОТ ЖЕ ДЕНЬ, что событие.
    Сравниваются именно даты, а не моменты: критерий приёмки SR-40 — «доля
    отметок в день выполнения», и признак обязан быть его отрицанием,
    иначе доля и признак разойдутся на одних и тех же данных.
    """
    return _parse(event_at).date() != _parse(mark_at).date()


class OrderAlreadyClosed(Exception):
    """SR-115: заказ на этом участке уже закрыт; вторая отметка не создаётся."""

    def __init__(self, order_num: int, area_id: str, closed_at: str):
        self.order_num = order_num
        self.area_id = area_id
        self.closed_at = closed_at
        super().__init__(
            f"Заказ {order_num} на участке {area_id} уже закрыт {closed_at}. "
            f"Вторая отметка не создаётся (SR-115)."
        )


class OrderNotOpen(Exception):
    """Закрытие без открытия: границы партии не существует."""

    def __init__(self, order_num: int, area_id: str):
        super().__init__(
            f"Заказ {order_num} на участке {area_id} не открыт. "
            f"Закрытие без открытия не создаёт длительности (SR-20)."
        )


@dataclass
class Session:
    """Сессия работы над заказом на участке: от открытия до закрытия."""

    session_id: str
    order_num: int
    area_id: str
    operator_id: str
    opened_at: str
    opened_mark_at: str
    closed_at: Optional[str] = None
    closed_mark_at: Optional[str] = None
    retro_open: bool = False
    retro_close: bool = False
    deferred: bool = False
    received_at: Optional[str] = None
    measure_name: Optional[str] = None
    measure_value: Optional[float] = None
    note: Optional[str] = None

    @property
    def duration_minutes(self) -> Optional[float]:
        """
        Время работы над заказом на участке.

        Считается по моментам СОБЫТИЙ, а не по моментам отметок: отметка
        задним числом не должна ни удлинять, ни укорачивать работу.
        """
        if not self.closed_at:
            return None
        delta = _parse(self.closed_at) - _parse(self.opened_at)
        return delta.total_seconds() / 60.0

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class OrderRegistration:
    """
    Позаказная регистрация поверх Storage.

    Storage остаётся единственным слоем доступа к данным: этот класс
    бизнес-логику держит, SQL — нет.
    """

    def __init__(self, storage, measures=None, enqueue_to_1c: bool = True):
        """
        measures — справочник натуральных измерителей (AreaMeasures).

        Необязателен: на участках, где измеритель ещё не задан технологом
        (пункт 0.3 шага 0), регистрация всё равно должна работать. Тогда
        закрытие пишется без измерителя, и это видно как пустое поле,
        а не как выдуманное значение.
        """
        self.st = storage
        self.measures = measures
        self.enqueue_to_1c = enqueue_to_1c

    # ------------------------------------------------------------------
    # SR-20. Открытие заказа на участке
    # ------------------------------------------------------------------
    def open_order(
        self,
        order_num: int,
        area_id: str,
        operator_id: str,
        event_at: Optional[str] = None,
        mark_at: Optional[str] = None,
        deferred: bool = False,
        received_at: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Session:
        """
        Открыть заказ на участке. Оно же подтверждение перехода на другой
        заказ: открытие нового заказа тем же оператором на том же участке
        не закрывает предыдущий автоматически — предыдущий закрывает
        оператор явно, иначе длительность работы окажется вымышленной.

        event_at — момент события. mark_at — момент постановки отметки.
        Если mark_at не задан, он равен event_at: отметка в момент действия.
        """
        now = _fmt(datetime.now())
        event_at = event_at or now
        mark_at = mark_at or event_at

        s = Session(
            session_id=str(uuid.uuid4()),
            order_num=order_num,
            area_id=area_id,
            operator_id=operator_id,
            opened_at=event_at,
            opened_mark_at=mark_at,
            retro_open=is_retro(event_at, mark_at),
            deferred=deferred,
            received_at=received_at or (now if deferred else None),
            note=note,
        )
        self.st.insert_order_session(s)
        return s

    # ------------------------------------------------------------------
    # SR-20а. Закрытие заказа на участке
    # ------------------------------------------------------------------
    def close_order(
        self,
        order_num: int,
        area_id: str,
        operator_id: Optional[str] = None,
        event_at: Optional[str] = None,
        mark_at: Optional[str] = None,
        deferred: bool = False,
        received_at: Optional[str] = None,
    ) -> Session:
        """
        Закрыть заказ на участке.

        SR-115: если заказ на этом участке уже закрыт — отклоняется,
        вторая отметка не создаётся, выработка участка не меняется.
        """
        closed = self.st.get_closed_order_session(order_num, area_id)
        if closed is not None:
            raise OrderAlreadyClosed(order_num, area_id, closed["closed_at"])

        row = self.st.get_open_order_session(order_num, area_id)
        if row is None:
            raise OrderNotOpen(order_num, area_id)

        now = _fmt(datetime.now())
        event_at = event_at or now
        mark_at = mark_at or event_at

        # SR-26а и SR-26б: измеритель берётся ИЗ СПРАВОЧНИКА участка и
        # вычисляется ИЗ СПЕЦИФИКАЦИИ, а не передаётся вызывающим кодом.
        # Прежняя редакция принимала measure_value параметром — так
        # требование «ровно один измеритель на участок» не обеспечивалось
        # ничем: вызывающий мог передать для одного участка два разных.
        measure_name = measure_value = None
        if self.measures is not None:
            try:
                m = self.measures.get_measure(area_id)
                measure_name = m.measure_name
                measure_value = self.measures.value_for_order(area_id, order_num)
            except Exception as exc:
                # Незаполненный справочник либо неразобранная спецификация —
                # это состояние шага 0, а не сбой регистрации. Отметка
                # ставится, измеритель остаётся пустым, причина пишется
                # в примечание: пустое поле честнее выдуманного числа.
                self.st.append_session_note(row["session_id"], str(exc))

        self.st.close_order_session(
            session_id=row["session_id"],
            closed_at=event_at,
            closed_mark_at=mark_at,
            retro_close=1 if is_retro(event_at, mark_at) else 0,
            operator_id=operator_id or row["operator_id"],
            measure_name=measure_name,
            measure_value=measure_value,
            deferred=1 if deferred else 0,
            received_at=received_at or (now if deferred else None),
        )
        session = self.get_session(row["session_id"])
        if self.enqueue_to_1c:
            self._enqueue_closing(session)
        return session

    # ------------------------------------------------------------------
    # Отправка закрытия в 1С — той же очередью, что подетальный учёт
    # ------------------------------------------------------------------
    def _enqueue_closing(self, s: "Session") -> None:
        """
        Положить закрытие заказа в status_outbox.

        В 1С уходит ТОЛЬКО закрытие: открытие заказа — внутреннее событие
        системы, 1С его не принимает и принимать не должна. Это и есть
        перехват точки ввода: ввод один, потребителей два.
        """
        link = self.st.get_order_link(s.order_num)
        op_key = f"{s.order_num}|{s.area_id}|order-close"
        payload = {
            "order_full_num": link["order_full_num"] if link else "",
            "order_date": link["order_date"] if link else "",
            "order_num": s.order_num,
            "area_id": s.area_id,
            "operation": self.st.get_setting(
                f"area_operation.{s.area_id}", ""),
            "status": "Выполнено",
            # Момент СОБЫТИЯ, а не отметки: 1С должна знать, когда работа
            # закончилась, а не когда о ней сообщили (SR-11а).
            "completed_at": s.closed_at,
            "marked_at": s.closed_mark_at,
            "operator": s.operator_id,
            "unit": "order",
            "measure_name": s.measure_name,
            "measure_value": s.measure_value,
            "retro": s.retro_close,
            "deferred": s.deferred,
        }
        self.st.enqueue_status(op_key, payload)

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def get_session(self, session_id: str) -> Optional[Session]:
        row = self.st.get_order_session(session_id)
        return _row_to_session(row) if row else None

    def open_sessions(self, area_id: str) -> list[Session]:
        """Заказы, взятые в работу на участке и ещё не закрытые."""
        return [_row_to_session(r) for r in self.st.get_open_sessions(area_id)]

    def order_history(self, order_num: int) -> list[Session]:
        """Все сессии заказа по участкам — основа карты потока (шаг 2)."""
        return [_row_to_session(r) for r in self.st.get_order_sessions(order_num)]


def _row_to_session(r) -> Session:
    return Session(
        session_id=r["session_id"],
        order_num=r["order_num"],
        area_id=r["area_id"],
        operator_id=r["operator_id"],
        opened_at=r["opened_at"],
        opened_mark_at=r["opened_mark_at"],
        closed_at=r["closed_at"],
        closed_mark_at=r["closed_mark_at"],
        retro_open=bool(r["retro_open"]),
        retro_close=bool(r["retro_close"]),
        deferred=bool(r["deferred"]),
        received_at=r["received_at"],
        measure_name=r["measure_name"],
        measure_value=r["measure_value"],
        note=r["note"],
    )
