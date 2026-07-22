"""
Парсер .SCX (Nanxing) → присадочные операции детали.

Контекст: docs/dataset-validation-report.md — участок присадки не покрыт
`.xbir` (в раскрое отверстий нет). Источник данных о сверлении — выгрузка
Базиса для станка Nanxing, папка с файлами `.SCX`.

## Формат .SCX

XML, кодировка UTF-8:

    <Panel ID="12_Цоколь_перед_1135х75_(1_штук)" Length="1135" Width="75"
           Thickness="16">
      <Machines>
        <Machining Type="1" Face="1" X="151" Y="75" Diameter="5" Depth="36"/>
        <Machining Type="2" Face="5" X="130" Y="37.5" Diameter="5" Depth="11"/>
        ...
      </Machines>
      <EdgeGroup>...</EdgeGroup>
    </Panel>

Type: 1 — отверстие в торец, 2 — отверстие в пласть.
Face: 1-4 — торцы, 5-6 — пласти.

## Разрыв идентификации (docs/dataset-validation-report.md)

В `.SCX` **нет GUID детали** — панель опознаётся по имени
(`12_Цоколь_перед_1135х75_(1_штук)`). Связать её с деталью из `.xbir`
можно только по позиции в изделии и габаритам, и это ненадёжно (детали
одного размера неразличимы). Поэтому парсер отдаёт **нормирование
присадки по заказу** — сколько всего отверстий, какого типа, — а не
факт по конкретной детали. Сквозной учёт присадки требует GUID в выгрузке
Nanxing (вопрос к Базису) и пока не строится.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Имя папки заказа: «1774_Прохорова» → номер 1774
ORDER_DIR_RE = re.compile(r"^(\d+)")

# Имя панели: «12_Цоколь_перед_1135х75_(1_штук)» → позиция 12, кол-во 1
PANEL_POS_RE = re.compile(r"^(\d+)_")
PANEL_QTY_RE = re.compile(r"\((\d+)[\s_]*штук")

DRILL_END = "1"      # отверстие в торец
DRILL_FACE = "2"     # отверстие в пласть


@dataclass
class Panel:
    """Панель из .SCX: габариты + присадочные операции."""
    panel_id: str
    name: str
    pos_no: str = ""
    qty: int = 1
    length: int = 0
    width: int = 0
    thickness: int = 0
    drill_end: int = 0            # отверстий в торец
    drill_face: int = 0           # отверстий в пласть
    diameters: Counter = field(default_factory=Counter)
    source_file: str = ""
    order_num: int | None = None

    @property
    def drill_total(self) -> int:
        return self.drill_end + self.drill_face

    @property
    def drill_total_with_qty(self) -> int:
        """Отверстий с учётом количества одинаковых деталей."""
        return self.drill_total * self.qty


@dataclass
class NanxingReport:
    files_total: int = 0
    files_failed: list[str] = field(default_factory=list)
    panels_total: int = 0
    drill_operations: int = 0
    orders: set[int] = field(default_factory=set)


def _int(value: str | None) -> int:
    try:
        return int(float(value)) if value else 0
    except (TypeError, ValueError):
        return 0


def _order_from_path(path: Path) -> int | None:
    """Достать номер заказа из имени папки в пути (…/1774_Прохорова/…)."""
    for part in path.parts:
        m = ORDER_DIR_RE.match(part)
        if m:
            return int(m.group(1))
    return None


def parse_scx(path: Path) -> list[Panel]:
    """Распарсить один .SCX → список панелей с присадкой."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    root = ET.fromstring(raw)

    order_num = _order_from_path(path)
    panels: list[Panel] = []

    for pe in root.iter("Panel"):
        name = pe.get("Name") or pe.get("ID") or ""
        pos_m = PANEL_POS_RE.match(name)
        qty_m = PANEL_QTY_RE.search(name)

        panel = Panel(
            panel_id=pe.get("ID", ""),
            name=name,
            pos_no=pos_m.group(1) if pos_m else "",
            qty=int(qty_m.group(1)) if qty_m else 1,
            length=_int(pe.get("Length")),
            width=_int(pe.get("Width")),
            thickness=_int(pe.get("Thickness")),
            source_file=str(path),
            order_num=order_num,
        )

        for mc in pe.iter("Machining"):
            mtype = mc.get("Type")
            if mtype == DRILL_END:
                panel.drill_end += 1
            elif mtype == DRILL_FACE:
                panel.drill_face += 1
            diameter = mc.get("Diameter")
            if diameter:
                panel.diameters[diameter] += 1

        panels.append(panel)

    return panels


def parse_order_folder(folder: Path) -> tuple[list[Panel], NanxingReport]:
    """
    Распарсить все .SCX заказа (папка заказа Nanxing) → панели + отчёт.

    Даёт нормирование присадки по заказу: сколько отверстий, какого типа,
    какими диаметрами. Это загрузка участка присадки — то, ради чего
    Nanxing и нужен.
    """
    report = NanxingReport()
    panels: list[Panel] = []

    for path in sorted(folder.rglob("*.SCX")) + sorted(folder.rglob("*.scx")):
        report.files_total += 1
        try:
            parsed = parse_scx(path)
        except Exception as e:                       # noqa: BLE001
            report.files_failed.append(f"{path.name}: {type(e).__name__}: {e}")
            continue
        panels.extend(parsed)
        for p in parsed:
            report.panels_total += 1
            report.drill_operations += p.drill_total
            if p.order_num is not None:
                report.orders.add(p.order_num)

    return panels, report


def summarize_drilling(panels: list[Panel]) -> dict:
    """
    Нормирование участка присадки по набору панелей.

    Возвращает сводку: отверстий всего (с учётом qty), в торец, в пласть,
    распределение по диаметрам, число деталей со сверлением.
    """
    diameters: Counter = Counter()
    end = face = 0
    drilled_details = 0

    for p in panels:
        end += p.drill_end * p.qty
        face += p.drill_face * p.qty
        for d, c in p.diameters.items():
            diameters[d] += c * p.qty
        if p.drill_total > 0:
            drilled_details += 1

    return {
        "panels": len(panels),
        "drilled_details": drilled_details,
        "holes_total": end + face,
        "holes_end": end,
        "holes_face": face,
        "diameters": dict(diameters.most_common()),
    }


def format_drilling_report(panels: list[Panel], report: NanxingReport) -> str:
    """Человекочитаемый отчёт нормирования присадки."""
    s = summarize_drilling(panels)
    lines = ["=== НОРМИРОВАНИЕ ПРИСАДКИ (Nanxing .SCX) ===", ""]
    lines.append(f"Файлов .SCX:        {report.files_total} "
                 f"(сбой разбора: {len(report.files_failed)})")
    lines.append(f"Панелей:            {s['panels']}")
    lines.append(f"Заказов:            {len(report.orders)}")
    lines.append(f"Деталей со сверлением: {s['drilled_details']}")
    lines.append("")
    lines.append(f"Отверстий всего:    {s['holes_total']}")
    lines.append(f"  в торец:          {s['holes_end']}")
    lines.append(f"  в пласть:         {s['holes_face']}")
    lines.append("")
    lines.append("По диаметрам:")
    for d, c in s["diameters"].items():
        lines.append(f"  d{d} мм: {c}")
    lines.append("")
    if report.files_failed:
        lines.append("Ошибки разбора:")
        for e in report.files_failed[:10]:
            lines.append(f"  • {e}")
    return "\n".join(lines)
