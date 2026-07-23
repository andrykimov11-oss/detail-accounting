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


def _expand_xbir(paths: list[str]) -> list[Path]:
    """
    Развернуть аргументы в список .xbir.

    Аргумент может быть файлом или ПАПКОЙ — папка обходится рекурсивно
    (.xbir/.XBIR). Это позволяет указать общую сетевую папку, куда Базис
    складывает раскрои, и читать её напрямую — без переноса файлов
    сторонними программами.
    """
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.xbir")) + sorted(p.rglob("*.XBIR")))
        elif p.is_file():
            out.append(p)
        else:
            print(f"  ✗ не найден: {p}", file=sys.stderr)
    return out


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
    paths = _expand_xbir(args.files)
    if not paths:
        print("Нет файлов для импорта (укажите .xbir или папку с ними)",
              file=sys.stderr)
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
        print("PAYLOAD для 1С (готовы к отправке):")
        for p in payloads:
            print(f"  {p}")
    return 0


def cmd_push(args) -> int:
    """Отправить статусы закрытых операций заказа в 1С."""
    core = _core(args.db)
    ctx = core.load_order(args.order)
    if not ctx.details:
        print(f"Заказ {args.order} не найден в БД.", file=sys.stderr)
        return 1
    queued, delivered = core.push_status_to_1c(ctx, args.ops)
    print(f"Поставлено в очередь: {queued}")
    print(f"Доставлено в 1С:      {delivered}")
    if queued > delivered:
        print(f"  ⚠ не доставлено {queued - delivered} — повторить: "
              f"python main.py --db {args.db} flush")
    return 0


def cmd_flush(args) -> int:
    """Повторно отправить недоставленные статусы из очереди."""
    core = _core(args.db)
    delivered, remaining = core.flush_1c_queue()
    print(f"Доставлено: {delivered}, осталось в очереди: {remaining}")
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
    for path in _expand_xbir(args.files):
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


def cmd_operators(args) -> int:
    """Список операторов в справочнике."""
    core = _core(args.db)
    rows = core.storage.get_operators(active_only=False)
    if not rows:
        print("Справочник операторов пуст. Заведите: "
              "python src/import_operators.py --db ... --plan ...")
        return 0
    print(f"Операторов: {len(rows)}\n")
    for r in rows:
        mark = " " if r["is_active"] else "✗"
        print(f"  {mark} {r['operator_id']:<22} {r['full_name']}")
    return 0


def cmd_add_operator(args) -> int:
    """Завести одного оператора вручную (новый сотрудник)."""
    from import_operators import make_operator_id

    core = _core(args.db)
    taken = {r["operator_id"] for r in core.storage.get_operators(active_only=False)}
    operator_id = args.id or make_operator_id(args.name, taken)
    core.storage.upsert_operator(operator_id, args.name, is_active=True)
    print(f"✓ заведён оператор: {operator_id}  {args.name}")
    return 0


def cmd_import_operators(args) -> int:
    """Массовый импорт операторов из выгрузки 1С (обёртка над скриптом)."""
    from import_operators import import_operators, format_report

    core = _core(args.db)
    report = import_operators(
        core.storage, Path(args.plan),
        area=args.area,
        areas_path=Path(args.areas) if args.areas else None,
    )
    print(format_report(report, args.area))
    return 0


def cmd_drilling(args) -> int:
    """Нормирование участка присадки из выгрузки Nanxing (.SCX)."""
    from nanxing_parser import parse_order_folder, format_drilling_report

    panels, report = parse_order_folder(Path(args.folder))
    if not panels:
        print("Не найдено ни одного .SCX", file=sys.stderr)
        return 1
    print(format_drilling_report(panels, report))
    return 0


def cmd_rules(args) -> int:
    """Построить правила отбора деталей из справочника операций 1С."""
    from operation_rules import format_rules_report

    core = _core(args.db)
    rules, report, collisions = core.load_rules_from_1c(Path(args.areas))
    print(format_rules_report(report, collisions))
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

    ps = sub.add_parser("push", help="отправить статусы закрытых операций в 1С")
    ps.add_argument("--order", type=int, required=True)
    ps.add_argument("--ops", nargs="+", required=True)
    ps.set_defaults(fn=cmd_push)

    sub.add_parser("flush", help="повторить отправку очереди в 1С").set_defaults(
        fn=cmd_flush)

    rl = sub.add_parser("rules", help="правила отбора деталей из справочника 1С")
    rl.add_argument("--areas", required=True, help="xlsx «Операции по участкам»")
    rl.set_defaults(fn=cmd_rules)

    sub.add_parser("operators", help="список операторов").set_defaults(fn=cmd_operators)

    ao = sub.add_parser("add-operator", help="завести одного оператора")
    ao.add_argument("--name", required=True, help="ФИО оператора")
    ao.add_argument("--id", default=None, help="ID (по умолчанию из ФИО)")
    ao.set_defaults(fn=cmd_add_operator)

    io = sub.add_parser("import-operators", help="массовый импорт операторов из 1С")
    io.add_argument("--plan", required=True, help="xlsx «производственные операции»")
    io.add_argument("--areas", default=None, help="xlsx «Операции по участкам»")
    io.add_argument("--area", default=None, help="только операторы этого участка")
    io.set_defaults(fn=cmd_import_operators)

    dr = sub.add_parser("drilling", help="нормирование присадки из Nanxing .SCX")
    dr.add_argument("--folder", required=True, help="папка выгрузки Nanxing")
    dr.set_defaults(fn=cmd_drilling)

    sub.add_parser("stats", help="сводка по БД").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
