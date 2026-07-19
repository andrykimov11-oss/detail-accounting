"""
Общие фикстуры тестов.

Реальные данные цеха содержат ПД (ФИО клиентов) и в репозиторий не попадают
(см. .gitignore: samples/). Поэтому тесты работают на **синтетическом .xbir**,
который собирается здесь по той же структуре, что и выгрузка Базис-раскроя:
<Cols> с именованными колонками + <Rows> с таб-разделёнными значениями.

Это даёт воспроизводимость: тесты не зависят от наличия у разработчика
конкретного заказа.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from pipeline import ProductionCore  # noqa: E402
from scan_processor import ScanEvent  # noqa: E402
from storage import Storage  # noqa: E402
from xbir_parser import parse_xbir  # noqa: E402


# Колонки .xbir в порядке Index (подмножество из 128, достаточное для учёта)
XBIR_COLUMNS = [
    "ID детали", "Номер заказа", "Материал", "Артикул материала", "Толщина",
    "Длина детали", "Ширина детали", "Кол-во", "Периметр", "Площадь", "Объем",
    "Кромка L1 толщ.", "Кромка L2 толщ.", "Кромка W1 толщ.", "Кромка W2 толщ.",
    "Кол. скв. отв. в пласть", "Кол. гл. отв. в пласть", "Кол. отв. в торец",
    "Кол. прямол. пазов", "Кол. кривол. пазов",
    "Номер плиты", "Номер карты", "Номер позиции",
]

# UID'ы фиксированные — чтобы QR были стабильны между прогонами
UID_PANEL_16 = "AAAAAAAA-0001-0000-0000-000000000001"   # 16мм, кромка 2, qty=2
UID_SHELF_16 = "AAAAAAAA-0002-0000-0000-000000000002"   # 16мм, кромка 0,8, qty=3
UID_BACK_10 = "AAAAAAAA-0003-0000-0000-000000000003"    # 10мм, без кромки, qty=1


def qr(uid: str) -> str:
    return hashlib.md5(uid.encode()).hexdigest()[:10]


def build_xbir(path: Path, rows: list[dict], *, with_bom: bool = True,
               columns: list[str] | None = None) -> Path:
    """
    Собрать синтетический .xbir. rows — список словарей {имя колонки: значение}.
    Пропущенные колонки заполняются пустой строкой.
    """
    columns = columns or XBIR_COLUMNS
    cols_xml = "".join(
        f'<Col Name="{name}" Type="String" Index="{i + 1}"/>'
        for i, name in enumerate(columns)
    )
    rows_xml = "".join(
        "<Row>" + "\t".join(str(r.get(c, "")) for c in columns) + "</Row>"
        for r in rows
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Document><Version>1.0</Version><Cols>{cols_xml}</Cols>"
        f"<Rows>{rows_xml}</Rows></Document>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ("﻿" if with_bom else "") + xml
    path.write_text(payload, encoding="utf-8")
    return path


def detail_row(uid: str, *, order: str = "6564 Спецторг ООО", thickness: int = 16,
               length: int = 1000, width: int = 500, qty: int = 1,
               edge_l1: float = 0, edge_l2: float = 0,
               edge_w1: float = 0, edge_w2: float = 0,
               pos: str = "1", area: float = 0.5,
               material: str = "ЛДСП Дуб") -> dict:
    """Одна строка .xbir с осмысленными значениями по умолчанию."""
    return {
        "ID детали": uid,
        "Номер заказа": order,
        "Материал": material,
        "Артикул материала": "ART-001",
        "Толщина": thickness,
        "Длина детали": length,
        "Ширина детали": width,
        "Кол-во": qty,
        "Периметр": round(2 * (length + width) / 1000, 3),
        "Площадь": area,
        "Объем": 0.01,
        "Кромка L1 толщ.": edge_l1,
        "Кромка L2 толщ.": edge_l2,
        "Кромка W1 толщ.": edge_w1,
        "Кромка W2 толщ.": edge_w2,
        "Кол. скв. отв. в пласть": 0,
        "Кол. гл. отв. в пласть": 4,
        "Кол. отв. в торец": 2,
        "Кол. прямол. пазов": 1,
        "Кол. кривол. пазов": 0,
        "Номер плиты": 1,
        "Номер карты": 1,
        "Номер позиции": pos,
    }


# --- Фикстуры ----------------------------------------------------------------

@pytest.fixture
def order_rows() -> list[dict]:
    """
    Типовой заказ 6564: три детали, покрывающие все ветки правил операций.
      - панель 16мм с кромкой 2мм,  qty=2
      - полка  16мм с кромкой 0,8мм, qty=3
      - задняя стенка 10мм без кромки, qty=1
    """
    return [
        detail_row(UID_PANEL_16, thickness=16, length=1928, width=838, qty=2,
                   edge_l1=2, edge_l2=2, pos="1", area=1.616),
        detail_row(UID_SHELF_16, thickness=16, length=600, width=400, qty=3,
                   edge_l1=0.8, edge_l2=0.8, edge_w1=0.8, edge_w2=0.8,
                   pos="2", area=0.24),
        detail_row(UID_BACK_10, thickness=10, length=500, width=300, qty=1,
                   pos="3", area=0.15),
    ]


@pytest.fixture
def xbir_file(tmp_path: Path, order_rows: list[dict]) -> Path:
    return build_xbir(tmp_path / "order6564.xbir", order_rows)


@pytest.fixture
def details(xbir_file: Path):
    parsed, _ = parse_xbir(xbir_file)
    return parsed


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def core(storage: Storage) -> ProductionCore:
    c = ProductionCore(storage)
    c.seed_areas()
    return c


@pytest.fixture
def loaded_core(core: ProductionCore, xbir_file: Path) -> ProductionCore:
    """Ядро с уже импортированным заказом 6564."""
    core.import_xbir([xbir_file])
    return core


@pytest.fixture
def t0() -> datetime:
    """Опорное время. Тесты используют явные смещения, без реального now()."""
    return datetime(2026, 7, 19, 8, 0, 0)


def scan(uid: str, operation: str, at: datetime, *, area: str = "area_edging",
         scan_id: str | None = None, operator: str = "op_test",
         qr_code: str | None = None) -> ScanEvent:
    """Хелпер построения события сканера."""
    return ScanEvent(
        scan_id=scan_id or f"evt_{at.strftime('%H%M%S%f')}_{uid[:4]}",
        qr_code=qr_code if qr_code is not None else qr(uid),
        area_id=area,
        operator_id=operator,
        operation_1c=operation,
        scanned_at=at,
    )


__all__ = [
    "UID_PANEL_16", "UID_SHELF_16", "UID_BACK_10",
    "qr", "scan", "build_xbir", "detail_row", "XBIR_COLUMNS", "timedelta",
]
