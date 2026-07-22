"""
Запись статуса операции обратно в 1С.

Контекст: docs/architecture.md (решение 4), docs/1c-export-spec.md.
Когда все плановые детали операции отсканированы, статус «Выполнено»
отправляется в 1С через HTTP-сервис. Это единственная доработка 1С в
проекте — сервис приёма статуса разрабатывает программист 1С.

## Принцип: очередь с гарантией доставки

Статус не отправляется «в никуда». Он кладётся в очередь (таблица
`status_outbox`) и помечается отправленным только после успешного ответа
1С. Если сервис недоступен, запись остаётся в очереди и уходит при
следующей попытке. Так статус операции не теряется, даже если 1С лежит.

## Заглушка на время пилота

HTTP-сервис 1С может быть ещё не готов (поле `operation_id`, endpoint,
авторизация уточняются). До этого момента writer работает в режиме
заглушки: пишет payload в лог и очередь, но никуда не отправляет.
Переключение на боевой режим — смена транспорта, остальной код не меняется.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class SendResult:
    """Итог попытки отправки статуса в 1С."""
    ok: bool
    http_status: int = 0
    message: str = ""
    payload: dict | None = None


# --- Транспорты --------------------------------------------------------------

class Transport(ABC):
    """Абстракция канала доставки статуса в 1С."""

    @abstractmethod
    def send(self, payload: dict) -> SendResult:
        ...


class LogTransport(Transport):
    """
    Заглушка на время пилота: статус никуда не уходит, только пишется в лог.

    Используется, пока HTTP-сервис 1С не готов. Всё остальное поведение
    (очередь, пометка отправленных) работает как в бою — переключение на
    HttpTransport ничего в вызывающем коде не меняет.
    """

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, payload: dict) -> SendResult:
        self.sent.append(payload)
        return SendResult(ok=True, http_status=0,
                          message="log stub (1С endpoint не подключён)",
                          payload=payload)


class HttpTransport(Transport):
    """
    Боевой транспорт: POST статуса на HTTP-сервис 1С.

    Endpoint, формат и авторизация — по docs/1c-export-spec.md. Таймаут
    небольшой: если 1С не ответил быстро, запись остаётся в очереди и
    уйдёт при следующей попытке, а не блокирует цех.
    """

    def __init__(self, endpoint: str, token: str = "", timeout: float = 5.0):
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout

    def send(self, payload: dict) -> SendResult:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(self.endpoint, data=data,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                code = resp.getcode()
                ok = 200 <= code < 300
                return SendResult(ok=ok, http_status=code,
                                  message="ok" if ok else f"HTTP {code}",
                                  payload=payload)
        except urllib.error.HTTPError as e:
            return SendResult(ok=False, http_status=e.code,
                              message=f"HTTP {e.code}: {e.reason}", payload=payload)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return SendResult(ok=False, http_status=0,
                              message=f"сеть недоступна: {e}", payload=payload)


# --- Отправитель с очередью --------------------------------------------------

class StatusWriter:
    """
    Отправка статусов операций в 1С через очередь с гарантией доставки.

    Статус сначала попадает в outbox (storage), затем отправляется.
    Помечается доставленным только при успехе. Недоставленные остаются в
    очереди — `flush_pending` повторяет их отправку.
    """

    def __init__(self, storage, transport: Transport):
        self.storage = storage
        self.transport = transport

    def enqueue(self, payload: dict) -> int:
        """
        Поставить статус в очередь отправки. Возвращает id записи outbox.

        Дедупликация по operation_id: повторный статус той же операции
        обновляет запись, а не плодит дубли (операция закрывается один раз).
        """
        key = payload.get("operation_id") or (
            f"{payload.get('order_num')}|{payload.get('operation')}"
        )
        return self.storage.enqueue_status(key, payload)

    def send_one(self, outbox_id: int, key: str, payload: dict) -> SendResult:
        result = self.transport.send(payload)
        self.storage.mark_status_sent(
            outbox_id,
            delivered=result.ok,
            http_status=result.http_status,
            message=result.message,
        )
        return result

    def flush_pending(self, limit: int = 100) -> tuple[int, int]:
        """
        Отправить все недоставленные статусы из очереди.

        Возвращает (доставлено, осталось). Вызывается по расписанию или
        после закрытия операций — недоставленное переживает недоступность 1С.
        """
        pending = self.storage.get_pending_statuses(limit=limit)
        delivered = 0
        for row in pending:
            payload = json.loads(row["payload"])
            result = self.send_one(row["id"], row["op_key"], payload)
            if result.ok:
                delivered += 1
        remaining = len(self.storage.get_pending_statuses(limit=limit + 1))
        return delivered, remaining

    def push_closing_operations(self, payloads: list[dict]) -> tuple[int, int]:
        """
        Поставить в очередь и сразу попытаться отправить статусы закрытых
        операций. Возвращает (поставлено, доставлено).
        """
        queued = 0
        for payload in payloads:
            if payload.get("status") != "Выполнено":
                continue
            self.enqueue(payload)
            queued += 1
        delivered, _ = self.flush_pending()
        return queued, delivered
