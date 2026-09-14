"""
Натуральные измерители участков (SR-26а — SR-26е).

ЗАЧЕМ
    Выработку участка нельзя мерить числом отметок: отметка на раскрое и
    отметка на сборке — разная работа. Поэтому у каждого участка есть свой
    натуральный измеритель, и выработка считается в нём (BR-42).

ДВА СВОЙСТВА, КОТОРЫЕ ЗДЕСЬ ОБЕСПЕЧИВАЮТСЯ

    SR-26а. У участка РОВНО ОДИН измеритель. Это свойство схемы, а не
    дисциплины: area_id — первичный ключ справочника, второй строки для
    участка физически не существует.

    SR-26б. Значение измерителя вычисляется ИЗ СПЕЦИФИКАЦИИ БАЗИС до
    начала работы и не зависит от отметок оператора. Поэтому все функции
    ниже читают `details` — плановый состав заказа — и не смотрят ни в
    scan_events, ни в facts. Оператор не может изменить выработку тем,
    как он отмечает.

ПОЧЕМУ ЭТО ВАЖНЕЕ, ЧЕМ КАЖЕТСЯ
    Подсветка отклонения (шаг 2) сравнивает выработку со скользящей
    медианой участка. Если измеритель зависел бы от отметок, участок мог
    бы влиять на собственный норматив способом отметки. Требование SR-26б
    закрывает это не проверкой, а источником данных.

ЧЕГО ЗДЕСЬ НЕТ
    Справочник НЕ заполняется кодом. Его заполняет технолог (пункт 0.3
    шага 0, лист `SETUP-001`). Здесь — только хранение, правило «ровно
    один» и вычисление значения. Значения по умолчанию не проставляются
    намеренно: измеритель участка есть решение предприятия, а не догадка
    системы.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


# Коды измерителей. Строка, а не Enum: справочник заполняет технолог,
# и набор может пополниться без правки кода.
SHEETS = "sheets"                 # листы ЛДСП — раскрой
EDGE_METERS = "edge_meters"       # погонные метры кромки — кромление
UNIQUE_DETAILS = "unique_details"  # уникальные детали — фрезерование, склад
DETAIL_ITEMS = "detail_items"     # детали с количеством — присадка
PRODUCTS = "products"             # изделия — сборка

MEASURE_NAMES = {
    SHEETS: "листы",
    EDGE_METERS: "пог. м кромки",
    UNIQUE_DETAILS: "уникальные детали",
    DETAIL_ITEMS: "детали с количеством",
    PRODUCTS: "изделия",
}


class MeasureNotSet(Exception):
    """
    Для участка не задан измеритель.

    Это не сбой программы, а незаполненный справочник: пункт 0.3 шага 0.
    Исключение названо отдельно, чтобы вызывающий код мог отличить
    «технолог ещё не заполнил» от «в данных ошибка».
    """

    def __init__(self, area_id: str):
        self.area_id = area_id
        super().__init__(
            f"Для участка «{area_id}» не задан натуральный измеритель "
            f"(SR-26а). Заполняется технологом, пункт 0.3 шага 0."
        )


class MeasureNotComputable(Exception):
    """Измеритель задан, но вычислить его по спецификации нечем."""

    def __init__(self, area_id: str, measure_code: str, why: str):
        super().__init__(
            f"Измеритель «{measure_code}» участка «{area_id}» "
            f"не вычисляется: {why}"
        )


@dataclass
class AreaMeasure:
    area_id: str
    measure_code: str
    measure_name: str


# ----------------------------------------------------------------------
# Вычисление по спецификации (SR-26б): читаем details, не отметки
# ----------------------------------------------------------------------
def _sheets(rows) -> float:
    """
    Раскрой: число листов ЛДСП.

    Считается по различным номерам плиты в спецификации. Номер плиты
    приходит из .xbir и означает физический лист, на котором размещены
    детали карты.
    """
    plates = {r["plate_no"] for r in rows if r["plate_no"]}
    if not plates:
        raise MeasureNotComputable(
            "raskroy", SHEETS,
            "в спецификации нет номеров плиты; вероятно, .xbir разобран "
            "версией парсера до того, как поле стало сохраняться")
    return float(len(plates))


def _edge_meters(rows) -> float:
    """
    Кромление: погонные метры кромки заказа.

    Длина кромки детали умножается на плановое количество экземпляров:
    оклеивается каждый экземпляр, а не позиция спецификации.
    Величина хранится в миллиметрах, приводится к метрам.
    """
    total_mm = sum((r["edge_total_len"] or 0) * (r["qty"] or 0) for r in rows)
    return round(total_mm / 1000.0, 2)


def _unique_details(rows) -> float:
    """Склад готовой продукции: число уникальных деталей заказа (SR-26е)."""
    return float(len({r["detail_uid"] for r in rows}))


def _unique_milled(rows) -> float:
    """
    Фрезерование: уникальные детали, ПРОХОДЯЩИЕ фрезерование (SR-26г).

    Не все детали заказа идут на фрезерование; признак — наличие пазов
    в спецификации. Считать весь заказ значило бы завысить выработку
    участка в разы.
    """
    return float(len({r["detail_uid"] for r in rows if (r["grooves"] or 0) > 0}))


def _detail_items(rows) -> float:
    """Присадка: детали с количеством — экземпляры, идущие на сверление."""
    return float(sum((r["qty"] or 0) for r in rows if (r["drill_total"] or 0) > 0))


def _products(rows) -> float:
    """
    Сборка: изделия заказа (SR-26д).

    Берётся из поля «Обозначение изделия» спецификации. Пустое значение
    в счёт не идёт: деталь без изделия не собирается.
    """
    codes = {r["product_code"] for r in rows if (r["product_code"] or "").strip()}
    if not codes:
        raise MeasureNotComputable(
            "sborka", PRODUCTS,
            "в спецификации не заполнено «Обозначение изделия»")
    return float(len(codes))


CALCULATORS: dict[str, Callable] = {
    SHEETS: _sheets,
    EDGE_METERS: _edge_meters,
    UNIQUE_DETAILS: _unique_details,
    "unique_milled": _unique_milled,
    DETAIL_ITEMS: _detail_items,
    PRODUCTS: _products,
}


class AreaMeasures:
    """Справочник измерителей участков и вычисление значений по заказу."""

    def __init__(self, storage):
        self.st = storage

    # -- справочник (SR-26а) -------------------------------------------
    def set_measure(self, area_id: str, measure_code: str,
                    measure_name: str = "") -> AreaMeasure:
        """
        Задать измеритель участка. Повторный вызов ЗАМЕНЯЕТ прежний,
        а не добавляет второй: у участка ровно один измеритель.
        """
        name = measure_name or MEASURE_NAMES.get(measure_code, measure_code)
        self.st.set_area_measure(area_id, measure_code, name)
        return AreaMeasure(area_id, measure_code, name)

    def get_measure(self, area_id: str) -> AreaMeasure:
        row = self.st.get_area_measure(area_id)
        if row is None:
            raise MeasureNotSet(area_id)
        return AreaMeasure(row["area_id"], row["measure_code"],
                           row["measure_name"])

    def all_measures(self) -> list[AreaMeasure]:
        return [AreaMeasure(r["area_id"], r["measure_code"], r["measure_name"])
                for r in self.st.get_area_measures()]

    def areas_without_measure(self, area_ids: list[str]) -> list[str]:
        """
        Участки, для которых технолог ещё не задал измеритель.

        Нужно для признака завершения пункта 0.3: «все шесть участков
        заполнены». Проверяется списком, а не на глаз.
        """
        have = {m.area_id for m in self.all_measures()}
        return [a for a in area_ids if a not in have]

    # -- вычисление (SR-26б) -------------------------------------------
    def value_for_order(self, area_id: str, order_num: int) -> float:
        """
        Значение измерителя участка для заказа — из спецификации БАЗИС.

        Ни одна отметка оператора на результат не влияет: читается только
        плановый состав заказа.
        """
        m = self.get_measure(area_id)
        calc = CALCULATORS.get(m.measure_code)
        if calc is None:
            raise MeasureNotComputable(
                area_id, m.measure_code,
                "правило вычисления для этого измерителя не задано")
        rows = self.st.get_details_by_order(order_num)
        if not rows:
            raise MeasureNotComputable(
                area_id, m.measure_code,
                f"спецификация заказа {order_num} не загружена")
        return calc(rows)
