"""
CLI ядра подетального учёта.

Команды:
    init      — создать БД и заполнить справочник участков
    import    — загрузить плановый состав деталей из .xbir
    scan      — обработать скан (для отладки без интерфейса оператора)
    status    — статус операций заказа, сверка план/факт
    stats     — сводка по хранилищу

Примеры:
    python main.py init --db prod.db
    python main.py import --db prod.db samples/**/*.xbir
    python main.py scan --db prod.db --order 6564 --qr 3c39598e6a \\
        --area area_edging --operator op_mazein --op "Облицовывание кромки 19/0,8"
    python main.py status --db prod.db --order 6564 \\
        --ops "Раскрой плиты 16мм" "Облицовывание кромки 19/0,8"
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from operation_plan import format_plan_report  # noqa: E402
from pipeline import ProductionCore  # noqa: E402
from scan_processor import ScanEvent  # noqa: E402
from status_calc import format_status_report, to_1c_payload  # noqa: E402
from storage import Storage  # noqa: E402


def _core(db: str) -> ProductionCore:
    return ProductionCore(Storage(db))


def cmd_init(args) -> int:
    core = _core(args.db)
    n = core.seed_areas()
    print(f"БД готова: {args.db}")
    print(f"Справочник участок→операция: {n} связок")
    for row in core.storage.get_all_areas():
        ops = core.area_operations(row["area_id"])
        print(f"  • {row['area_name']} ({row['area_id']}): {len(ops)} операций")
    return 0


def cmd_import(args) -> int:
    core = _core(args.db)
    paths = [Path(p) for p in args.files]
    missing = [p for p in paths if not p.is_file()]
    for p in missing:
        print(f"  ✗ не найден: {p}", file=sys.stderr)
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("Нет файлов для импорта", file=sys.stderr)
        return 1

    res = core.import_xbir(paths)
    print(f"Файлов:    {res.files}")
    print(f"Строк:     {res.rows_total}")
    print(f"Деталей:   {res.details_imported}")
    print(f"Заказов:   {len(res.orders)} → {res.orders}")
    print(f"Ошибок:    {res.errors}")

    if res.qr_collisions:
        print()
        print(f"  ⚠ КОЛЛИЗИИ QR: {len(res.qr_collisions)} — учёт неоднозначен!")
        for code, a, b in res.qr_collisions[:5]:
            print(f"      {code}: {a} ↔ {b}")
    for r in res.reports:
        for e in r.errors[:5]:
            print(f"      • {e}")
    return 0 if res.errors == 0 else 1


def cmd_scan(args) -> int:
    core = _core(args.db)
    ctx = core.load_order(args.order)
    if not ctx.details:
        print(f"Заказ {args.order} не найден в БД. Сначала import.", file=sys.stderr)
        return 1

    event = ScanEvent(
        scan_id=args.scan_id or f"evt_{uuid.uuid4().hex[:12]}",
        qr_code=args.qr,
        area_id=args.area,
        operator_id=args.operator,
        operation_1c=args.op,
        scanned_at=datetime.now(),
    )
    r = core.handle_scan(event, ctx)

    icon = {"accepted": "✓", "duplicate": "·", "overplan": "⚠"}.get(r.status.value, "✗")
    print(f"{icon} {r.status.value}")
    if r.detail_uid:
        print(f"   деталь: {r.detail_uid}")
    if r.planned_qty:
        print(f"   счётчик: {r.scanned_count}/{r.planned_qty}")
    if r.message:
        print(f"   {r.message}")
    if r.suggest_detail_list:
        print("   → показать оператору список деталей заказа")
    return 0


def cmd_status(args) -> int:
    core = _core(args.db)
    ctx = core.load_order(args.order)
    if not ctx.details:
        print(f"Заказ {args.order} не найден в БД.", file=sys.stderr)
        return 1

    plans, warnings = core.order_plan(ctx, args.ops)
    print(format_plan_report(args.order, plans, warnings))
    print()
    print(format_status_report(core.order_status(ctx, args.ops)))

    payloads = core.closing_payloads(ctx, args.ops)
    if payloads:
        print()
        print("PAYLOAD для 1С (отправка — Этап B):")
        for p in payloads:
            print(f"  {p}")
    return 0


def cmd_stats(args) -> int:
    core = _core(args.db)
    for k, v in core.storage.stats().items():
        print(f"  {k:<14} {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ядро подетального учёта")
    p.add_argument("--db", default="detail_accounting.db", help="файл БД SQLite")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать БД и справочник участков").set_defaults(fn=cmd_init)

    imp = sub.add_parser("import", help="импорт .xbir")
    imp.add_argument("files", nargs="+")
    imp.set_defaults(fn=cmd_import)

    sc = sub.add_parser("scan", help="обработать скан")
    sc.add_argument("--order", type=int, required=True)
    sc.add_argument("--qr", required=True)
    sc.add_argument("--area", required=True)
    sc.add_argument("--operator", required=True)
    sc.add_argument("--op", required=True, help="операция 1С")
    sc.add_argument("--scan-id", default=None)
    sc.set_defaults(fn=cmd_scan)

    st = sub.add_parser("status", help="статус операций заказа")
    st.add_argument("--order", type=int, required=True)
    st.add_argument("--ops", nargs="+", required=True, help="операции 1С заказа")
    st.set_defaults(fn=cmd_status)

    sub.add_parser("stats", help="сводка по БД").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
