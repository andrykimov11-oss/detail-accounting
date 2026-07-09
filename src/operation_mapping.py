"""
Маппинг операций цеха 1С → правила расчёта планового количества деталей.

Контекст см. docs/architecture.md (решение 3: статус операции выводится
из факта по деталям). Чтобы операция автоматически получила статус
«Выполнено», нужно знать плановое количество деталей, которые должны её
пройти. Это правило задано здесь — по операции.

Конфиг вынесен отдельно от кода, чтобы технолог мог менять правила
без правки логики.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Импортируем модель детали (без относительного импорта — для простоты запуска)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from xbir_parser import Detail  # noqa: E402


# --- Тип предиката: функция (Detail) -> bool -------------------------------
DetailPredicate = Callable[[Detail], bool]


@dataclass(frozen=True)
class OperationRule:
    """
    Правило связки операции цеха (строка из 1С) с деталями (.xbir).

    operation_1c     — точное имя операции в 1С (для сопоставления строк)
    stage            — укрупнённая стадия: 'cutting' | 'edging' | 'drilling'
                       | 'packing' (для группировки в учёте)
    detail_predicate — какие детали попадают в плановый состав операции
    counted_as       — что считаем плановым количеством:
                       'instances' — кол-во физических деталей (Σ qty)
                       'unique'    — кол-во уникальных detail_uid
    note             — комментарий технолога
    """
    operation_1c: str
    stage: str
    detail_predicate: DetailPredicate
    counted_as: str = "instances"
    note: str = ""


# --- Предикаты --------------------------------------------------------------

def _thickness(t: int) -> DetailPredicate:
    """Детали из ДСП заданной толщины (для операций раскроя)."""
    return lambda d: d.thickness == t


def _has_edge(t: float) -> DetailPredicate:
    """Детали, у которых хотя бы с одной стороны есть кромка толщиной t."""
    def pred(d: Detail) -> bool:
        return any(getattr(d, c) == t
                   for c in ("edge_l1", "edge_l2", "edge_w1", "edge_w2"))
    return pred


# --- Реестр правил ----------------------------------------------------------
# ВНИМАНИЕ: ключ точного сопоставления — ИМЯ операции из 1С.
# Имена взяты из анализа отчёта «производственные операции.xlsx».
# Если в 1С добавится новая операция — добавить правило сюда.

OPERATION_RULES: list[OperationRule] = [
    # --- Раскрой (по толщине ДСП) ---
    OperationRule(
        operation_1c="Раскрой плиты 10мм",
        stage="cutting",
        detail_predicate=_thickness(10),
        note="все детали 10мм",
    ),
    OperationRule(
        operation_1c="Раскрой плиты 16мм",
        stage="cutting",
        detail_predicate=_thickness(16),
        note="все детали 16мм",
    ),
    OperationRule(
        operation_1c="Раскрой плиты 16мм (свыше 30 м.п.)",
        stage="cutting",
        detail_predicate=_thickness(16),
        note="тарифная надбавка: те же детали 16мм, отдельного учёта не требует",
    ),

    # --- Кромление (по толщине кромки) ---
    OperationRule(
        operation_1c="Облицовывание кромки 19/0,8",
        stage="edging",
        detail_predicate=_has_edge(0.8),
        note="детали с кромкой 0,8мм; '19' — толщина ДСП в типовом",
    ),
    OperationRule(
        operation_1c="Облицовывание кромки 19/0,4",
        stage="edging",
        detail_predicate=_has_edge(0.4),
    ),
    OperationRule(
        operation_1c="Облицовывание кромки 19/2",
        stage="edging",
        detail_predicate=_has_edge(2.0),
    ),
    OperationRule(
        operation_1c="Облицовывание кромки 35/2",
        stage="edging",
        detail_predicate=_has_edge(2.0),
        note="шаблон: в заказе Спецторг деталей 35мм нет — план=0",
    ),

    # --- Документооборотные операции: НЕ подетальные (статус по заказу) ---
    # Для них detail_predicate всегда ложный → плановое количество = 0.
    # Статус ставится отдельно (руками или по факту выдачи), не через сканер.
    OperationRule(
        operation_1c="Операция ОТК",
        stage="quality",
        detail_predicate=lambda d: False,
        note="позаказная, не подетальная",
    ),
    OperationRule(
        operation_1c="Приемка на СГП",
        stage="warehouse",
        detail_predicate=lambda d: False,
        note="позаказная",
    ),
    OperationRule(
        operation_1c="Упаковка паллета",
        stage="packing",
        detail_predicate=lambda d: False,
        note="над паллетом, не над деталью",
    ),
    OperationRule(
        operation_1c="Упаковка раскроя",
        stage="packing",
        detail_predicate=lambda d: False,
        note="позаказная",
    ),
    OperationRule(
        operation_1c="Передача клиенту",
        stage="delivery",
        detail_predicate=lambda d: False,
        note="позаказная",
    ),
]


def find_rule(operation_1c: str) -> OperationRule | None:
    """Найти правило по точному имени операции. None — операция неизвестна."""
    for r in OPERATION_RULES:
        if r.operation_1c == operation_1c:
            return r
    return None
