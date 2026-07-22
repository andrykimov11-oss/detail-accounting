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
from dataclasses import dataclass, field, replace
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
                     operations_1c: list[str],
                     rules: dict | None = None) -> tuple[list[OperationPlan], list[str]]:
    """
    Построить плановый состав операций по заказу.

    Аргументы:
        details        — детали заказа (из .xbir)
        operations_1c  — список имён операций цеха из 1С по этому заказу
        rules          — правила из справочника 1С (operation_rules.build_rules).
                         Если не переданы, используется встроенный набор из
                         operation_mapping — это режим отладки, покрывающий
                         12 операций из 227.

    Возвращает:
        (plans, warnings)
        plans    — список OperationPlan (по одной на операцию 1С)
        warnings — операции без правила
    """
    if rules is not None:
        return _build_plan_from_rules(details, operations_1c, rules)

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


def _build_plan_from_rules(details: list[Detail], operations_1c: list[str],
                           rules: dict) -> tuple[list[OperationPlan], list[str]]:
    """
    Плановый состав по правилам из справочника 1С (operation_rules).

    Отличия от встроенного набора:
      • покрыты все 227 операций цеха, а не 12;
      • тарифные надбавки («свыше 30 м.п.», «глянец») плана не берут —
        деталь считается один раз учётной операцией группы;
      • операции, у которых характеристика не извлекается из названия,
        помечаются позаказными, а не отбирают случайные детали.
    """
    plans: list[OperationPlan] = []
    warnings: list[str] = []

    # Дедупликация плана внутри заказа.
    #
    # Несколько операций могут иметь одинаковый предикат («Облицовывание
    # кромки 19/2» и «35/2» обе берут кромку 2 мм — толщина ДСП из названия
    # не применяется, таких толщин в .xbir нет). Если каждая заберёт полный
    # состав, план раздуется вдвое и операция НИКОГДА не закроется:
    # отсканировано всегда будет меньше планового.
    #
    # Поэтому детали засчитываются один раз — первой операции группы.
    # Базовой считается операция с самым коротким названием: «Облицовывание
    # кромки 19/2» короче, чем «Облицовывание криволинейных деталей 19/2».
    # Это эвристика: точное разделение (какие детали криволинейные, какие
    # узкие) требует признака от технолога, в .xbir его нет.
    claimed: dict[tuple, str] = {}
    claimed_details: dict[str, set] = {}
    for op_name in sorted(operations_1c, key=lambda n: (len(n), n)):
        rule = rules.get(op_name)
        if rule is not None and rule.is_per_detail:
            claimed.setdefault(rule.signature, op_name)

    from operation_rules import NON_SHOP_OPERATIONS  # noqa: PLC0415

    for op_name in sorted(operations_1c, key=lambda n: (len(n), n)):
        rule = rules.get(op_name)

        if op_name in NON_SHOP_OPERATIONS:
            plans.append(OperationPlan(
                operation_1c=op_name, stage="non_shop", counted_as="instances",
                is_per_detail=False,
                note="нецеховая услуга — в учёте загрузки не участвует",
            ))
            continue

        if rule is None:
            plans.append(OperationPlan(
                operation_1c=op_name, stage="unknown", counted_as="instances",
                is_per_detail=False,
                note="операции нет в справочнике 1С",
            ))
            warnings.append(op_name)
            continue

        if rule.is_tariff:
            # Надбавка не берёт план — но только если учётная операция группы
            # тоже есть в заказе. Иначе деталь не посчитал бы никто: заказ
            # содержит «Раскрой 16мм (свыше 30 м.п.)» без «Раскрой 16мм»,
            # и участок раскроя остался бы без плана.
            primary_present = (rule.primary_operation
                               and rule.primary_operation in operations_1c)
            if primary_present:
                plans.append(OperationPlan(
                    operation_1c=op_name, stage=rule.stage,
                    counted_as="instances", is_per_detail=False,
                    note=f"тарифная надбавка — закрывается вместе "
                         f"с «{rule.primary_operation}»",
                ))
                continue
            # Учётной операции в заказе нет — надбавка считается за неё
            rule = replace(rule, is_tariff=False, is_per_detail=True,
                           note="учётной операции группы нет в заказе — "
                                "план считает эта операция")

        if not rule.is_per_detail:
            plans.append(OperationPlan(
                operation_1c=op_name, stage=rule.stage, counted_as="instances",
                is_per_detail=False,
                note=rule.note or "позаказная операция",
            ))
            continue

        # Детали этой группы уже засчитаны другой операцией заказа
        owner = claimed.get(rule.signature)
        if owner and owner != op_name:
            plans.append(OperationPlan(
                operation_1c=op_name, stage=rule.stage, counted_as="instances",
                is_per_detail=False,
                note=f"те же детали, что у «{owner}» — учтены там, "
                     f"чтобы план не задвоился",
            ))
            continue

        subset = [d for d in details if rule.matches(d)]

        # На раскрое, фрезеровании и присадке деталь проходит участок один
        # раз, поэтому несколько операций одного участка не могут считать её
        # повторно («Раскрой плиты 16мм» и «Рез ЛДСП 10, 16мм» пересекаются
        # по деталям 16 мм). На кромлении иначе: деталь с кромками 0,4 и 2
        # законно проходит две операции, там разделение идёт по предикату.
        if rule.stage != "edging":
            already = claimed_details.setdefault(rule.stage, set())
            subset = [d for d in subset if d.detail_uid not in already]
            already.update(d.detail_uid for d in subset)
        if not subset:
            plans.append(OperationPlan(
                operation_1c=op_name, stage=rule.stage, counted_as="instances",
                is_per_detail=False,
                note="подетальная операция, но подходящих деталей в заказе нет",
            ))
            continue

        uids = sorted({d.detail_uid for d in subset})
        plans.append(OperationPlan(
            operation_1c=op_name,
            stage=rule.stage,
            counted_as="instances",
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
