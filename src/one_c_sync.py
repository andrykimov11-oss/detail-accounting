# -*- coding: utf-8 -*-
"""
Обмен с 1С: три вида сообщений, застрявшая очередь, расхождение за месяц.

ТРЕБОВАНИЯ: SR-45 (ровно три вида сообщений), SR-46 (очередь с гарантией
доставки), SR-48 (недоступность дольше суток предъявляется отдельным
отчётом), SR-49 (расхождение считается по числу закрытых операций за
месяц, порог отключения старой регистрации 2 %), BR-81 (отчёт адресован
руководителю производства), SR-104 (событие в 1С не создаётся).

ЧТО ЗДЕСЬ РЕШАЕТСЯ И ПОЧЕМУ ЭТО ГЛАВНОЕ В ШАГЕ 1

    Шаг 1 объявлен завершённым не тогда, когда отметки пошли, а тогда,
    когда по ним МОЖНО ВЫКЛЮЧИТЬ СТАРУЮ РЕГИСТРАЦИЮ. Пока цех отмечает
    и здесь, и в 1С, проект добавил работы и не убрал ни одной. Условие
    выключения названо числом: расхождение по числу закрытых операций за
    месяц меньше 2 % (SR-49, решение D-163). Значит расхождение надо
    уметь считать — иначе критерий приёмки остаётся словами.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ

    Числа 1С модуль НЕ ДОБЫВАЕТ. Оно приходит аргументом. Формат выгрузки
    операционной истории зависит от ответа программиста 1С по вопросам
    OQ-114 (полон ли ключ отдачи статуса) и OQ-115 (принимает ли сервис
    код исполнителя вместо ФИО). Написать разборщик под невыясненный
    формат значит написать его дважды; написать сверку так, чтобы число
    приходило снаружи, — значит не ждать ответа вовсе.

    То же с видом сообщения «завершение заказа»: маршрут заказа известен
    из выгрузки 1С, которой ещё нет. Поэтому маршрут — аргумент, а не
    догадка модуля. Решение о том, что переделка фабричного заказа
    завершается передачей (D-225), сюда не внесено: чем именно
    отмечается передача, спрошено вопросом OQ-127 и не отвечено.

ГРАНИЦА С НАПРАВЛЕНИЕМ ПОДЕТАЛЬНОГО УЧЁТА

    Таблица `status_outbox` принадлежит соседнему направлению (КОНТРАКТ
    интеграции §5). Схема здесь НЕ МЕНЯЕТСЯ: вид сообщения живёт полем
    внутри payload и в ключе очереди, доступ — только через публичные
    методы `Storage`. Это осознанный размен: поле в JSON хуже колонки для
    выборок, но колонка в чужой таблице хуже для совместной работы.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# SR-45. Ровно три вида сообщений — перечень ЯВНЫЙ и закрытый
# ---------------------------------------------------------------------------
# Требование говорит «ровно три». Перечислить их в одном месте и проверять
# принадлежность — единственный способ, которым «ровно три» отличается от
# «не менее трёх»: четвёртый вид не появится опечаткой в вызове.
MSG_OPERATION_STATUS = "operation-status"   # статус операции на участке
MSG_ORDER_DONE = "order-done"               # завершение заказа
MSG_REJECTION = "rejection"                 # отклонение

MESSAGE_KINDS = (MSG_OPERATION_STATUS, MSG_ORDER_DONE, MSG_REJECTION)

KIND_NAMES = {
    MSG_OPERATION_STATUS: "статус операции",
    MSG_ORDER_DONE: "завершение заказа",
    MSG_REJECTION: "отклонение",
}

# SR-49, решение D-163: порог, ниже которого старую регистрацию можно
# выключать. Держится константой, а не числом в коде отчёта: критерий
# приёмки обязан быть виден в одном месте и меняться решением, а не правкой.
DISCREPANCY_THRESHOLD = 0.02

# SR-48: «дольше суток». Часы, а не «давно».
STUCK_AFTER_HOURS = 24


class UnknownMessageKind(ValueError):
    """Вид сообщения вне перечня SR-45."""

    def __init__(self, kind: str):
        super().__init__(
            f"вид сообщения «{kind}» не входит в три, названные SR-45: "
            f"{', '.join(MESSAGE_KINDS)}")
        self.kind = kind


class RouteNotKnown(ValueError):
    """Маршрут заказа не передан, а вывести его модулю неоткуда."""

    def __init__(self, order_num: int):
        super().__init__(
            f"маршрут заказа {order_num} не передан. Модуль его не "
            f"домысливает: маршрут приходит из выгрузки 1С, формат которой "
            f"выясняется вопросом OQ-114")
        self.order_num = order_num


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Отчёты
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StuckQueueReport:
    """
    SR-48 и BR-81: что предъявляется руководителю производства.

    Отчёт отвечает на три вопроса цеха, а не на один: сколько висит,
    с какого момента и на чём именно споткнулось. Последнее важнее
    прочего: «очередь не уходит» без причины отказа — сообщение, по
    которому нельзя действовать.
    """
    stuck: int
    oldest_at: Optional[str]
    oldest_age_hours: float
    by_kind: dict
    last_messages: tuple
    # Адресат назван требованием BR-81 поимённо — роль, а не «ответственный».
    # Хранится в двух падежах: отчёт читает человек, и «предъявляется
    # руководитель производства» читается как ошибка, а не как адресат.
    addressee: str = "Руководитель производства"
    addressee_dative: str = "руководителю производства"

    @property
    def is_alarm(self) -> bool:
        """Тревога — по СРОКУ, а не по числу: одна запись суточной давности
        значит, что доставка сломана, а сто записей за час — что цех работал."""
        return self.oldest_age_hours >= STUCK_AFTER_HOURS

    def as_text(self) -> str:
        if not self.stuck:
            return "Очередь в 1С пуста: недоставленных сообщений нет."
        виды = ", ".join(f"{KIND_NAMES.get(k, k)} — {v}"
                         for k, v in sorted(self.by_kind.items()))
        строки = [
            f"Недоставлено сообщений: {self.stuck} ({виды}).",
            f"Самое старое ждёт {self.oldest_age_hours:.1f} ч "
            f"(с {self.oldest_at}).",
        ]
        if self.is_alarm:
            строки.append(
                f"СВЫШЕ СУТОК. По требованию SR-48 предъявляется "
                f"{self.addressee_dative} (BR-81). Цех при этом не останавливался: "
                f"отметки приняты и сохранены, ждёт только передача в 1С.")
        if self.last_messages:
            строки.append("Последние отказы: " + "; ".join(self.last_messages))
        return "\n".join(строки)


@dataclass(frozen=True)
class ReconcileReport:
    """
    SR-49: расхождение за месяц по числу ЗАКРЫТЫХ ОПЕРАЦИЙ.

    Единица сравнения названа требованием и здесь не подменяется: не
    заказы, не детали, не отметки — закрытые операции. Подмена единицы —
    самый тихий способ получить красивое расхождение.
    """
    month: str
    ours: int
    theirs: int
    threshold: float = DISCREPANCY_THRESHOLD

    @property
    def diff(self) -> int:
        return self.ours - self.theirs

    @property
    def rate(self) -> float:
        """Доля расхождения. База — число 1С: старая регистрация пока
        считается верной, и меряется отклонение от неё, а не от себя."""
        if self.theirs == 0:
            # Ноль в знаменателе — не «расхождение 0 %», а невозможность
            # считать. Возвращается бесконечность, чтобы порог никогда не
            # был пройден молча.
            return float("inf") if self.ours else 0.0
        return abs(self.diff) / self.theirs

    @property
    def may_switch_off(self) -> bool:
        """Можно ли выключать старую регистрацию (SR-49, D-163)."""
        return self.rate < self.threshold

    def as_text(self) -> str:
        if self.theirs == 0 and self.ours:
            return (f"{self.month}: в 1С за месяц ноль закрытых операций "
                    f"при {self.ours} у нас. Это не расхождение в процентах, "
                    f"а отсутствие базы сравнения — считать нечего, старую "
                    f"регистрацию выключать нельзя.")
        вывод = ("расхождение ниже порога — старую регистрацию можно "
                 "выключать" if self.may_switch_off else
                 "расхождение выше порога — старая регистрация остаётся")
        return (f"{self.month}: закрытых операций у нас {self.ours}, "
                f"в 1С {self.theirs}, расхождение {self.diff:+d} "
                f"({self.rate:.2%} при пороге {self.threshold:.0%}). {вывод}.")


# ---------------------------------------------------------------------------
# Сам обмен
# ---------------------------------------------------------------------------
class OneCSync:
    """Постановка сообщений в очередь и два отчёта о её состоянии."""

    def __init__(self, storage):
        self.st = storage

    # -- постановка в очередь ------------------------------------------
    def enqueue(self, kind: str, op_key: str, payload: dict) -> int:
        """
        Единственный вход в очередь: вид сообщения проверяется здесь.

        Проверка стоит на входе, а не при чтении, потому что читателем
        может оказаться 1С, и узнать о четвёртом виде от неё — значит
        узнать поздно.
        """
        if kind not in MESSAGE_KINDS:
            raise UnknownMessageKind(kind)
        return self.st.enqueue_status(op_key, {**payload, "kind": kind})

    def enqueue_order_done(self, order_num: int, route: Iterable[str],
                           closed_areas: Optional[Iterable[str]] = None) -> Optional[int]:
        """
        SR-45, сообщение второе: завершение заказа.

        Ставится ТОЛЬКО когда закрыты все участки маршрута. Маршрут
        приходит аргументом — см. заголовок модуля. Если закрыты не все,
        возвращается None: молчание здесь правильнее сообщения, потому
        что «заказ готов» при незакрытом участке — ложь, которую 1С
        примет за правду.
        """
        route = [a for a in route]
        if not route:
            raise RouteNotKnown(order_num)
        if closed_areas is None:
            closed_areas = [r["area_id"] for r in self.st.get_order_sessions(order_num)
                            if r["closed_at"]]
        осталось = [a for a in route if a not in set(closed_areas)]
        if осталось:
            return None
        link = self.st.get_order_link(order_num)
        return self.enqueue(MSG_ORDER_DONE, f"{order_num}|order-done", {
            "order_full_num": link["order_full_num"] if link else "",
            "order_date": link["order_date"] if link else "",
            "order_num": order_num,
            "route": route,
            "status": "Заказ выполнен",
            "completed_at": _now(),
        })

    def enqueue_rejection(self, order_num: int, area_id: str, reason: str,
                          person_code: str = "") -> int:
        """
        SR-45, сообщение третье: отклонение.

        Отклонение — не ошибка системы, а отказ принять работу: 1С обязана
        узнать, что заказ не пошёл дальше, и почему. Причина обязательна:
        отклонение без причины неотличимо от потери сообщения.
        """
        if not reason or not reason.strip():
            raise ValueError("отклонение без причины не отправляется: "
                             "1С не сможет отличить его от потери сообщения")
        link = self.st.get_order_link(order_num)
        return self.enqueue(MSG_REJECTION, f"{order_num}|{area_id}|rejection", {
            "order_full_num": link["order_full_num"] if link else "",
            "order_date": link["order_date"] if link else "",
            "order_num": order_num,
            "area_id": area_id,
            "status": "Отклонено",
            "reason": reason.strip(),
            "person_code": person_code,
            "rejected_at": _now(),
        })

    # -- отчёты ---------------------------------------------------------
    def stuck_queue(self, now: Optional[str] = None,
                    limit: int = 10_000) -> StuckQueueReport:
        """SR-48, BR-81: что висит в очереди и сколько уже висит."""
        сейчас = _parse(now) if now else datetime.now()
        строки = self.st.get_pending_statuses(limit)
        if not строки:
            return StuckQueueReport(0, None, 0.0, {}, ())
        по_видам, самая_старая, отказы = {}, None, []
        for r in строки:
            try:
                payload = json.loads(r["payload"])
            except (ValueError, TypeError):
                payload = {}
            вид = payload.get("kind", MSG_OPERATION_STATUS)
            по_видам[вид] = по_видам.get(вид, 0) + 1
            создана = _parse(r["created_at"])
            if самая_старая is None or создана < самая_старая:
                самая_старая = создана
            if r["message"]:
                отказы.append(f'{r["op_key"]}: {r["message"]}')
        часы = (сейчас - самая_старая).total_seconds() / 3600
        return StuckQueueReport(
            stuck=len(строки),
            oldest_at=самая_старая.isoformat(timespec="seconds"),
            oldest_age_hours=часы,
            by_kind=по_видам,
            last_messages=tuple(отказы[-3:]),
        )

    def closed_operations(self, month: str) -> int:
        """
        Число закрытых операций за месяц ПО НАШИМ данным.

        Операция = закрытие заказа на участке, то есть строка
        `order_area_sessions` с проставленным `closed_at`. Именно эта
        единица названа требованием SR-49; счёт по заказам дал бы число
        втрое меньше и сравнение с 1С потеряло бы смысл.
        """
        строки = self.st._conn.execute(
            """SELECT COUNT(*) FROM order_area_sessions
                WHERE closed_at IS NOT NULL AND substr(closed_at,1,7)=?""",
            (month,)).fetchone()
        return строки[0]

    def reconcile(self, month: str, theirs: int,
                  ours: Optional[int] = None) -> ReconcileReport:
        """
        SR-49: сверка за месяц. Число 1С приходит аргументом — см. заголовок.
        """
        return ReconcileReport(month=month,
                               ours=self.closed_operations(month) if ours is None else ours,
                               theirs=theirs)
