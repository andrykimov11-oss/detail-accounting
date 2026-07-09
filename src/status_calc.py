"""
Расчёт статуса операции по плану и факту.

Контекст: docs/architecture.md, решение 3. Статус операции цеха выводится
из факта по деталям: «Выполнено» ⇔ отсканировано = плановое количество
деталей. Этот модуль считает статус и формирует то, что уходит в 1С через
HTTP-сервис (решение 4).

Входы:
    - OperationPlan (плановый состав операции — из operation_plan)
    - OrderContext (накопленный факт сканов — из scan_processor)

Выходы:
    - OperationStatus со статусом и перечнем потерянных/лишних деталей
    - payload для HTTP-запроса в 1С
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from operation_plan import OperationPlan  # noqa: E402
from scan_processor import OrderContext  # noqa: E402


class OpStatus(str, Enum):
    NOT_STARTED = "not_started"     # ни одной детали не отсканировано
    IN_PROGRESS = "in_progress"     # часть деталей отсканирована
    COMPLETED = "completed"         # все плановые детали отсканированы
    OVERPLAN = "overplan"           # есть факты сверх плана
    LOST = "lost"                   # в конце смены остались неотсканированные


@dataclass
class DetailProgress:
    """Прогресс по одной детали (UID) в рамках операции."""
    detail_uid: str
    planned_qty: int
    scanned_qty: int

    @property
    def is_done(self) -> bool:
        return self.scanned_qty >= self.planned_qty

    @property
    def remaining(self) -> int:
        return max(0, self.planned_qty - self.scanned_qty)


@dataclass
class OperationStatus:
    """Статус операции цеха по заказу."""
    order_num: int
    operation_1c: str
    stage: str
    status: OpStatus
    planned_total: int = 0          # Σ плановых экземпляров
    scanned_total: int = 0          # Σ отсканированных экземпляров
    overplan_total: int = 0         # сверх плана
    details_progress: list[DetailProgress] = field(default_factory=list)
    lost_uids: list[str] = field(default_factory=list)    # неотсканированные UID
    extra_uids: list[str] = field(default_factory=list)   # UID сверх плана/чужие
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def progress_pct(self) -> float:
        if self.planned_total == 0:
            return 0.0
        return round(100.0 * self.scanned_total / self.planned_total, 1)

    @property
    def is_closing(self) -> bool:
        """Готова ли операция к закрытию (запись в 1С)."""
        return self.status == OpStatus.COMPLETED


def calc_operation_status(plan: OperationPlan,
                          ctx: OrderContext,
                          now: datetime | None = None) -> OperationStatus:
    """
    Посчитать статус одной операции по плану и накопленному факту.

    Для позаказных операций (is_per_detail=False) статус не считается
    через сканер — возвращается NOT_STARTED с пометкой.
    """
    now = now or datetime.now()

    if not plan.is_per_detail:
        return OperationStatus(
            order_num=ctx.order_num,
            operation_1c=plan.operation_1c,
            stage=plan.stage,
            status=OpStatus.NOT_STARTED,
            updated_at=now,
        )

    # Проходим по плановым UID операции и собираем прогресс
    progress: list[DetailProgress] = []
    for uid in plan.detail_uids:
        planned = ctx.detail_qty.get(uid, 0)
        scanned = ctx.scanned.get((plan.operation_1c, uid), 0)
        progress.append(DetailProgress(uid, planned, scanned))

    planned_total = sum(p.planned_qty for p in progress)
    scanned_total = sum(min(p.scanned_qty, p.planned_qty) for p in progress)
    overplan_total = sum(max(0, p.scanned_qty - p.planned_qty) for p in progress)
    lost_uids = [p.detail_uid for p in progress if not p.is_done]

    # Факты по UID, которых нет в плане операции (чужие детали / аномалии)
    planned_uids = set(plan.detail_uids)
    extra_uids = []
    for (op, uid), scanned in ctx.scanned.items():
        if op == plan.operation_1c and uid not in planned_uids and scanned > 0:
            extra_uids.append(uid)

    # Определение статуса
    if scanned_total == 0 and overplan_total == 0:
        status = OpStatus.NOT_STARTED
    elif scanned_total >= planned_total and not lost_uids:
        status = OpStatus.COMPLETED
    elif scanned_total < planned_total:
        status = OpStatus.IN_PROGRESS
    else:
        status = OpStatus.OVERPLAN

    return OperationStatus(
        order_num=ctx.order_num,
        operation_1c=plan.operation_1c,
        stage=plan.stage,
        status=status,
        planned_total=planned_total,
        scanned_total=scanned_total,
        overplan_total=overplan_total,
        details_progress=progress,
        lost_uids=lost_uids,
        extra_uids=extra_uids,
        updated_at=now,
    )


def to_1c_payload(s: OperationStatus) -> dict:
    """
    Сформировать payload для HTTP-запроса в 1С (см. архитектуру, решение 4).
    Только когда операция готова к закрытию — остальное не отправляется.
    """
    return {
        "order_num": s.order_num,
        "operation": s.operation_1c,
        "status": "Выполнено" if s.is_closing else None,
        "completed_at": s.updated_at.isoformat() if s.is_closing else None,
        "scanned_details": s.scanned_total,
        "planned_details": s.planned_total,
    }


def format_status_report(statuses: list[OperationStatus]) -> str:
    """Человекочитаемый отчёт по всем операциям заказа."""
    lines = [f"=== СТАТУС ОПЕРАЦИЙ: заказ {statuses[0].order_num} ===", ""]
    ICON = {
        OpStatus.NOT_STARTED: "⚪",
        OpStatus.IN_PROGRESS: "🔵",
        OpStatus.COMPLETED:   "✅",
        OpStatus.OVERPLAN:    "🟠",
        OpStatus.LOST:        "🔴",
    }
    for s in statuses:
        lines.append(f"{ICON[s.status]} {s.operation_1c}")
        if s.planned_total > 0:
            lines.append(f"     {s.scanned_total}/{s.planned_total} шт "
                         f"({s.progress_pct}%) | сверх плана: {s.overplan_total}")
            if s.lost_uids:
                lines.append(f"     ⚠ не отсканировано: {len(s.lost_uids)} деталей (UID)")
            if s.extra_uids:
                lines.append(f"     ⚠ чужих/лишних: {len(s.extra_uids)} деталей")
        else:
            lines.append(f"     позаказная (не через сканер)")
        if s.is_closing:
            lines.append(f"     → READY для записи в 1С")
        lines.append("")
    closing = sum(1 for s in statuses if s.is_closing)
    lines.append(f"ИТОГО: {closing}/{len(statuses)} операций готовы к закрытию в 1С")
    return "\n".join(lines)
