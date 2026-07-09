"""
Расчёт планового состава операции из деталей (.xbir).

Контекст: docs/architecture.md, решение 3. Статус операции «Выполнено»
выводится из факта по деталям. Чтобы его посчитать, нужно знать плановое
количество деталей, которые должны пройти операцию — это считает данный модуль.

Логика:
    1. Загружаем детали заказа (из .xbir через парсер).
    2. Для каждой операции цеха берём правило (operation_mapping).
    3. Фильтруем детали по предикату правила → плановый состав операции.
    4. Считаем плановое количество (штук экземпляров = Σ qty,
       либо уникальных detail_uid — по правилу).

Применение:
    from operation_plan import build_order_plan, format_plan_report
    plan = build_order_plan(details, operations_1c)
    print(format_plan_report(plan))
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from xbir_parser import Detail  # noqa: E402
from operation_mapping import OperationRule, find_rule  # noqa: E402


@dataclass
class OperationPlan:
    """Плановый состав одной операции цеха по заказу."""
    operation_1c: str
    stage: str
    counted_as: str
    planned_instances: int = 0      # Σ qty подходящих деталей
    planned_unique: int = 0         # кол-во уникальных detail_uid
    planned_area_m2: float = 0.0    # Σ площади (для оценки загрузки, м²)
    planned_edge_m: float = 0.0     # Σ кромки (для кромления, пог.м)
    detail_uids: list[str] = field(default_factory=list)
    note: str = ""
    is_per_detail: bool = True      # True — операция подетальная (статус из сканера)

    @property
    def planned_qty(self) -> int:
        """Плановое количество в единицах правила (instances или unique)."""
        return self.planned_instances if self.counted_as == "instances" else self.planned_unique


def _edge_meters(d: Detail) -> float:
    """Сумма длин сторон с кромкой, в погонных метрах."""
    total = 0.0
    if d.edge_l1 > 0:
        total += d.length
    if d.edge_l2 > 0:
        total += d.length
    if d.edge_w1 > 0:
        total += d.width
    if d.edge_w2 > 0:
        total += d.width
    return total / 1000.0


def build_order_plan(details: list[Detail],
                     operations_1c: list[str]) -> tuple[list[OperationPlan], list[str]]:
    """
    Построить плановый состав операций по заказу.

    Аргументы:
        details        — детали заказа (из .xbir)
        operations_1c  — список имён операций цеха из 1С по этому заказу

    Возвращает:
        (plans, warnings)
        plans    — список OperationPlan (по одной на операцию 1С)
        warnings — операции без правила (нужно добавить в operation_mapping)
    """
    plans: list[OperationPlan] = []
    warnings: list[str] = []

    for op_name in operations_1c:
        rule: OperationRule | None = find_rule(op_name)
        if rule is None:
            plans.append(OperationPlan(
                operation_1c=op_name,
                stage="unknown",
                counted_as="instances",
                is_per_detail=False,
                note="НЕТ ПРАВИЛА — операция не описана в operation_mapping",
            ))
            warnings.append(op_name)
            continue

        # Фильтруем детали по предикату правила
        subset = [d for d in details if rule.detail_predicate(d)]

        # Документооборотные операции (предикат всегда False) — план = 0
        if not subset:
            plans.append(OperationPlan(
                operation_1c=op_name,
                stage=rule.stage,
                counted_as=rule.counted_as,
                note=rule.note or "план=0 (позаказная операция)",
                is_per_detail=False,
            ))
            continue

        # Считаем плановые объёмы
        uids = sorted({d.detail_uid for d in subset})
        plans.append(OperationPlan(
            operation_1c=op_name,
            stage=rule.stage,
            counted_as=rule.counted_as,
            planned_instances=sum(d.qty for d in subset),
            planned_unique=len(uids),
            planned_area_m2=round(sum(d.area * d.qty for d in subset), 2),
            planned_edge_m=round(sum(_edge_meters(d) * d.qty for d in subset), 2),
            detail_uids=uids,
            note=rule.note,
            is_per_detail=True,
        ))

    return plans, warnings


def format_plan_report(order_num: int, plans: list[OperationPlan],
                       warnings: list[str]) -> str:
    """Человекочитаемый отчёт плана по операциям заказа."""
    lines = []
    lines.append(f"=== ПЛАН ПО ОПЕРАЦИЯМ: заказ {order_num} ===")
    lines.append("")

    for p in plans:
        flag = "🔒" if p.is_per_detail else "📋"
        lines.append(f"{flag} {p.operation_1c}")
        if p.is_per_detail:
            lines.append(f"     план: {p.planned_qty} шт "
                         f"({p.planned_unique} уникальных) | "
                         f"{p.planned_area_m2} м² | {p.planned_edge_m} пог.м кромки")
        else:
            lines.append(f"     {p.note}")
        lines.append("")

    if warnings:
        lines.append("⚠ ОПЕРАЦИИ БЕЗ ПРАВИЛА:")
        for w in warnings:
            lines.append(f"   • {w}")
        lines.append("")

    per_detail = [p for p in plans if p.is_per_detail]
    total_to_scan = sum(p.planned_qty for p in per_detail)
    lines.append(f"ИТОГО к сканированию (подетальные операции): {total_to_scan} шт")
    return "\n".join(lines)
