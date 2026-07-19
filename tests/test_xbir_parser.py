"""
Тесты парсера .xbir (docs/xbir-parser-spec.md).

Отдельный акцент — устойчивость к «приблизительному» номеру заказа
и к изменению набора колонок между версиями Базиса. Это корневые риски
проекта, зафиксированные куратором.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    UID_BACK_10,
    UID_PANEL_16,
    UID_SHELF_16,
    XBIR_COLUMNS,
    build_xbir,
    detail_row,
)
from xbir_parser import _parse_order_num, parse_xbir


# --- Базовый разбор ----------------------------------------------------------

def test_parses_all_rows(xbir_file: Path):
    details, report = parse_xbir(xbir_file)
    assert report.rows_total == 3
    assert report.rows_parsed == 3
    assert report.errors == []
    assert len(details) == 3


def test_field_mapping_by_name(details):
    """Поля берутся по имени колонки, значения приводятся к типам."""
    panel = next(d for d in details if d.detail_uid == UID_PANEL_16)
    assert panel.order_num == 6564
    assert panel.thickness == 16
    assert panel.length == 1928
    assert panel.width == 838
    assert panel.qty == 2
    assert panel.material_name == "ЛДСП Дуб"
    assert panel.pos_no == "1"


def test_bom_is_handled(tmp_path: Path, order_rows):
    """UTF-8 BOM в начале файла не должен ломать ElementTree."""
    with_bom = build_xbir(tmp_path / "bom.xbir", order_rows, with_bom=True)
    without_bom = build_xbir(tmp_path / "nobom.xbir", order_rows, with_bom=False)
    assert len(parse_xbir(with_bom)[0]) == len(parse_xbir(without_bom)[0]) == 3


def test_derived_fields(details):
    """Производные поля считаются парсером, а не берутся из файла."""
    shelf = next(d for d in details if d.detail_uid == UID_SHELF_16)
    # кромка со всех 4 сторон: 2*длина + 2*ширина (в метрах)
    assert shelf.edge_total_len == pytest.approx(2 * 0.6 + 2 * 0.4, abs=1e-3)
    assert shelf.grooves == 1  # прямол. 1 + кривол. 0

    back = next(d for d in details if d.detail_uid == UID_BACK_10)
    assert back.edge_total_len == 0.0  # кромки нет


# --- Номер заказа: корневой риск проекта ------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("6564 Спецторг ООО", 6564),
    ("6564", 6564),
    ("ЛД00-006564", 0),        # regex берёт ПЕРВОЕ число — это поведение зафиксировано
    ("0006564 Клиент", 6564),  # ведущие нули срезаются приведением к int
    ("Заказ 7706 от 12.05", 7706),
])
def test_order_num_extraction(raw: str, expected: int):
    """
    Номер заказа переносится в Базис «приблизительно» (без нулей и префиксов).
    Тест фиксирует фактическое поведение регулярки, включая уязвимый кейс
    'ЛД00-006564' → 0: полный номер документа 1С парсится НЕВЕРНО.
    """
    assert _parse_order_num(raw) == expected


def test_order_num_prefix_case_is_a_known_gap():
    """
    Явная фиксация разрыва для Этапа B: если в .xbir попадёт полный номер
    документа 1С вида 'ЛД00-006564', связка заказ↔раскрой сломается тихо.
    Нормализатор номера — задача интегратора 1С.
    """
    assert _parse_order_num("ЛД00-006564") != 6564


def test_row_without_order_num_is_rejected(tmp_path: Path):
    rows = [detail_row(UID_PANEL_16, order="без номера")]
    f = build_xbir(tmp_path / "bad.xbir", rows)
    details, report = parse_xbir(f)
    assert details == []
    assert any("order_num" in e for e in report.errors)


# --- Валидация строк ---------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("Кол-во", 0),
    ("Длина детали", 0),
    ("Ширина детали", 0),
    ("Толщина", 0),
])
def test_invalid_dimensions_rejected(tmp_path: Path, field: str, value: int):
    row = detail_row(UID_PANEL_16)
    row[field] = value
    f = build_xbir(tmp_path / "invalid.xbir", [row])
    details, report = parse_xbir(f)
    assert details == []
    assert len(report.errors) == 1


def test_empty_uid_rejected(tmp_path: Path):
    row = detail_row("")
    f = build_xbir(tmp_path / "nouid.xbir", [row])
    details, report = parse_xbir(f)
    assert details == []
    assert any("detail_uid" in e for e in report.errors)


def test_valid_rows_survive_invalid_neighbours(tmp_path: Path, order_rows):
    """Одна битая строка не должна ронять разбор всего файла."""
    bad = detail_row(UID_PANEL_16, qty=0)
    f = build_xbir(tmp_path / "mixed.xbir", order_rows + [bad])
    details, report = parse_xbir(f)
    assert report.rows_total == 4
    assert report.rows_parsed == 3
    assert len(report.errors) == 1


# --- Устойчивость к смене версии Базиса -------------------------------------

def test_missing_columns_reported_not_fatal(tmp_path: Path):
    """Набор колонок урезан — парсер продолжает работу и сообщает о нехватке."""
    reduced = [c for c in XBIR_COLUMNS if c not in ("Периметр", "Объем")]
    f = build_xbir(tmp_path / "reduced.xbir", [detail_row(UID_PANEL_16)],
                   columns=reduced)
    details, report = parse_xbir(f)
    assert len(details) == 1
    assert set(report.missing_columns) == {"Периметр", "Объем"}


def test_unknown_columns_are_audited(tmp_path: Path):
    """Новые колонки Базиса игнорируются, но фиксируются в отчёте."""
    extended = XBIR_COLUMNS + ["Новая колонка Базиса"]
    f = build_xbir(tmp_path / "extended.xbir", [detail_row(UID_PANEL_16)],
                   columns=extended)
    details, report = parse_xbir(f)
    assert len(details) == 1
    assert "Новая колонка Базиса" in report.unknown_columns


def test_column_order_independence(tmp_path: Path):
    """Парсер работает по имени колонки, а не по позиции."""
    shuffled = list(reversed(XBIR_COLUMNS))
    f = build_xbir(tmp_path / "shuffled.xbir", [detail_row(UID_PANEL_16, qty=7)],
                   columns=shuffled)
    details, _ = parse_xbir(f)
    assert len(details) == 1
    assert details[0].qty == 7
    assert details[0].detail_uid == UID_PANEL_16
