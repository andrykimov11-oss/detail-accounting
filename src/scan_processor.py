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


# --- Конфиг: участок → список операций 1С -----------------------------------
# Оператор регистрируется на участке (area_id), видит операции участка.

AREAS: dict[str, list[str]] = {
    "area_cutting": [
        "Раскрой плиты 10мм",
        "Раскрой плиты 16мм",
        "Раскрой плиты 16мм (свыше 30 м.п.)",
    ],
    "area_edging": [
        "Облицовывание кромки 19/0,8",
        "Облицовывание кромки 19/2",
        "Облицовывание кромки 19/0,4",
    ],
}

DUPLICATE_WINDOW_SEC = 5  # Принцип 4: дубликат в течение 5 сек


# --- Модель входа/выхода -----------------------------------------------------

@dataclass
class ScanEvent:
    """Сырое событие сканера (Принцип 2)."""
    scan_id: str
    qr_code: str
    area_id: str                    # участок, где оператор зарегистрировался
    operator_id: str
    operation_1c: str               # выбранная операция (из списка участка)
    scanned_at: datetime


class FactStatus(str, Enum):
    ACCEPTED = "accepted"           # факт засчитан
    DUPLICATE = "duplicate"         # отброшен как дубликат
    OVERPLAN = "overplan"           # сверх плана
    UNKNOWN_QR = "unknown_qr"       # QR не найден → показать список деталей
    WRONG_ORDER = "wrong_order"     # другой заказ на участке
    WRONG_OPERATION = "wrong_op"    # операция не относится к детали → список деталей
    UNKNOWN_AREA = "unknown_area"   # участок не описан
    OP_NOT_IN_AREA = "op_not_in_area"  # операция не входит в операции участка


@dataclass
class FactResult:
    """Результат обработки события сканера."""
    status: FactStatus
    detail_uid: str = ""
    order_num: int = 0
    operation_1c: str = ""
    scanned_count: int = 0          # сколько деталей этого GUID отсканировано
    planned_qty: int = 0
    message: str = ""
    anomaly: bool = False
    suggest_detail_list: bool = False  # показать список деталей (fallback)


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
    # накопленный факт: (operation_1c, detail_uid) → сколько деталей отсканировано
    scanned: dict[tuple[str, str], int] = field(default_factory=dict)
    # последние сканы для детекта дубликатов: (qr, operation) → время последнего скана
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

    Поток (см. docs/scan-event-spec.md):
      участок → операции участка → выбранная операция
      → проверка QR → проверка применимости операции к детали
      → счётчик деталей по GUID → факт.
    """
    # 1. Участок → список операций
    area_ops = AREAS.get(event.area_id)
    if area_ops is None:
        return FactResult(
            status=FactStatus.UNKNOWN_AREA,
            message=f"участок '{event.area_id}' не описан",
        )
    if event.operation_1c not in area_ops:
        return FactResult(
            status=FactStatus.OP_NOT_IN_AREA,
            message=f"операция '{event.operation_1c}' не входит в операции участка '{event.area_id}'",
        )

    # 2. Дубликат в окне 5 сек (Принцип 4)
    dup_key = (event.qr_code, event.operation_1c)
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
            suggest_detail_list=True,
        )

    # 4. Чужой заказ
    if detail.order_num != ctx.order_num:
        return FactResult(
            status=FactStatus.WRONG_ORDER,
            detail_uid=detail.detail_uid,
            order_num=detail.order_num,
            message=f"деталь заказа {detail.order_num}, а участок заказа {ctx.order_num}",
        )

    # 5. Применима ли операция к детали? (Принцип 6: нет → список деталей)
    rule = find_rule(event.operation_1c)
    if rule is not None and not rule.detail_predicate(detail):
        return FactResult(
            status=FactStatus.WRONG_OPERATION,
            detail_uid=detail.detail_uid,
            order_num=detail.order_num,
            operation_1c=event.operation_1c,
            message=f"деталь не подходит под операцию '{event.operation_1c}'",
            suggest_detail_list=True,
        )

    # 6. Плановое qty и текущий счётчик
    planned_qty = ctx.detail_qty.get(detail.detail_uid, 0)
    if planned_qty == 0:
        return FactResult(
            status=FactStatus.UNKNOWN_QR,
            detail_uid=detail.detail_uid,
            message="нет планового qty для детали (проверить .xbir)",
            suggest_detail_list=True,
        )

    fact_key = (event.operation_1c, detail.detail_uid)
    current = ctx.scanned.get(fact_key, 0)
    new_count = current + 1

    # 7. Превышение плана
    if new_count > planned_qty:
        ctx.last_scan_time[dup_key] = event.scanned_at
        return FactResult(
            status=FactStatus.OVERPLAN,
            detail_uid=detail.detail_uid,
            order_num=detail.order_num,
            operation_1c=event.operation_1c,
            scanned_count=new_count,
            planned_qty=planned_qty,
            message=f"превышение плана: отсканировано {new_count} при qty={planned_qty}",
        )

    # 8. Принять факт
    ctx.scanned[fact_key] = new_count
    ctx.last_scan_time[dup_key] = event.scanned_at
    return FactResult(
        status=FactStatus.ACCEPTED,
        detail_uid=detail.detail_uid,
        order_num=detail.order_num,
        operation_1c=event.operation_1c,
        scanned_count=new_count,
        planned_qty=planned_qty,
    )


def suggest_details(ctx: OrderContext, operation_1c: str) -> list[dict]:
    """
    Список деталей заказа для ручного выбора (fallback при ошибке скана).
    Возвращает детали, применимые к операции, с их характеристиками.
    """
    rule = find_rule(operation_1c)
    out = []
    for d in ctx.details:
        if rule and not rule.detail_predicate(d):
            continue
        out.append({
            "detail_uid": d.detail_uid,
            "qr_code": hashlib.md5(d.detail_uid.encode()).hexdigest()[:10],
            "pos_no": d.pos_no,
            "length": d.length,
            "width": d.width,
            "thickness": d.thickness,
            "qty": d.qty,
            "material": d.material_name,
        })
    return out
