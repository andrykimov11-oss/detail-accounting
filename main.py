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

    res = core.import_xbir(paths, require_link=args.require_link)
    print(f"Файлов:    {res.files}")
    print(f"Строк:     {res.rows_total}")
    print(f"Деталей:   {res.details_imported}")
    print(f"Заказов:   {len(res.orders)} → {res.orders}")
    print(f"Ошибок:    {res.errors}")

    if res.skipped_unlinked:
        print()
        print(f"  ⚠ ПРОПУЩЕНО {len(res.skipped_unlinked)} заказов без связки с 1С:")
        print(f"      {res.skipped_unlinked[:15]}")
        print(f"      разобрать: python main.py --db {args.db} pending")

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


def cmd_link(args) -> int:
    """Разрешить связки заказов Базиса с документами 1С."""
    from one_c_loader import load_production_plan
    from order_resolver import format_resolve_report
    from xbir_parser import parse_xbir

    core = _core(args.db)

    one_c, load_rep = load_production_plan(Path(args.plan))
    print(f"1С: строк {load_rep.rows_total}, заказов активных {load_rep.orders_total}")
    print(f"    префиксы: {load_rep.prefixes}")

    xbir_orders: dict[int, str] = {}
    for f in args.files:
        path = Path(f)
        if not path.is_file():
            print(f"  ✗ не найден: {path}", file=sys.stderr)
            continue
        details, _ = parse_xbir(path)
        for d in details:
            if d.order_num is not None:
                xbir_orders.setdefault(d.order_num, d.order_raw.strip())

    if not xbir_orders:
        print("Не найдено ни одного заказа в .xbir", file=sys.stderr)
        return 1

    print()
    print(format_resolve_report(core.resolve_links(xbir_orders, one_c)))
    return 0


def cmd_pending(args) -> int:
    """Очередь ручного сопоставления для технолога."""
    core = _core(args.db)
    rows = core.pending_links()

    if not rows:
        print("Очередь пуста: связки всех заказов подтверждены.")
        return 0

    print(f"ТРЕБУЕТСЯ СОПОСТАВЛЕНИЕ: {len(rows)} заказов\n")
    for r in rows:
        print(f"  Базис {r['order_num']}: «{r['xbir_client'] or '—'}»")
        print(f"     {r['reason']}")
        for cand in (r["candidates"] or "").split(";"):
            if cand:
                print(f"       ? {cand}")
        print(f"     подтвердить:  python main.py --db {args.db} confirm "
              f"--order {r['order_num']} --doc <НОМЕР> --by <ФИО>")
        print()
    return 0


def cmd_confirm(args) -> int:
    """Технолог подтверждает связку вручную."""
    core = _core(args.db)
    core.confirm_link(args.order, args.doc, args.by)
    row = core.storage.get_order_link(args.order)
    print(f"✓ заказ {args.order} → {row['order_full_num']}")
    print(f"   подтвердил: {row['confirmed_by']}")
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
    imp.add_argument("--require-link", action="store_true",
                     help="не импортировать заказы без подтверждённой связки с 1С")
    imp.set_defaults(fn=cmd_import)

    ln = sub.add_parser("link", help="разрешить связки заказов Базис ↔ 1С")
    ln.add_argument("files", nargs="+", help="файлы .xbir")
    ln.add_argument("--plan", required=True, help="xlsx выгрузки 1С")
    ln.set_defaults(fn=cmd_link)

    sub.add_parser("pending", help="очередь ручного сопоставления").set_defaults(
        fn=cmd_pending)

    cf = sub.add_parser("confirm", help="подтвердить связку вручную")
    cf.add_argument("--order", type=int, required=True, help="номер заказа Базиса")
    cf.add_argument("--doc", required=True, help="номер документа 1С")
    cf.add_argument("--by", required=True, help="кто подтвердил")
    cf.set_defaults(fn=cmd_confirm)

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
