"""
Тесты загрузчика выгрузок 1С (src/one_c_loader.py).

Отдельный акцент — **сопоставление колонок**. Резолвер связки может быть
безупречен, но если загрузчик подставит не ту колонку, связка не найдётся
никогда, причём молча. Именно так и произошло: поле «Клиент» подхватило
колонку «Заказ клиента», и сопоставление на реальном массиве дало 0
разрешённых заказов при полностью зелёных юнит-тестах логики.

Поэтому заголовок реальной выгрузки зафиксирован здесь как эталон.
"""
from __future__ import annotations

import pytest

from one_c_loader import (
    COLUMN_ALIASES,
    OneCOrder,
    _resolve_columns,
    parse_order_number,
)

# Заголовок реальной выгрузки «производственные операции New_1 (XLSX).xlsx».
# Содержит четыре колонки со словом «клиент» — источник ошибки сопоставления.
REAL_HEADER = [
    "Заказ клиента",
    "Менеджер",
    "Номер заказа",
    "Дата заказа",
    "Заказ клиента.Статус",
    "Клиент",
    "Срок изготовления",
    "Плановая дата выдачи",
    "Операция цеха",
    "Дата передачи в цех",
    "Статус",
    "Исполнитель",
    "Дата выполнения",
    "Комментарий",
    "Заказ клиента.Номер телефона для уведомления клиента (Рондо)",
    "Количество записей",
]


# --- Сопоставление колонок ---------------------------------------------------

def test_client_column_is_exact_not_substring():
    """
    «Клиент» должен указывать на колонку 5, а не на «Заказ клиента» (0),
    где лежит строка «Заказ клиента ЛД00-007936 от 30.07.2024».
    """
    idx, missing = _resolve_columns(REAL_HEADER)
    assert missing == []
    assert idx["client_name"] == 5
    assert REAL_HEADER[idx["client_name"]] == "Клиент"


def test_order_number_column():
    idx, _ = _resolve_columns(REAL_HEADER)
    assert REAL_HEADER[idx["order_full_num"]] == "Номер заказа"


def test_deadline_prefers_planned_date():
    """«Плановая дата выдачи» приоритетнее «Срока изготовления» (это дни)."""
    idx, _ = _resolve_columns(REAL_HEADER)
    assert REAL_HEADER[idx["deadline"]] == "Плановая дата выдачи"


def test_status_column_is_order_status():
    idx, _ = _resolve_columns(REAL_HEADER)
    assert REAL_HEADER[idx["order_status"]] == "Заказ клиента.Статус"


def test_all_mapped_columns_are_distinct():
    """Ни одна колонка не должна обслуживать два поля модели сразу."""
    idx, _ = _resolve_columns(REAL_HEADER)
    assert len(set(idx.values())) == len(idx)


def test_missing_required_columns_reported():
    idx, missing = _resolve_columns(["Номер заказа", "Операция цеха"])
    assert "client_name" in missing


def test_column_order_independence():
    """Порядок колонок в выгрузке может измениться — сопоставление по имени."""
    shuffled = list(reversed(REAL_HEADER))
    idx, missing = _resolve_columns(shuffled)
    assert missing == []
    assert shuffled[idx["client_name"]] == "Клиент"


# --- Разбор номера заказа ----------------------------------------------------

@pytest.mark.parametrize("raw,prefix,num", [
    ("ЛД00-006564", "ЛД00", 6564),
    ("ПС00-007730", "ПС00", 7730),
    ("0Ч00-000936", "0Ч00", 936),
])
def test_prefix_and_number_split(raw, prefix, num):
    assert parse_order_number(raw) == (prefix, num)


def test_number_without_prefix():
    assert parse_order_number("6564") == ("", 6564)


@pytest.mark.parametrize("raw", ["", "   ", "мусор", "ЛД00-"])
def test_unparseable_numbers(raw):
    assert parse_order_number(raw) is None


# --- Модель заказа -----------------------------------------------------------

def test_active_order():
    o = OneCOrder("ЛД00-006564", "ЛД00", 6564,
                  order_status="К выполнению / В резерве")
    assert o.is_active


def test_closed_order_is_not_active():
    o = OneCOrder("ЛД00-006564", "ЛД00", 6564, order_status="Закрыт")
    assert not o.is_active


def test_aliases_cover_required_fields():
    assert "order_full_num" in COLUMN_ALIASES
    assert "client_name" in COLUMN_ALIASES
