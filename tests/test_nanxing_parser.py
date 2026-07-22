"""
Тесты парсера Nanxing .SCX (src/nanxing_parser.py) — источник присадки.

Реальные данные Nanxing содержат ПД (имена клиентов в путях) и в
репозиторий не попадают. Тесты работают на синтетическом .SCX той же
структуры.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nanxing_parser import (
    Panel,
    parse_order_folder,
    parse_scx,
    summarize_drilling,
)

SCX_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Root Cad="BuiltInCad" version="2.0">
  <Project><Panels>
    <Panel IsProduce="true" ID="{pid}" Name="{name}"
           Length="{length}" Width="{width}" Thickness="{thickness}">
      <Machines>
        {machines}
      </Machines>
    </Panel>
  </Panels></Project>
</Root>"""


def machining(mtype, face, diameter, depth=11):
    return (f'<Machining Type="{mtype}" Face="{face}" X="100" Y="37.5" '
            f'Diameter="{diameter}" Depth="{depth}"/>')


def write_scx(path: Path, name="12_Цоколь_1135х75_(1_штук)", length=1135,
              width=75, thickness=16, machines=None, with_bom=True):
    body = SCX_TEMPLATE.format(
        pid=name, name=name, length=length, width=width, thickness=thickness,
        machines="\n".join(machines or []),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ("﻿" if with_bom else "") + body
    path.write_text(payload, encoding="utf-8")
    return path


# --- Разбор одной панели -----------------------------------------------------

def test_parse_panel_dimensions(tmp_path):
    f = write_scx(tmp_path / "p.SCX", length=1135, width=75, thickness=16)
    panels = parse_scx(f)
    assert len(panels) == 1
    p = panels[0]
    assert (p.length, p.width, p.thickness) == (1135, 75, 16)


def test_position_and_qty_from_name(tmp_path):
    f = write_scx(tmp_path / "p.SCX", name="5_Стенка_1880х544_(2_штук)")
    p = parse_scx(f)[0]
    assert p.pos_no == "5"
    assert p.qty == 2


def test_drill_types_counted(tmp_path):
    machines = [
        machining(1, 1, 5, depth=36),   # торец
        machining(1, 4, 5, depth=36),   # торец
        machining(2, 5, 5),             # пласть
        machining(2, 6, 5),             # пласть
        machining(2, 5, 8),             # пласть
    ]
    f = write_scx(tmp_path / "p.SCX", machines=machines)
    p = parse_scx(f)[0]
    assert p.drill_end == 2
    assert p.drill_face == 3
    assert p.drill_total == 5


def test_diameters_collected(tmp_path):
    machines = [machining(2, 5, 5), machining(2, 5, 5), machining(1, 1, 8)]
    f = write_scx(tmp_path / "p.SCX", machines=machines)
    p = parse_scx(f)[0]
    assert p.diameters["5"] == 2
    assert p.diameters["8"] == 1


def test_bom_handled(tmp_path):
    with_bom = write_scx(tmp_path / "bom.SCX", with_bom=True)
    without = write_scx(tmp_path / "nobom.SCX", with_bom=False)
    assert len(parse_scx(with_bom)) == len(parse_scx(without)) == 1


def test_panel_without_drilling(tmp_path):
    f = write_scx(tmp_path / "p.SCX", machines=[])
    p = parse_scx(f)[0]
    assert p.drill_total == 0


# --- qty и нормирование ------------------------------------------------------

def test_qty_multiplies_holes():
    """qty=3 деталей — отверстия считаются на все экземпляры."""
    p = Panel(panel_id="x", name="1_деталь_(3_штук)", qty=3,
              drill_end=2, drill_face=4)
    assert p.drill_total == 6
    assert p.drill_total_with_qty == 18


def test_summary_respects_qty(tmp_path):
    machines = [machining(1, 1, 5), machining(2, 5, 8)]
    f = write_scx(tmp_path / "p.SCX", name="1_д_(3_штук)", machines=machines)
    panels = parse_scx(f)
    s = summarize_drilling(panels)
    assert s["holes_end"] == 3       # 1 торец × qty 3
    assert s["holes_face"] == 3      # 1 пласть × qty 3
    assert s["holes_total"] == 6
    assert s["diameters"] == {"5": 3, "8": 3}


# --- Заказ и папка -----------------------------------------------------------

def test_order_number_from_folder(tmp_path):
    folder = tmp_path / "1774_Прохорова" / "изделие"
    write_scx(folder / "p.SCX", machines=[machining(1, 1, 5)])
    panels, report = parse_order_folder(tmp_path)
    assert panels[0].order_num == 1774
    assert report.orders == {1774}


def test_folder_aggregates_multiple_files(tmp_path):
    folder = tmp_path / "2054_Клиент"
    write_scx(folder / "a.SCX", machines=[machining(1, 1, 5), machining(2, 5, 8)])
    write_scx(folder / "b.SCX", machines=[machining(2, 6, 5)])
    panels, report = parse_order_folder(tmp_path)
    assert report.files_total == 2
    assert report.panels_total == 2
    assert report.drill_operations == 3


def test_broken_file_does_not_stop_folder(tmp_path):
    folder = tmp_path / "3000_X"
    write_scx(folder / "ok.SCX", machines=[machining(1, 1, 5)])
    (folder / "bad.SCX").write_text("<not valid xml", encoding="utf-8")
    panels, report = parse_order_folder(tmp_path)
    assert report.panels_total == 1
    assert len(report.files_failed) == 1


def test_identity_gap_no_guid(tmp_path):
    """
    Фиксация разрыва: в .SCX нет GUID детали — только имя и габариты.
    Панель нельзя однозначно связать с деталью .xbir, поэтому парсер
    даёт нормирование по заказу, а не факт по конкретной детали.
    """
    f = write_scx(tmp_path / "p.SCX")
    p = parse_scx(f)[0]
    assert not hasattr(p, "detail_uid")
    assert p.name and p.pos_no        # опознаётся именем и позицией
