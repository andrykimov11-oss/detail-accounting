"""
Парсер .xbir → нормализованная модель детали.

Спецификация: docs/xbir-parser-spec.md (v0.1)

.xbir — XML-выгрузка программы «Базис-раскрой». Содержит описание 128 колонок
в <Cols> и таб-разделённые строки данных в <Rows>/<Row>. Парсер работает по
имени колонки (не по позиции), устойчив к изменению набора колонок между
версиями Базиса.

Применение:
    python src/xbir_parser.py <файл.xbir> [<файл2.xbir> ...] --out <папка>
    python src/xbir_parser.py samples/6564-Spectorg-OOO/Slejt-M-10mm.-(19150)-1/.xbir --out out
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# --- Маппинг колонок .xbir → поле модели (см. спецификацию v0.1) -------------
# Имя поля модели : (имя колонки в .xbir, тип приведения)
FIELD_MAP: list[tuple[str, str, type]] = [
    ("detail_uid",       "ID детали",                 str),
    ("order_raw",        "Номер заказа",              str),
    ("material_name",    "Материал",                  str),
    ("material_article", "Артикул материала",         str),
    ("thickness",        "Толщина",                   int),
    ("length",           "Длина детали",              int),
    ("width",            "Ширина детали",             int),
    ("qty",              "Кол-во",                    int),
    ("perimeter",        "Периметр",                  float),
    ("area",             "Площадь",                   float),
    ("volume",           "Объем",                     float),
    ("edge_l1",          "Кромка L1 толщ.",           float),
    ("edge_l2",          "Кромка L2 толщ.",           float),
    ("edge_w1",          "Кромка W1 толщ.",           float),
    ("edge_w2",          "Кромка W2 толщ.",           float),
    ("drill_through",    "Кол. скв. отв. в пласть",   int),
    ("drill_blind",      "Кол. гл. отв. в пласть",    int),
    ("drill_end",        "Кол. отв. в торец",         int),
    ("grooves_lin",      "Кол. прямол. пазов",        int),
    ("grooves_curv",     "Кол. кривол. пазов",        int),
    ("plate_no",         "Номер плиты",               int),
    ("map_no",           "Номер карты",               int),
    ("pos_no",           "Номер позиции",             str),
]

ORDER_NUM_RE = re.compile(r"\d+")


@dataclass
class Detail:
    """Нормализованная модель детали (одна запись .xbir)."""
    detail_uid: str = ""
    order_num: Optional[int] = None
    order_raw: str = ""
    material_name: str = ""
    material_article: str = ""
    thickness: int = 0
    length: int = 0
    width: int = 0
    qty: int = 0
    perimeter: float = 0.0
    area: float = 0.0
    volume: float = 0.0
    edge_l1: float = 0.0
    edge_l2: float = 0.0
    edge_w1: float = 0.0
    edge_w2: float = 0.0
    edge_total_len: float = 0.0       # расчётное: пог.м кромки по периметру
    drill_through: int = 0
    drill_blind: int = 0
    drill_end: int = 0
    grooves: int = 0                  # расчётное: сумма пазов
    plate_no: int = 0
    map_no: int = 0
    pos_no: str = ""
    source_file: str = ""


@dataclass
class ValidationReport:
    source_file: str
    rows_total: int = 0
    rows_parsed: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)


# --- Приведение значений ------------------------------------------------------

def _coerce(raw: str, typ: type) -> object:
    """Привести строку .xbir к типу; пустая строка → 0/0.0/'' ."""
    raw = (raw or "").strip()
    if raw == "":
        return 0 if typ is int else (0.0 if typ is float else "")
    if typ is int:
        # Базис может писать "10.0" — берём целую часть
        return int(float(raw))
    if typ is float:
        return float(raw.replace(",", "."))
    return raw


def _parse_order_num(order_raw: str) -> Optional[int]:
    """'6564 Спецторг ООО' → 6564."""
    m = ORDER_NUM_RE.search(order_raw or "")
    return int(m.group()) if m else None


def _edge_total(detail: Detail) -> float:
    """
    Приближённая длина кромки в погонных метрах.
    Кромка L1/L2 — по длинной стороне (length), W1/W2 — по ширине (width).
    Если кромка на стороне есть (толщина > 0) — берём длину стороны; иначе 0.
    """
    L_m = detail.length / 1000.0
    W_m = detail.width / 1000.0
    total = 0.0
    if detail.edge_l1 > 0:
        total += L_m
    if detail.edge_l2 > 0:
        total += L_m
    if detail.edge_w1 > 0:
        total += W_m
    if detail.edge_w2 > 0:
        total += W_m
    return round(total, 3)


# --- Основной парсинг ---------------------------------------------------------

def parse_xbir(path: Path) -> tuple[list[Detail], ValidationReport]:
    """Распарсить один .xbir. Возвращает детали + отчёт валидации."""
    report = ValidationReport(source_file=str(path))
    details: list[Detail] = []

    # BOM-безопасное чтение: ElementTree спотыкается о UTF-8 BOM
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    root = ET.fromstring(raw)

    # 1. Колонки: имя → позиция (0-based по порядку Index)
    col_elems = root.findall(".//Col")
    name_to_pos: dict[str, int] = {}
    all_names: list[str] = []
    for c in col_elems:
        name = c.get("Name", "")
        idx = int(c.get("Index", "0"))
        # Index 1-based → позиция 0-based
        name_to_pos[name] = idx - 1
        all_names.append(name)

    # Контроль присутствия нужных колонок
    needed = {f for _, f, _ in FIELD_MAP}
    for n in needed:
        if n not in name_to_pos:
            report.missing_columns.append(n)

    # Неизвестные колонки (не из спецификации) — для аудита
    spec_names = set(needed)
    for n in all_names:
        if n not in spec_names:
            report.unknown_columns.append(n)

    # 2. Строки данных
    rows = root.findall(".//Row")
    report.rows_total = len(rows)

    for i, row_el in enumerate(rows, 1):
        text = row_el.text or ""
        cells = text.split("\t")

        detail = Detail(source_file=str(path))

        for field_name, col_name, typ in FIELD_MAP:
            pos = name_to_pos.get(col_name)
            if pos is None or pos >= len(cells):
                continue
            value = _coerce(cells[pos], typ)
            setattr(detail, field_name, value)

        # Производные поля
        detail.order_num = _parse_order_num(detail.order_raw)
        detail.edge_total_len = _edge_total(detail)
        detail.grooves = detail.grooves_lin + detail.grooves_curv

        # 3. Валидация строки
        row_errors: list[str] = []
        if not detail.detail_uid:
            row_errors.append("пустой detail_uid")
        if detail.order_num is None:
            row_errors.append(f"не извлечён order_num из '{detail.order_raw}'")
        if detail.qty <= 0:
            row_errors.append(f"qty={detail.qty}")
        if detail.length <= 0:
            row_errors.append(f"length={detail.length}")
        if detail.width <= 0:
            row_errors.append(f"width={detail.width}")
        if detail.thickness <= 0:
            row_errors.append(f"thickness={detail.thickness}")

        if row_errors:
            report.errors.append(
                f"строка {i} (uid={detail.detail_uid[:8]}…): "
                + "; ".join(row_errors)
            )
        else:
            details.append(detail)

    report.rows_parsed = len(details)
    return details, report


# --- Выгрузка результатов -----------------------------------------------------

CSV_COLUMNS = [
    "detail_uid", "order_num", "order_raw", "material_name", "material_article",
    "thickness", "length", "width", "qty", "perimeter", "area", "volume",
    "edge_l1", "edge_l2", "edge_w1", "edge_w2", "edge_total_len",
    "drill_through", "drill_blind", "drill_end", "grooves",
    "plate_no", "map_no", "pos_no", "source_file",
]


def write_csv(details: list[Detail], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for d in details:
            writer.writerow(asdict(d))


def write_report(reports: list[ValidationReport], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(r) for r in reports]
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Парсер .xbir → детали")
    p.add_argument("files", nargs="+", help="файлы .xbir")
    p.add_argument("--out", default="out", help="папка для результатов")
    args = p.parse_args(argv)

    all_details: list[Detail] = []
    all_reports: list[ValidationReport] = []
    errors_total = 0

    for f in args.files:
        path = Path(f)
        if not path.is_file():
            print(f"  ✗ файл не найден: {path}", file=sys.stderr)
            errors_total += 1
            continue
        try:
            details, report = parse_xbir(path)
        except ET.ParseError as e:
            print(f"  ✗ XML-ошибка в {path}: {e}", file=sys.stderr)
            errors_total += 1
            continue

        all_details.extend(details)
        all_reports.append(report)
        errors_total += len(report.errors)

        status = "✓" if not report.errors else "⚠"
        print(
            f"  {status} {path.name}: строк {report.rows_total}, "
            f"распарсено {report.rows_parsed}, ошибок {len(report.errors)}, "
            f"отсутствует колонок {len(report.missing_columns)}"
        )
        for e in report.errors[:5]:
            print(f"      • {e}")
        if report.missing_columns:
            print(f"      • отсутствуют колонки: {', '.join(report.missing_columns)}")

    out_dir = Path(args.out)
    write_csv(all_details, out_dir / "details.csv")
    write_report(all_reports, out_dir / "validation_report.json")

    print()
    print(f"  ИТОГО: деталей {len(all_details)}, ошибок {errors_total}")
    print(f"  → {out_dir / 'details.csv'}")
    print(f"  → {out_dir / 'validation_report.json'}")
    return 0 if errors_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
