"""
Тесты записи статуса операции в 1С (src/one_c_writer.py).

Проверяют главное свойство — **гарантию доставки**: статус не теряется,
если 1С недоступен, и не отправляется дважды при повторном закрытии.
"""
from __future__ import annotations

import json

import pytest

from one_c_writer import (
    HttpTransport,
    LogTransport,
    SendResult,
    StatusWriter,
    Transport,
)
from storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "outbox.db")
    yield s
    s.close()


def payload(op_id="op_6564_3", operation="Раскрой плиты 16мм",
            status="Выполнено"):
    return {
        "operation_id": op_id,
        "order_num": 6564,
        "operation": operation,
        "status": status,
        "completed_at": "2026-07-22T10:00:00",
        "scanned_details": 295,
        "planned_details": 295,
    }


class FailingTransport(Transport):
    """1С недоступен — все отправки падают."""
    def send(self, payload):
        return SendResult(ok=False, http_status=0, message="сеть недоступна")


class FlakyTransport(Transport):
    """Падает заданное число раз, потом отвечает успехом."""
    def __init__(self, fail_times: int):
        self.remaining = fail_times
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return SendResult(ok=False, message="temporarily down")
        return SendResult(ok=True, http_status=200, message="ok")


# --- Заглушка-лог ------------------------------------------------------------

def test_log_transport_records_but_does_not_send():
    t = LogTransport()
    r = t.send(payload())
    assert r.ok
    assert t.sent == [payload()]


def test_writer_delivers_via_log_stub(storage):
    writer = StatusWriter(storage, LogTransport())
    queued, delivered = writer.push_closing_operations([payload()])
    assert queued == 1
    assert delivered == 1
    assert storage.count_delivered_statuses() == 1


# --- Гарантия доставки -------------------------------------------------------

def test_undelivered_status_stays_in_queue(storage):
    """1С лежит — статус остаётся в очереди, не теряется."""
    writer = StatusWriter(storage, FailingTransport())
    writer.push_closing_operations([payload()])

    assert storage.count_delivered_statuses() == 0
    pending = storage.get_pending_statuses()
    assert len(pending) == 1
    assert json.loads(pending[0]["payload"])["operation_id"] == "op_6564_3"


def test_pending_delivered_on_retry(storage):
    """Статус уходит при повторной попытке, когда 1С поднялся."""
    transport = FlakyTransport(fail_times=1)
    writer = StatusWriter(storage, transport)

    writer.push_closing_operations([payload()])       # первая попытка — провал
    assert storage.count_delivered_statuses() == 0

    delivered, remaining = writer.flush_pending()      # 1С поднялся
    assert delivered == 1
    assert remaining == 0
    assert storage.count_delivered_statuses() == 1


def test_status_deduplicated_by_operation(storage):
    """Повторное закрытие той же операции не плодит дубли в очереди."""
    writer = StatusWriter(storage, FailingTransport())
    writer.push_closing_operations([payload()])
    writer.push_closing_operations([payload()])        # то же самое ещё раз

    assert len(storage.get_pending_statuses()) == 1


def test_only_completed_statuses_are_queued(storage):
    """Незакрытая операция (status != Выполнено) в 1С не отправляется."""
    writer = StatusWriter(storage, LogTransport())
    queued, _ = writer.push_closing_operations([
        payload(status=None),
        payload(op_id="op_x", status="Выполнено"),
    ])
    assert queued == 1


def test_attempts_counter_increments(storage):
    writer = StatusWriter(storage, FailingTransport())
    writer.push_closing_operations([payload()])
    writer.flush_pending()
    writer.flush_pending()

    row = storage.get_pending_statuses()[0]
    assert row["attempts"] >= 2


# --- HTTP-транспорт (без реальной сети) --------------------------------------

def test_http_transport_handles_unreachable():
    """Недоступный endpoint не роняет процесс — возвращает неуспех."""
    t = HttpTransport("http://127.0.0.1:9/nonexistent", timeout=0.2)
    r = t.send(payload())
    assert not r.ok
    assert r.http_status == 0


# --- Интеграция с оркестратором ----------------------------------------------

def test_pipeline_push_status(tmp_path):
    """Сквозной путь: закрытая операция → очередь → доставка через заглушку."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from pipeline import ProductionCore
    from conftest import UID_SHELF_16, build_xbir, detail_row, scan
    from datetime import timedelta

    core = ProductionCore(Storage(tmp_path / "p.db"))
    core.seed_areas()
    f = build_xbir(tmp_path / "o.xbir", [
        detail_row(UID_SHELF_16, thickness=16, length=600, width=400, qty=3,
                   edge_l1=0.8, edge_l2=0.8, pos="2"),
    ])
    core.import_xbir([f])
    ctx = core.load_order(6564)

    t0 = __import__("datetime").datetime(2026, 7, 22, 8, 0, 0)
    for i in range(3):
        core.handle_scan(
            scan(UID_SHELF_16, "Облицовывание кромки 19/0,8",
                 t0 + timedelta(seconds=30 * i)), ctx)

    queued, delivered = core.push_status_to_1c(
        ctx, ["Облицовывание кромки 19/0,8"])
    assert queued == 1
    assert delivered == 1
    core.storage.close()
