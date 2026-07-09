"""
Обработка события сканера → факт по детали.

Контекст: docs/scan-event-spec.md. Принимает «сырое» событие сканера,
резолвит QR → деталь, проверяет по плану операции и возвращает либо факт
по детали, либо ошибку.

Не занимается: хранением факта (БД), UI, транспортом события от сканера.
Это — чистая логика обработки, тестируемая без железа.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from xbir_parser import Detail  # noqa: E402
from operation_mapping import find_rule  # noqa: E402
from operation_plan import build_order_plan  # noqa: E402


# --- Конфиг рабочих мест: станок → операция 1С ------------------------------

WORKSTATIONS: dict[str, str] = {
    "ws_cut_10_1": "Раскрой плиты 10мм",
    "ws_cut_16_1": "Раскрой плиты 16мм",
    "ws_edge_08_1": "Облицовывание кромки 19/0,8",
    "ws_edge_2_1":  "Облицовывание кромки 19/2",
    "ws_edge_04_1": "Облицовывание кромки 19/0,4",
}

DUPLICATE_WINDOW_SEC = 30  # Принцип 4: дубликат в течение 30 сек


# --- Модель входа/выхода -----------------------------------------------------

@dataclass
class ScanEvent:
    """Сырое событие сканера (Принцип 2)."""
    scan_id: str
    qr_code: str
    workstation_id: str
    operator_id: str
    scanned_at: datetime


class FactStatus(str, Enum):
    ACCEPTED = "accepted"           # факт засчитан
    DUPLICATE = "duplicate"         # отброшен как дубликат
    OVERPLAN = "overplan"           # сверх плана (сохраняется отдельно)
    UNKNOWN_QR = "unknown_qr"       # QR не найден в плане
    WRONG_ORDER = "wrong_order"     # другой заказ на станции
    WRONG_OPERATION = "wrong_op"    # деталь не относится к операции (аномалия)
    UNKNOWN_WORKSTATION = "unknown_ws"  # станок не описан


@dataclass
class FactResult:
    """Результат обработки события сканера."""
    status: FactStatus
    detail_uid: str = ""
    order_num: int = 0
    operation_1c: str = ""
    instance_no: int = 0            # какой по счёту экземпляр (для qty>1)
    planned_qty: int = 0
    message: str = ""
    anomaly: bool = False           # Принцип 6: факт засчитан, но с предупреждением


# --- Состояние системы: план + накопленный факт -----------------------------

@dataclass
class OrderContext:
    """
    Контекст одного заказа для обработки сканов:
    план по операциям + счётчики уже отсканированных деталей.
    Создаётся один раз при загрузке заказа, переиспользуется между сканами.
    """
    order_num: int
    details: list[Detail]                               # детали из .xbir
    qr_to_detail: dict[str, Detail]                     # MD5(GUID)[:10] → Detail
    detail_qty: dict[str, int]                          # detail_uid → плановое qty
    # накопленный факт: (operation_1c, detail_uid) → сколько экземпляров отсканировано
    scanned: dict[tuple[str, str], int] = field(default_factory=dict)
    # последние сканы для детекта дубликатов: (qr, ws) → время последнего скана
    last_scan_time: dict[tuple[str, str], datetime] = field(default_factory=dict)

    @classmethod
    def build(cls, order_num: int, details: list[Detail]) -> "OrderContext":
        qr_map: dict[str, Detail] = {}
        qty_map: dict[str, int] = {}
        for d in details:
            if d.order_num != order_num:
                continue
            qr = hashlib.md5(d.detail_uid.encode()).hexdigest()[:10]
            qr_map[qr] = d
            # если одна деталь встречается в нескольких плитах — берём max qty
            qty_map[d.detail_uid] = max(qty_map.get(d.detail_uid, 0), d.qty)
        return cls(
            order_num=order_num,
            details=[d for d in details if d.order_num == order_num],
            qr_to_detail=qr_map,
            detail_qty=qty_map,
        )


# --- Обработка одного события -----------------------------------------------

def process_scan(event: ScanEvent, ctx: OrderContext) -> FactResult:
    """
    Обработать событие сканера. Возвращает результат (засчитан/ошибка).
    Мутирует ctx: обновляет счётчики факта.
    """
    # 1. Станок → операция
    operation_1c = WORKSTATIONS.get(event.workstation_id)
    if operation_1c is None:
        return FactResult(
            status=FactStatus.UNKNOWN_WORKSTATION,
            message=f"рабочее место '{event.workstation_id}' не описано",
        )

    # 2. Дубликат в окне 30 сек (Принцип 4)
    dup_key = (event.qr_code, event.workstation_id)
    last = ctx.last_scan_time.get(dup_key)
    if last and (event.scanned_at - last) < timedelta(seconds=DUPLICATE_WINDOW_SEC):
        return FactResult(
            status=FactStatus.DUPLICATE,
            message=f"повторный скан в течение {DUPLICATE_WINDOW_SEC}с — отброшен",
        )

    # 3. QR → деталь
    detail = ctx.qr_to_detail.get(event.qr_code)
    if detail is None:
        return FactResult(
            status=FactStatus.UNKNOWN_QR,
            message=f"QR '{event.qr_code}' не найден в плане заказа {ctx.order_num}",
        )

    # 4. Чужой заказ (на станции активен другой заказ)
    if detail.order_num != ctx.order_num:
        return FactResult(
            status=FactStatus.WRONG_ORDER,
            detail_uid=detail.detail_uid,
            order_num=detail.order_num,
            message=f"деталь заказа {detail.order_num}, а станция заказа {ctx.order_num}",
        )

    # 5. Относится ли деталь к операции? (Принцип 6: аномалия, но засчитываем)
    rule = find_rule(operation_1c)
    anomaly = False
    if rule is not None and not rule.detail_predicate(detail):
        anomaly = True  # деталь без кромки на кромлении и т.п.

    # 6. Плановое qty и текущий счётчик
    planned_qty = ctx.detail_qty.get(detail.detail_uid, 0)
    if planned_qty == 0:
        return FactResult(
            status=FactStatus.UNKNOWN_QR,
            detail_uid=detail.detail_uid,
            message="нет планового qty для детали (проверить .xbir)",
        )

    fact_key = (operation_1c, detail.detail_uid)
    current = ctx.scanned.get(fact_key, 0)
    instance_no = current + 1

    # 7. Превышение плана (Принцип 6)
    if instance_no > planned_qty:
        ctx.scanned[fact_key] = current  # не увеличиваем, сверх плана отдельно
        ctx.last_scan_time[dup_key] = event.scanned_at
        return FactResult(
            status=FactStatus.OVERPLAN,
            detail_uid=detail.detail_uid,
            order_num=detail.order_num,
            operation_1c=operation_1c,
            instance_no=instance_no,
            planned_qty=planned_qty,
            message=f"превышение плана: отсканировано {instance_no} при qty={planned_qty}",
        )

    # 8. Принять факт
    ctx.scanned[fact_key] = instance_no
    ctx.last_scan_time[dup_key] = event.scanned_at
    return FactResult(
        status=FactStatus.ACCEPTED,
        detail_uid=detail.detail_uid,
        order_num=detail.order_num,
        operation_1c=operation_1c,
        instance_no=instance_no,
        planned_qty=planned_qty,
        anomaly=anomaly,
        message="аномалия: деталь не подходит под операцию" if anomaly else "",
    )
