"""
Тесты оркестратора (src/pipeline.py) — сквозной контур и персистентность.

Главное, что здесь проверяется: **факт переживает перезапуск приложения**.
До Этапа A счётчики сканов жили только в памяти OrderContext и терялись
при остановке процесса — операция «сбрасывалась» в ноль.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from conftest import (
    UID_BACK_10,
    UID_PANEL_16,
    UID_SHELF_16,
    build_xbir,
    detail_row,
    qr,
    scan,
)
from pipeline import ProductionCore, qr_of
from scan_processor import FactStatus
from status_calc import OpStatus
from storage import Storage

EDGE_08 = "Облицовывание кромки 19/0,8"
CUT_16 = "Раскрой плиты 16мм"
PACKING = "Упаковка паллета"


# --- Импорт .xbir в хранилище -----------------------------------------------

def test_import_loads_details(core: ProductionCore, xbir_file: Path):
    res = core.import_xbir([xbir_file])
    assert res.files == 1
    assert res.details_imported == 3
    assert res.orders == [6564]
    assert res.errors == 0
    assert core.storage.count_details() == 3


def test_import_computes_qr(loaded_core: ProductionCore):
    row = loaded_core.storage.get_detail_by_qr(qr(UID_SHELF_16))
    assert row is not None
    assert row["detail_uid"] == UID_SHELF_16
    assert row["qty"] == 3


def test_import_is_idempotent(core: ProductionCore, xbir_file: Path):
    """Повторный импорт того же файла не плодит дубли деталей."""
    core.import_xbir([xbir_file])
    core.import_xbir([xbir_file])
    assert core.storage.count_details() == 3


def test_import_detects_qr_collisions(core: ProductionCore, tmp_path: Path):
    """
    Коллизия MD5[:10] сделала бы два GUID неразличимыми для сканера.
    На синтетике её не подделать, поэтому проверяем, что механизм
    контроля существует и на чистых данных молчит.
    """
    f = build_xbir(tmp_path / "clean.xbir", [
        detail_row(UID_PANEL_16, pos="1"),
        detail_row(UID_SHELF_16, pos="2"),
    ])
    res = core.import_xbir([f])
    assert res.qr_collisions == []


def test_qr_matches_spec_format():
    """QR = MD5(GUID)[:10], 10 hex-символов (docs/qr-format-spec.md)."""
    code = qr_of(UID_PANEL_16)
    assert len(code) == 10
    assert all(c in "0123456789abcdef" for c in code)


# --- Загрузка заказа из БД ---------------------------------------------------

def test_load_order_restores_details(loaded_core: ProductionCore):
    ctx = loaded_core.load_order(6564)
    assert len(ctx.details) == 3
    assert set(ctx.qr_to_detail) == {
        qr(UID_PANEL_16), qr(UID_SHELF_16), qr(UID_BACK_10)
    }
    assert ctx.detail_qty[UID_SHELF_16] == 3


def test_load_unknown_order_is_empty(loaded_core: ProductionCore):
    ctx = loaded_core.load_order(9999)
    assert ctx.details == []


# --- Персистентность факта: ключевая проверка Этапа A -----------------------

def test_fact_survives_restart(loaded_core: ProductionCore, t0, tmp_path: Path):
    """
    Отсканировали 2 из 3 деталей, «перезапустили приложение» —
    счётчик должен восстановиться, а не обнулиться.
    """
    ctx = loaded_core.load_order(6564)
    for i in range(2):
        r = loaded_core.handle_scan(
            scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=30 * i)), ctx
        )
        assert r.status is FactStatus.ACCEPTED

    db_path = loaded_core.storage.db_path
    loaded_core.storage.close()

    # Новый процесс: новое подключение, новый контекст
    revived = ProductionCore(Storage(db_path))
    ctx2 = revived.load_order(6564)
    assert ctx2.scanned[(EDGE_08, UID_SHELF_16)] == 2

    # Третий скан закрывает операцию, а не начинает счёт заново
    r = revived.handle_scan(scan(UID_SHELF_16, EDGE_08, t0 + timedelta(hours=1)), ctx2)
    assert r.scanned_count == 3

    statuses = revived.order_status(ctx2, [EDGE_08])
    assert statuses[0].status is OpStatus.COMPLETED
    revived.storage.close()


def test_duplicate_window_survives_restart(loaded_core: ProductionCore, t0):
    """
    Окно дубликатов должно быть общим, а не жить в памяти процесса.

    Дефект, найденный на CLI: last_scan_time хранился только в
    OrderContext, поэтому повторный скан после перезапуска (или с
    соседнего рабочего места по тому же заказу) проходил как новая
    деталь и завышал факт.
    """
    ctx = loaded_core.load_order(6564)
    r1 = loaded_core.handle_scan(scan(UID_SHELF_16, EDGE_08, t0), ctx)
    assert r1.status is FactStatus.ACCEPTED

    db_path = loaded_core.storage.db_path
    loaded_core.storage.close()

    # Другой процесс / другое рабочее место, скан в пределах окна
    other = ProductionCore(Storage(db_path))
    ctx2 = other.load_order(6564)
    r2 = other.handle_scan(
        scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=1)), ctx2
    )
    assert r2.status is FactStatus.DUPLICATE
    assert other.storage.get_fact_count(EDGE_08, UID_SHELF_16) == 1
    other.storage.close()


def test_accepted_scan_written_to_facts(loaded_core: ProductionCore, t0):
    ctx = loaded_core.load_order(6564)
    loaded_core.handle_scan(scan(UID_SHELF_16, EDGE_08, t0), ctx)
    assert loaded_core.storage.get_fact_count(EDGE_08, UID_SHELF_16) == 1


def test_rejected_scan_not_written_to_facts(loaded_core: ProductionCore, t0):
    """Ошибочный скан в факт не идёт, но в audit log попадает."""
    ctx = loaded_core.load_order(6564)
    r = loaded_core.handle_scan(
        scan("x", EDGE_08, t0, qr_code="deadbeef00"), ctx
    )
    assert r.status is FactStatus.UNKNOWN_QR
    assert loaded_core.storage.get_facts_by_order(6564) == []
    assert loaded_core.storage.count_scan_events() == 1


# --- Audit log ---------------------------------------------------------------

def test_all_events_logged_including_errors(loaded_core: ProductionCore, t0):
    """Принцип 6: все ошибки и предупреждения пишутся в лог, не теряются."""
    ctx = loaded_core.load_order(6564)
    events = [
        scan(UID_SHELF_16, EDGE_08, t0),                                  # accepted
        scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=1)),           # duplicate
        scan("x", EDGE_08, t0 + timedelta(seconds=20), qr_code="beef00dead"),  # unknown
        scan(UID_BACK_10, EDGE_08, t0 + timedelta(seconds=40)),           # wrong op
    ]
    for e in events:
        loaded_core.handle_scan(e, ctx)

    assert loaded_core.storage.count_scan_events() == 4
    assert loaded_core.storage.count_accepted_events() == 1


def test_stats_summary(loaded_core: ProductionCore, t0):
    ctx = loaded_core.load_order(6564)
    loaded_core.handle_scan(scan(UID_SHELF_16, EDGE_08, t0), ctx)
    stats = loaded_core.storage.stats()
    assert stats == {"details": 3, "scan_events": 1, "accepted": 1}


# --- Справочник участков из БД ----------------------------------------------

def test_seed_areas_populates_directory(core: ProductionCore):
    ops = core.area_operations("area_edging")
    assert EDGE_08 in ops
    assert CUT_16 not in ops


def test_technologist_can_add_operation_without_code_change(
    loaded_core: ProductionCore, t0
):
    """
    Технолог добавляет операцию участку прямо в БД — код не трогается,
    скан начинает проходить.
    """
    loaded_core.storage.upsert_area_operation(
        "area_edging", "Кромление", CUT_16
    )
    ctx = loaded_core.load_order(6564)
    r = loaded_core.handle_scan(
        scan(UID_PANEL_16, CUT_16, t0, area="area_edging"), ctx
    )
    assert r.status is FactStatus.ACCEPTED


# --- Сквозной сценарий -------------------------------------------------------

def test_end_to_end_order_closing(loaded_core: ProductionCore, t0):
    """
    Полный цикл: импорт → сканы всех деталей операции → операция закрыта →
    payload готов к отправке в 1С.
    """
    ctx = loaded_core.load_order(6564)
    operations = [EDGE_08, CUT_16, PACKING]

    for i in range(3):
        loaded_core.handle_scan(
            scan(UID_SHELF_16, EDGE_08, t0 + timedelta(seconds=30 * i)), ctx
        )

    payloads = loaded_core.closing_payloads(ctx, operations)
    assert len(payloads) == 1
    assert payloads[0]["operation"] == EDGE_08
    assert payloads[0]["status"] == "Выполнено"

    statuses = {s.operation_1c: s.status for s in loaded_core.order_status(ctx, operations)}
    assert statuses[EDGE_08] is OpStatus.COMPLETED
    assert statuses[CUT_16] is OpStatus.NOT_STARTED
