#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тестовый стенд ПСР на ВЫДУМАННЫХ заказах (решение владельца 20.09.2026).

ЗАЧЕМ
    Показать рабочие места цеху и собрать замечания до того, как система
    попадёт в цеховую сеть. Данные здесь придуманы целиком: ни одного
    настоящего клиента, ни одной настоящей фамилии, ни одного файла из
    выгрузки БАЗИС. Поэтому стенд можно поднимать где угодно, включая
    сервер в интернете, и вопрос персональных данных не возникает.

ЧТО СТЕНД ПРОВЕРЯЕТ И ЧЕГО НЕ ПРОВЕРЯЕТ — ЧИТАТЬ ОБЯЗАТЕЛЬНО

    Проверяет:
      — понятны ли экраны оператору и начальнику цеха;
      — одно ли движение нужно оператору (скан открывает и закрывает);
      — отказ вместо угадывания: заказ вне сменного задания (SR-108);
      — предъявление декоров с ЦЕЛЫМ числом листов (SR-111, SR-112);
      — подтверждение связки, его однократность и отзыв (SR-113);
      — права по ролям и вход по бейджу (SR-96, SR-97);
      — натуральные измерители шести участков (SR-26) в редакции D-210.

    НЕ проверяет — и это надо помнить при чтении замечаний:
      — что связка работает на НАСТОЯЩИХ данных: заказы придуманы, и
        разнобой реальных номеров, дат и имён папок здесь не встретится;
      — BR-51 «отказ канала связи не останавливает цех»: при сервере в
        интернете это требование не выполняется по построению;
      — A-09а в боевом виде: рабочая папка станка здесь — папка на том же
        сервере, а не папка у станка в цехе.

    Последние два проверяются только на сервере в цеховой сети. Стенд их
    не заменяет и заменить не может.

ПЕРЕНОС В ЦЕХОВУЮ СЕТЬ
    Меняются ДВЕ настройки: `bazis_root` и `machine_dir`. Всё остальное —
    база, права, справочники — переносится файлом базы либо строится
    этим же скриптом заново. Ничего в коде править не нужно.

Запуск:
    python3 tools/make_test_stand.py --root /путь/к/стенду
    python3 tools/run_stand.py      --root /путь/к/стенду

ПОЧЕМУ НЕ `flask --app src.operator_app run`
    Потому что так не работает, и это проверено запуском, а не
    предположено. `create_app(db_path="prod.db")` берёт путь к базе
    аргументом и переменную окружения не читает; команда `flask --app`
    вызывает фабрику без аргументов, и приложение открывает `prod.db` в
    текущей папке. Первая редакция этой инструкции советовала именно
    такую команду — CF-428.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from access import (  # noqa: E402
    CHIEF,
    OPERATOR,
    STOREKEEPER,
    TECHNOLOGIST,
    Access,
    seed_default_permissions,
)
from area_measures import (  # noqa: E402
    DETAIL_ITEMS,
    DRILLED_ITEMS,
    EDGE_METERS,
    MILLED_ITEMS,
    SHEETS,
    AreaMeasures,
)
from raskroy_nav import SETTING_BAZIS_ROOT, SETTING_MACHINE_DIR  # noqa: E402
from storage import Storage  # noqa: E402

# --- Выдуманные заказы -------------------------------------------------------
#
# Номера взяты из девятитысячного диапазона намеренно: в настоящей выгрузке
# таких нет, и тестовый заказ ни при каких обстоятельствах не спутается с
# боевым. Клиенты названы так, чтобы никто не принял их за настоящих.
#
# Каждый заказ проверяет своё:
#
#   9001 — три декора: список выбора, подтверждение, копирование
#   9002 — один декор: SR-116, программа без списка
#   9003 — в связке ЕСТЬ, в сменном задании НЕТ: SR-108, отказ
#   9004 — детали с разными маршрутами: измерители и задел под CHG-003

ORDERS = [
    dict(order_num=9001, doc_num="ПС00-900001", doc_date="2026-09-21",
         client="Тестовый заказчик А", machine="Gabbiani",
         decors={"Kronoshpan-2500h1830-1": 4,
                 "Oreh-Karija-16-mm-1": 2,
                 "HDF-2800h2070-1": 2},
         in_task=True, row_no=1),
    dict(order_num=9002, doc_num="ПС00-900002", doc_date="2026-09-21",
         client="Тестовый заказчик Б", machine="Gabbiani",
         decors={"Belyj-R-16mm-1": 3},
         in_task=True, row_no=2),
    dict(order_num=9003, doc_num="ПС00-900003", doc_date="2026-09-21",
         client="Тестовый заказчик В", machine="Gabbiani",
         decors={"Dub-Votan-1": 5},
         in_task=False, row_no=None),
    dict(order_num=9004, doc_num="ПС00-900004", doc_date="2026-09-21",
         client="Тестовый заказчик Г", machine="Nanxing",
         decors={"LDSP-Seryj-1": 6, "HDF-Belyj-1": 1},
         in_task=True, row_no=3),
]

# Детали заказов. Маршрут детали задаётся признаками: кромка, пазы,
# отверстия. У 9004 детали идут РАЗНЫМИ путями — это половина заказов в
# настоящей выгрузке (измерено: 33 из 68), и стенд обязан этот случай
# показывать, а не прятать.
DETAILS = {
    9001: [
        # uid, qty, кромка мм, пазы, отверстия, декор
        ("T9001-01", 4, 1500.0, 0, 8, "Kronoshpan-2500h1830-1"),
        ("T9001-02", 2, 900.0, 0, 4, "Kronoshpan-2500h1830-1"),
        ("T9001-03", 6, 0.0, 0, 0, "Oreh-Karija-16-mm-1"),
        ("T9001-04", 1, 2200.0, 2, 0, "HDF-2800h2070-1"),
    ],
    9002: [
        ("T9002-01", 20, 700.0, 0, 2, "Belyj-R-16mm-1"),
        ("T9002-02", 4, 700.0, 0, 2, "Belyj-R-16mm-1"),
    ],
    9003: [
        ("T9003-01", 8, 1200.0, 0, 0, "Dub-Votan-1"),
    ],
    9004: [
        ("T9004-01", 10, 1800.0, 0, 12, "LDSP-Seryj-1"),   # кромка + присадка
        ("T9004-02", 3, 1800.0, 4, 12, "LDSP-Seryj-1"),    # + фрезерование
        ("T9004-03", 12, 0.0, 0, 0, "HDF-Belyj-1"),        # только раскрой
    ],
}

# Исполнители стенда. ФИО выдуманы; коды система присвоит сама (ИСП-NNNN).
PEOPLE = [
    ("Тестовый Оператор Раскроя", OPERATOR, "B-1001"),
    ("Тестовый Оператор Кромления", OPERATOR, "B-1002"),
    ("Тестовый Кладовщик", STOREKEEPER, "B-1003"),
    ("Тестовый Начальник Цеха", CHIEF, "B-1004"),
    ("Тестовый Технолог", TECHNOLOGIST, "B-1005"),
]

# Измерители участков в редакции решения D-210.
MEASURES = {
    "raskroy": SHEETS,
    "kromlenie": EDGE_METERS,
    "frezerovanie": MILLED_ITEMS,
    "prisadka": DRILLED_ITEMS,
    "sborka": DETAIL_ITEMS,
    "sklad": DETAIL_ITEMS,
}

TASK_ID = "ТЕСТ-01"
TASK_DATE = "2026-09-21"


def build_catalog(root: Path) -> int:
    """
    Каталог заданий БАЗИС той же структуры, что настоящий.

    Имена папок содержат только номер и слово «Тест»: фамилий здесь нет
    и быть не может — см. заголовок модуля.
    """
    files = 0
    for o in ORDERS:
        folder = root / o["machine"] / f"{o['order_num']}-Test"
        for decor, sheets in o["decors"].items():
            d = folder / decor
            d.mkdir(parents=True, exist_ok=True)
            # По программе на лист — так же, как в настоящем каталоге.
            for i in range(1, sheets + 1):
                (d / f"Board-{i}.xPrg").write_text(
                    f"; тестовая программа раскроя\n"
                    f"; заказ {o['order_num']}, декор {decor}, лист {i}\n",
                    encoding="utf-8")
                files += 1
    return files


def seed_db(db_path: Path, bazis_root: Path, machine_dir: Path) -> dict:
    """Наполнить базу стенда. Возвращает сводку для печати."""
    st = Storage(str(db_path))
    st.set_setting(SETTING_BAZIS_ROOT, str(bazis_root))
    st.set_setting(SETTING_MACHINE_DIR, str(machine_dir))

    # Справочник участков и измерителей (SR-26а, редакция D-210).
    measures = AreaMeasures(st)
    for area, code in MEASURES.items():
        measures.set_measure(area, code)

    # Связка «заказ БАЗИС ↔ документ 1С». На стенде она задаётся прямо;
    # в бою её строит резолвер по выгрузкам.
    for o in ORDERS:
        st.upsert_order_link(o["order_num"], "confirmed",
                             order_full_num=o["doc_num"],
                             order_date=o["doc_date"],
                             client_name=o["client"])

    # Состав заказов.
    details = 0
    for order_num, rows in DETAILS.items():
        src = f"{order_num}-Test"
        for uid, qty, edge, grooves, drill, decor in rows:
            st.upsert_detail(dict(
                detail_uid=uid, order_num=order_num, qr_code=uid, qty=qty,
                edge_total_len=edge, grooves=grooves, drill_total=drill,
                material_name=decor, plate_no=1, map_no=1,
                source_file=f"{src}/{decor}"))
            details += 1

    # Сменное задание: заказ 9003 в него НЕ входит намеренно (SR-108).
    st.upsert_shift_task(TASK_ID, TASK_DATE, "Тестовый склад ЛДСП")
    rows = 0
    for o in ORDERS:
        if not o["in_task"]:
            continue
        for decor, sheets in o["decors"].items():
            st.add_shift_task_row(TASK_ID, o["row_no"], o["doc_num"],
                                  o["doc_date"], decor, sheets,
                                  order_num=o["order_num"])
            rows += 1

    # Роли, права и бейджи.
    ac = Access(st)
    granted = seed_default_permissions(ac)
    people = [ac.add_person(name, role, badge_id=badge)
              for name, role, badge in PEOPLE]

    st.close()
    return dict(details=details, task_rows=rows, granted=granted,
                people=people)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="./teststand",
                    help="папка стенда (каталог БАЗИС, папка станка, база)")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать существующий стенд")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    db = root / "psr.db"
    if db.exists() and not args.force:
        print(f"Стенд уже есть: {db}\nДобавьте --force, чтобы пересобрать.")
        return 1
    if db.exists():
        db.unlink()

    bazis, machine = root / "bazis", root / "station"
    root.mkdir(parents=True, exist_ok=True)
    machine.mkdir(parents=True, exist_ok=True)

    programs = build_catalog(bazis)
    info = seed_db(db, bazis, machine)

    print("=" * 66)
    print("ТЕСТОВЫЙ СТЕНД ПСР СОБРАН")
    print("=" * 66)
    print(f"Папка стенда:        {root}")
    print(f"База:                {db}")
    print(f"Каталог БАЗИС:       {bazis}")
    print(f"Рабочая папка станка:{machine}")
    print()
    print(f"Заказов:             {len(ORDERS)}")
    print(f"Деталей:             {info['details']}")
    print(f"Программ раскроя:    {programs}")
    print(f"Строк задания:       {info['task_rows']}  (задание {TASK_ID})")
    print(f"Прав выдано:         {info['granted']}")
    print()
    print("БЕЙДЖИ ДЛЯ ВХОДА")
    for p in info["people"]:
        print(f"  {p.badge_id}   {p.person_code}   {p.role_name}")
    print()
    print("КОДЫ БЛАНКОВ ДЛЯ СКАНА (вводятся с клавиатуры, если нет сканера)")
    for o in ORDERS:
        mark = "" if o["in_task"] else "   ← НЕТ в задании: ожидается отказ"
        print(f"  {o['doc_num']}|{o['doc_date']}"
              f"   заказ {o['order_num']}, "
              f"декоров {len(o['decors'])}{mark}")
    print()
    print("ЗАПУСК — одной командой")
    print(f"  python3 tools/run_stand.py --root {root}")
    print()
    print("  Чтобы открывалось с планшета или телефона в той же сети:")
    print(f"  python3 tools/run_stand.py --root {root} --host 0.0.0.0")
    print()
    print("  Оператор раскроя:  /psr/operator?area=raskroy")
    print("  Оператор кромления:/psr/operator?area=kromlenie")
    print("  Начальник цеха:    /psr/chief")
    print()
    print("ПЕРЕНОС В ЦЕХОВУЮ СЕТЬ — меняются ДВЕ настройки:")
    print(f"  bazis_root   → путь к настоящему каталогу заданий БАЗИС")
    print(f"  machine_dir  → рабочая папка станка в цехе")
    print("  Код не правится. База переносится файлом либо строится заново.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
