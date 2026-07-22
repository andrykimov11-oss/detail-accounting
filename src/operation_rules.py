"""
Построение правил отбора деталей из справочника операций 1С.

Заменяет захардкоженный список `operation_mapping.OPERATION_RULES` (12 правил)
на разбор реального справочника цеха: **227 операций, 6 участков**.

## Как в названиях закодированы характеристики

Названия операций 1С содержат характеристики, по которым отбираются детали:

    «Раскрой плиты 16мм»              → детали толщиной 16 мм
    «Раскрой плиты 22мм и 26мм»       → детали 22 или 26 мм
    «Облицовывание кромки 19/0,4»     → детали с кромкой 0,4 мм
    «Облицовывание кромки 26,29/2мм»  → детали с кромкой 2 мм

Формат кромления — «толщина ДСП / толщина кромки».

## Что проверено на данных

**Толщина кромки — надёжный признак.** На 20 заказах, где операция кромления
ровно одна, толщина кромки в деталях `.xbir` совпала с названием операции во
всех 20 случаях (0,4 → только 0,4; 0,8 → только 0,8; 2 → только 2).

**Толщина ДСП из названия кромления — НЕ применяется как фильтр.** В названиях
стоят номинальные значения (19, 26, 29, 35), которых в `.xbir` не существует:
фактические толщины 3, 4, 10, 16, 18, 22 мм. Соответствие номинала фактическому
значению — вопрос к технологу, а не к коду.

## Коллизии правил

Из-за этого две операции могут получить одинаковый предикат:

    «Облицовывание кромки 19/2»  →  кромка 2 мм
    «Облицовывание кромки 35/2»  →  кромка 2 мм

В 498 заказах (13% заказов с несколькими операциями кромления) такие пары
встречаются вместе, и план по кромке задвоился бы.

Система **обнаруживает коллизию и сообщает о ней**, а не молча удваивает
план. Разрешает её технолог, задав различающий признак в справочнике
(`storage.upsert_operation_rule`) — например, диапазон толщин детали.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from xbir_parser import Detail

# --- Участки цеха (из выгрузки «Операции по участкам») -----------------------

# Участки, где учёт идёт подетально (деталь физически проходит операцию).
PER_DETAIL_AREAS = ("Раскрой", "Кромление", "Присадка", "Фрезерование")

# Участки документооборота: статус позаказный, сканер не нужен.
ORDER_LEVEL_AREAS = ("Склад готовой продукции", "Сборка")

# Нецеховые услуги: в учёте загрузки участков не участвуют вообще.
# Их нет в справочнике «Операции по участкам» и не должно быть — это
# не операции над деталью, а сопутствующие услуги (решение заказчика).
NON_SHOP_OPERATIONS = (
    "Экспедиторские услуги",
    "Кромка в отмотку",
    "Услуга Дизайнера",
)

# Участок → код стадии для группировки в учёте
AREA_TO_STAGE = {
    "Раскрой": "cutting",
    "Кромление": "edging",
    "Присадка": "drilling",
    "Фрезерование": "milling",
    "Сборка": "assembly",
    "Склад готовой продукции": "warehouse",
}

# --- Разбор характеристик из названия операции -------------------------------

# Кромление: «19/0,4», «26,29/2мм», «35/2» — толщина кромки идёт после «/»
EDGE_AFTER_SLASH_RE = re.compile(r"/\s*(\d+(?:[.,]\d+)?)")

# Кромление без дроби: «Облицовывание прямолинейной кромки 0,4»
EDGE_PLAIN_RE = re.compile(r"кромк\w*\s+(\d+(?:[.,]\d+)?)\s*(?:мм)?\s*$", re.I)

# Раскрой: «плиты 16мм», «10, 16мм», «22мм и 26мм»
THICKNESS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:,\s*(\d+(?:[.,]\d+)?)\s*)?мм", re.I)

# Толщины кромки, реально встречающиеся в .xbir
KNOWN_EDGE_THICKNESS = (0.4, 0.8, 1.0, 2.0)

# Толщины ДСП, реально встречающиеся в .xbir
KNOWN_PANEL_THICKNESS = (3, 4, 10, 16, 18, 22)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def parse_edge_thickness(operation: str) -> Optional[float]:
    """
    Достать толщину кромки из названия операции кромления.

        «Облицовывание кромки 19/0,4»       → 0.4
        «Облицовывание кромки 26,29/2мм»    → 2.0
        «Облицовывание криволинейных 19/04» → 0.4   (опечатка «04»)
        «Облицовывание прямолинейной кромки 0,4» → 0.4
        «Облицовывание кромки столешницы»   → None  (толщина не указана)

    Возвращает None, если толщину определить нельзя — такая операция не
    получает подетального правила и остаётся позаказной.
    """
    m = EDGE_AFTER_SLASH_RE.search(operation)
    if m:
        raw = m.group(1)

        # «19/04» — потерянная запятая в справочнике: 04 → 0,4
        if raw.startswith("0") and "." not in raw and "," not in raw and len(raw) > 1:
            candidate = _to_float("0." + raw[1:])
            if candidate in KNOWN_EDGE_THICKNESS:
                return candidate

        value = _to_float(raw)
        return value if value in KNOWN_EDGE_THICKNESS else None

    m = EDGE_PLAIN_RE.search(operation)
    if m:
        value = _to_float(m.group(1))
        return value if value in KNOWN_EDGE_THICKNESS else None

    return None


def parse_panel_thickness(operation: str) -> tuple[int, ...]:
    """
    Достать толщины ДСП из названия операции раскроя.

        «Раскрой плиты 16мм»               → (16,)
        «Раскрой плиты 22мм и 26мм»        → (22,)      — 26 нет в .xbir
        «Рез ЛДСП 10, 16мм (по высоте)»    → (10, 16)
        «Раскрой ДВПО / ХДФО»              → ()         — толщина не указана

    Оставляются только толщины, реально встречающиеся в `.xbir`.
    Пустой кортеж означает: правило по толщине не построить.
    """
    found: list[int] = []
    for m in THICKNESS_RE.finditer(operation):
        for group in m.groups():
            if not group:
                continue
            value = _to_float(group)
            if value.is_integer() and int(value) in KNOWN_PANEL_THICKNESS:
                found.append(int(value))

    # «Рез ЛДСП 10, 16мм» — первое число не перед «мм», ловим отдельно
    if "мм" in operation.lower():
        for m in re.finditer(r"(\d+)\s*,\s*(\d+)\s*мм", operation):
            for g in m.groups():
                v = int(g)
                if v in KNOWN_PANEL_THICKNESS:
                    found.append(v)

    return tuple(sorted(set(found)))


# --- Модель правила ----------------------------------------------------------

DetailPredicate = Callable[[Detail], bool]


# --- Тарифные надбавки -------------------------------------------------------

# Маркеры в названии, означающие тарифную разновидность той же самой работы:
# деталь физически обрабатывается один раз, а операций в заказе несколько.
# «Раскрой плиты 16мм» и «Раскрой плиты 16мм (свыше 30 м.п.)» — одна и та же
# деталь, вторая строка лишь надбавка за объём.
TARIFF_MARKERS = (
    "свыше",            # надбавка за объём: «свыше 30 м.п.», «свыше 40 м.п.»
    "сложн",            # надбавка за сложность
    "глянец",           # надбавка за материал
    "глянцевая",
    "матовый",
    "матовая",
    "сборка",           # та же операция в контуре сборки
    "черновой",
    "нанесение клея",   # подготовительная операция кромления
)


def is_tariff_variant(operation: str) -> bool:
    """
    Тарифная разновидность — не самостоятельная операция над деталью.

    Такие операции не забирают плановый состав: иначе одна деталь попала бы
    в план дважды. Статус они получают вместе с учётной операцией группы.
    """
    low = operation.lower()
    return any(marker in low for marker in TARIFF_MARKERS)


@dataclass
class OperationRule:
    """Правило отбора деталей для одной операции цеха."""
    operation_1c: str
    area: str
    stage: str
    is_per_detail: bool = False
    edge_thickness: Optional[float] = None
    panel_thickness: tuple[int, ...] = ()
    is_tariff: bool = False          # тарифная надбавка, плана не имеет
    primary_operation: str = ""      # учётная операция группы (для надбавок)
    note: str = ""

    @property
    def signature(self) -> tuple:
        """
        Отпечаток предиката. Две операции с одинаковым отпечатком отберут
        одни и те же детали — это коллизия, ведущая к задвоению плана.
        """
        return (self.stage, self.edge_thickness, self.panel_thickness)

    def matches(self, d: Detail) -> bool:
        """Попадает ли деталь в плановый состав операции."""
        if not self.is_per_detail:
            return False

        if self.edge_thickness is not None:
            has_edge = any(
                getattr(d, c) == self.edge_thickness
                for c in ("edge_l1", "edge_l2", "edge_w1", "edge_w2")
            )
            if not has_edge:
                return False

        if self.panel_thickness and d.thickness not in self.panel_thickness:
            return False

        return True


@dataclass
class RulesReport:
    """Итог построения правил из справочника."""
    operations_total: int = 0
    per_detail: int = 0
    tariff: int = 0
    order_level: int = 0
    no_predicate: list[str] = field(default_factory=list)
    unknown_area: list[str] = field(default_factory=list)
    areas: dict[str, int] = field(default_factory=dict)


def build_rules(area_map: dict[str, str]) -> tuple[dict[str, OperationRule], RulesReport]:
    """
    Построить правила из справочника «операция → участок» (выгрузка 1С).

    Аргументы:
        area_map — {название операции: название участка}

    Операция получает подетальное правило, только если участок подетальный
    И из названия удалось извлечь характеристику. Иначе она остаётся
    позаказной: лучше считать её вручную, чем отобрать не те детали.
    """
    rules: dict[str, OperationRule] = {}
    report = RulesReport(operations_total=len(area_map))

    for operation, area in sorted(area_map.items()):
        stage = AREA_TO_STAGE.get(area)
        if stage is None:
            report.unknown_area.append(operation)
            stage = "unknown"

        report.areas[area] = report.areas.get(area, 0) + 1

        rule = OperationRule(operation_1c=operation, area=area, stage=stage)

        if area in ORDER_LEVEL_AREAS:
            rule.note = "позаказная операция (участок документооборота)"
            report.order_level += 1
            rules[operation] = rule
            continue

        if area == "Кромление":
            rule.edge_thickness = parse_edge_thickness(operation)
        elif area == "Раскрой":
            rule.panel_thickness = parse_panel_thickness(operation)

        has_predicate = (rule.edge_thickness is not None or rule.panel_thickness)
        if not has_predicate:
            rule.note = "характеристика не извлечена из названия — считать позаказно"
            report.no_predicate.append(operation)
            report.order_level += 1
            rules[operation] = rule
            continue

        if is_tariff_variant(operation):
            # Тарифная надбавка: предикат есть, но плановый состав не берёт —
            # деталь уже посчитана учётной операцией группы.
            rule.is_tariff = True
            rule.note = "тарифная надбавка — план берёт учётная операция группы"
            report.tariff += 1
        else:
            rule.is_per_detail = True
            report.per_detail += 1

        rules[operation] = rule

    _assign_primary_operations(rules)
    return rules, report


def _assign_primary_operations(rules: dict[str, OperationRule]) -> None:
    """
    Привязать тарифные надбавки к учётной операции их группы.

    Группа — операции с одинаковым предикатом. Учётной становится
    единственная неторифная операция группы; если таких нет или несколько,
    привязка не делается и коллизия остаётся видимой в отчёте.
    """
    groups: dict[tuple, list[OperationRule]] = {}
    for r in rules.values():
        if r.is_per_detail or r.is_tariff:
            groups.setdefault(r.signature, []).append(r)

    for members in groups.values():
        primary = [r for r in members if r.is_per_detail]
        if len(primary) != 1:
            continue
        for r in members:
            if r.is_tariff:
                r.primary_operation = primary[0].operation_1c


def find_collisions(rules: Sequence[OperationRule]) -> dict[tuple, list[str]]:
    """
    Найти операции с одинаковым предикатом.

    Такие операции отберут одни и те же детали. Если обе попадут в один
    заказ, план задвоится: «Облицовывание кромки 19/2» и «35/2» обе
    заберут детали с кромкой 2 мм.

    Возвращает {отпечаток: [операции]} только для групп длиннее одной.
    """
    groups: dict[tuple, list[str]] = {}
    for r in rules:
        # Тарифные надбавки плана не берут — в коллизиях не участвуют
        if not r.is_per_detail:
            continue
        groups.setdefault(r.signature, []).append(r.operation_1c)
    return {sig: ops for sig, ops in groups.items() if len(ops) > 1}


def format_rules_report(report: RulesReport,
                        collisions: dict[tuple, list[str]] | None = None) -> str:
    """Человекочитаемый отчёт о построенных правилах."""
    lines = ["=== ПРАВИЛА ОТБОРА ДЕТАЛЕЙ ИЗ СПРАВОЧНИКА 1С ===", ""]
    lines.append(f"Операций в справочнике: {report.operations_total}")
    lines.append(f"  учётных подетальных:  {report.per_detail}")
    lines.append(f"  тарифных надбавок:    {report.tariff}")
    lines.append(f"  позаказных:           {report.order_level}")
    lines.append("")

    lines.append("Участки:")
    for area, count in sorted(report.areas.items(), key=lambda x: -x[1]):
        mark = "подетально" if area in PER_DETAIL_AREAS else "позаказно"
        lines.append(f"  {count:>4}  {area}  ({mark})")
    lines.append("")

    if report.no_predicate:
        lines.append(f"Без характеристики в названии: {len(report.no_predicate)}")
        for op in report.no_predicate[:12]:
            lines.append(f"      • {op}")
        if len(report.no_predicate) > 12:
            lines.append(f"      … ещё {len(report.no_predicate) - 12}")
        lines.append("")

    if collisions:
        lines.append(f"⚠ КОЛЛИЗИИ ПРЕДИКАТОВ: {len(collisions)} групп")
        lines.append("  Операции ниже отберут одни и те же детали. Если обе")
        lines.append("  попадут в заказ, план задвоится — нужен различающий")
        lines.append("  признак от технолога.")
        for sig, ops in list(collisions.items())[:8]:
            lines.append(f"\n    предикат {sig}:")
            for op in ops:
                lines.append(f"      • {op}")
        lines.append("")

    return "\n".join(lines)
