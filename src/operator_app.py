"""
Веб-интерфейс оператора у станка.

Тонкая обёртка над ProductionCore: отдаёт JSON, всю бизнес-логику держит
ядро. Клиент — браузер телефона оператора: страница открывается в мобильном
браузере, QR сканируется камерой (html5-qrcode в templates/operator.html).

Развёртывание: один сервер в цехе + телефоны как браузерные клиенты в общей
Wi-Fi. База одна, поэтому счётчик по заказу общий. Камере нужен HTTPS —
сервер поднимается с самоподписанным сертификатом (см. _ensure_cert и
docs/Развёртывание_пилот.md).

Поток экранов на одной странице (см. templates/operator.html):
    вход (выбор себя) → выбор участка (open_shift) → рабочий экран (сканы).

Ключевое решение backend: оператор НЕ выбирает заказ руками. Заказ
определяется по отсканированной детали (QR → order_num). А ядро (handle_scan)
работает в контексте одного заказа, поэтому здесь есть резолвер QR → заказ по
всей базе и понятие «активный заказ смены»: первый успешный скан фиксирует
заказ, последующие сверяются с ним, «Новый заказ» сбрасывает.
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from flask import Flask, current_app, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent))

from pipeline import ProductionCore, qr_of  # noqa: E402
from scan_processor import FactStatus, ScanEvent, suggest_details  # noqa: E402
from storage import Storage  # noqa: E402

# Операция упаковки (пилотная): подетальный контроль комплектности заказа.
PACK_OP = "Упаковка раскроя"

# GUID детали (как в .xbir/на бирке Базиса): 8-4-4-4-12 hex.
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# «Активный заказ смены» на оператора. Живёт в памяти процесса: это оперативное
# состояние рабочего места, а не учётные данные (факт лежит в БД и переживает
# перезапуск). Первый успешный скан фиксирует заказ, «Новый заказ» — сбрасывает.
_active_orders: dict[str, int] = {}


def create_app(db_path: str | Path = "prod.db") -> Flask:
    """
    Собрать Flask-приложение поверх БД по пути db_path.

    Соединение со Storage открывается на каждый запрос заново (см. _core):
    так интерфейс безопасен к многопоточному dev-серверу sqlite и остаётся
    простым — запрос оператора короткий, накладные расходы незаметны.
    """
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    # PIN админки: из окружения DA_ADMIN_PIN, иначе "0000". Сменить в бою.
    app.config["ADMIN_PIN"] = os.environ.get("DA_ADMIN_PIN", "0000")

    def _core() -> ProductionCore:
        """Свежий ProductionCore на текущий запрос (своё соединение sqlite)."""
        return ProductionCore(Storage(current_app.config["DB_PATH"]))

    # --- Страница -----------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("operator.html")

    # --- Админка: управление операторами ------------------------------------
    #
    # Простая веб-страница технолога: список операторов, добавление нового,
    # включение/выключение. Защита — PIN (config ADMIN_PIN, по умолчанию
    # "0000"): в изолированной сети цеха этого достаточно, полноценная
    # авторизация — на промышленный этап.

    def _check_pin() -> bool:
        want = str(current_app.config.get("ADMIN_PIN", "0000"))
        got = request.headers.get("X-Admin-Pin", "")
        return got == want

    @app.get("/admin")
    def admin_page():
        return render_template("admin.html")

    @app.get("/api/admin/operators")
    def api_admin_operators():
        """Все операторы (включая выключенных) — для страницы админки."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        core = _core()
        try:
            return jsonify([
                {"operator_id": o["operator_id"], "full_name": o["full_name"],
                 "is_active": bool(o["is_active"])}
                for o in core.storage.get_operators(active_only=False)
            ])
        finally:
            core.storage.close()

    @app.post("/api/admin/add-operator")
    def api_admin_add_operator():
        """Завести нового оператора по ФИО. ID генерируется автоматически."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        from import_operators import make_operator_id  # noqa: PLC0415

        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("full_name") or "").strip()
        if len(name) < 3:
            return jsonify({"error": "введите ФИО"}), 400
        core = _core()
        try:
            taken = {o["operator_id"]
                     for o in core.storage.get_operators(active_only=False)}
            operator_id = make_operator_id(name, taken)
            core.storage.upsert_operator(operator_id, name, is_active=True)
            return jsonify({"operator_id": operator_id, "full_name": name})
        finally:
            core.storage.close()

    @app.post("/api/admin/set-active")
    def api_admin_set_active():
        """Включить/выключить оператора (не удаляем — сохраняем историю)."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        data = request.get_json(force=True, silent=True) or {}
        operator_id = data.get("operator_id", "")
        active = bool(data.get("is_active", True))
        core = _core()
        try:
            op = core.storage.get_operator(operator_id)
            if op is None:
                return jsonify({"error": "оператор не найден"}), 404
            core.storage.upsert_operator(operator_id, op["full_name"], is_active=active)
            return jsonify({"operator_id": operator_id, "is_active": active})
        finally:
            core.storage.close()

    @app.post("/api/admin/delete-operator")
    def api_admin_delete_operator():
        """Удалить оператора (для ошибочных записей; штатно — выключение)."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        data = request.get_json(force=True, silent=True) or {}
        core = _core()
        try:
            core.storage.delete_operator(data.get("operator_id", ""))
            return jsonify({"ok": True})
        finally:
            core.storage.close()

    # --- Админка: настройки путей -------------------------------------------

    @app.get("/api/admin/settings")
    def api_admin_settings():
        """Все настройки (пути к 1С, Базису, логам станков)."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        core = _core()
        try:
            return jsonify(core.storage.all_settings())
        finally:
            core.storage.close()

    @app.post("/api/admin/settings")
    def api_admin_set_settings():
        """Сохранить настройки (словарь ключ→значение)."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        data = request.get_json(force=True, silent=True) or {}
        core = _core()
        try:
            for key, value in data.items():
                core.storage.set_setting(str(key), str(value))
            return jsonify(core.storage.all_settings())
        finally:
            core.storage.close()

    # --- Админка: справочник участков ---------------------------------------

    @app.get("/api/admin/areas")
    def api_admin_areas():
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        core = _core()
        try:
            out = []
            for row in core.storage.get_all_areas():
                ops = core.storage.get_operations_by_area(row["area_id"])
                out.append({"area_id": row["area_id"],
                            "area_name": row["area_name"], "operations": ops})
            return jsonify(out)
        finally:
            core.storage.close()

    @app.post("/api/admin/area-operation")
    def api_admin_area_op():
        """Добавить операцию к участку (создаёт участок, если нового id)."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        data = request.get_json(force=True, silent=True) or {}
        area_id = (data.get("area_id") or "").strip()
        area_name = (data.get("area_name") or "").strip()
        operation = (data.get("operation_1c") or "").strip()
        if not (area_id and area_name and operation):
            return jsonify({"error": "нужны area_id, area_name, operation_1c"}), 400
        core = _core()
        try:
            core.storage.upsert_area_operation(area_id, area_name, operation)
            return jsonify({"ok": True})
        finally:
            core.storage.close()

    @app.post("/api/admin/area-operation-delete")
    def api_admin_area_op_delete():
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        data = request.get_json(force=True, silent=True) or {}
        core = _core()
        try:
            core.storage.delete_area_operation(
                data.get("area_id", ""), data.get("operation_1c", ""))
            return jsonify({"ok": True})
        finally:
            core.storage.close()

    @app.post("/api/admin/area-delete")
    def api_admin_area_delete():
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        data = request.get_json(force=True, silent=True) or {}
        core = _core()
        try:
            removed = core.storage.delete_area(data.get("area_id", ""))
            return jsonify({"removed": removed})
        finally:
            core.storage.close()

    # --- Админка: отчёты -----------------------------------------------------

    @app.get("/api/admin/report/order")
    def api_admin_report_order():
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        from reports import order_report  # noqa: PLC0415

        try:
            order_num = int(request.args.get("order_num", "0"))
        except ValueError:
            return jsonify({"error": "order_num должен быть числом"}), 400
        core = _core()
        try:
            rep = order_report(core, order_num)
            return jsonify({
                "order_num": rep.order_num,
                "order_full_num": rep.order_full_num,
                "client_name": rep.client_name,
                "planned_total": rep.planned_total,
                "scanned_total": rep.scanned_total,
                "operations": [
                    {"operation_1c": o.operation_1c, "planned": o.planned,
                     "scanned": o.scanned, "pct": o.pct, "status": o.status,
                     "lost": o.lost}
                    for o in rep.operations
                ],
            })
        finally:
            core.storage.close()

    @app.get("/api/admin/report/shift")
    def api_admin_report_shift():
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        from reports import shift_report  # noqa: PLC0415

        core = _core()
        try:
            rep = shift_report(core)
            return jsonify({
                "orders": len(rep.orders),
                "scans_total": rep.scans_total,
                "accepted": rep.accepted,
                "duplicate": rep.duplicate,
                "overplan": rep.overplan,
                "errors": rep.errors,
                "anomalies": rep.anomalies,
                "anomaly_pct": rep.anomaly_pct,
                "by_operator": rep.by_operator.most_common(),
                "by_operation": rep.by_operation.most_common(),
            })
        finally:
            core.storage.close()

    @app.get("/api/admin/report/shift.xlsx")
    def api_admin_report_shift_xlsx():
        """Выгрузка сводки смены в xlsx — для журнала пилота."""
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        import tempfile  # noqa: PLC0415
        from flask import send_file  # noqa: PLC0415
        from reports import export_shift_xlsx, shift_report  # noqa: PLC0415

        core = _core()
        try:
            rep = shift_report(core)
            out = Path(tempfile.gettempdir()) / "shift_report.xlsx"
            export_shift_xlsx(rep, out)
            return send_file(out, as_attachment=True,
                             download_name="Отчёт_смены.xlsx")
        finally:
            core.storage.close()

    @app.post("/api/admin/run-import")
    def api_admin_run_import():
        """
        Запустить связку + импорт заказов по путям из настроек.

        Читает настройки basis_xbir (папка/файл .xbir) и one_c_plan (выгрузка
        1С), разрешает связки и импортирует связанные заказы. Кнопка в
        админке вместо ручной команды в терминале.
        """
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        from one_c_loader import load_production_plan  # noqa: PLC0415
        from xbir_parser import parse_xbir  # noqa: PLC0415

        core = _core()
        try:
            settings = core.storage.all_settings()
            basis = settings.get("basis_xbir", "").strip()
            plan = settings.get("one_c_plan", "").strip()
            if not basis or not plan:
                return jsonify({"error": "задайте пути basis_xbir и one_c_plan "
                                "в настройках"}), 400

            basis_path = Path(basis)
            files = (sorted(basis_path.rglob("*.xbir")) +
                     sorted(basis_path.rglob("*.XBIR"))) \
                if basis_path.is_dir() else [basis_path]

            one_c, _ = load_production_plan(Path(plan))
            xbir_orders: dict[int, str] = {}
            for f in files:
                if not f.is_file():
                    continue
                for d in parse_xbir(f)[0]:
                    if d.order_num is not None:
                        xbir_orders.setdefault(d.order_num, d.order_raw.strip())

            links = core.resolve_links(xbir_orders, one_c)
            resolved = sum(1 for r in links if r.is_resolved)
            res = core.import_xbir(files, require_link=True)
            return jsonify({
                "orders_found": len(xbir_orders),
                "links_resolved": resolved,
                "links_manual": len(xbir_orders) - resolved,
                "details_imported": res.details_imported,
                "skipped_unlinked": len(res.skipped_unlinked),
            })
        finally:
            core.storage.close()

    @app.post("/api/admin/pull-1c-ftp")
    def api_admin_pull_ftp():
        """
        Забрать выгрузку 1С с FTP (rascroi.ru) в папку incoming рядом с БД и
        прописать путь в настройку one_c_plan, чтобы «Связать и импортировать»
        подхватило файл. Пароль — из окружения DA_FTP_PASS, не из веб-настроек.
        """
        if not _check_pin():
            return jsonify({"error": "неверный PIN"}), 403
        from fetch_sources import FtpConfig, fetch_files  # noqa: PLC0415

        core = _core()
        try:
            s = core.storage.all_settings()
            host = (s.get("ftp_host") or "").strip()
            files = [f.strip() for f in (s.get("ftp_files") or "").split(",")
                     if f.strip()]
            if not host:
                return jsonify({"error": "задайте ftp_host в настройках"}), 400
            if not files:
                return jsonify({"error": "задайте ftp_files (имена через запятую)"}), 400

            cfg = FtpConfig(
                host=host,
                user=(s.get("ftp_user") or "anonymous"),
                password=os.environ.get("DA_FTP_PASS", ""),
                directory=(s.get("ftp_dir") or "").strip(),
                filenames=files,
                secure=(s.get("ftp_secure", "1") != "0"),
            )
            incoming = Path(current_app.config["DB_PATH"]).resolve().parent / "incoming"
            saved = fetch_files(cfg, incoming)

            # первый файл считаем «производственные операции» → путь для импорта
            if saved:
                core.storage.set_setting("one_c_plan", str(saved[0]))
            return jsonify({"downloaded": [p.name for p in saved],
                            "dir": str(incoming),
                            "one_c_plan": str(saved[0]) if saved else ""})
        except Exception as e:  # noqa: BLE001 — вернуть причину технологу
            return jsonify({"error": f"FTP: {e}"}), 502
        finally:
            core.storage.close()

    # --- Справочники --------------------------------------------------------

    @app.get("/api/operators")
    def api_operators():
        """39 физлиц из 1С для экрана входа (авторизация без пароля)."""
        core = _core()
        try:
            ops = core.storage.get_operators(active_only=True)
            return jsonify([
                {"operator_id": o["operator_id"], "full_name": o["full_name"]}
                for o in ops
            ])
        finally:
            core.storage.close()

    @app.get("/api/areas")
    def api_areas():
        """Участки смены для экрана выбора участка."""
        core = _core()
        try:
            return jsonify(_areas_payload(core))
        finally:
            core.storage.close()

    @app.post("/api/login")
    def api_login():
        """Оператор выбрал себя. Отдаём его данные и список участков."""
        data = request.get_json(force=True, silent=True) or {}
        operator_id = data.get("operator_id", "")
        core = _core()
        try:
            op = core.storage.get_operator(operator_id)
            if op is None:
                return jsonify({"error": "оператор не найден"}), 404
            return jsonify({
                "operator": {"operator_id": op["operator_id"],
                             "full_name": op["full_name"]},
                "areas": _areas_payload(core),
            })
        finally:
            core.storage.close()

    @app.post("/api/shift")
    def api_shift():
        """
        Оператор заступил на участок. Открываем смену и отдаём операции
        участка — из них оператор выберет текущую перед сканами.
        """
        data = request.get_json(force=True, silent=True) or {}
        operator_id = data.get("operator_id", "")
        area_id = data.get("area_id", "")

        core = _core()
        try:
            # Одна открытая смена на оператора: смена участка закрывает прежнюю,
            # иначе get_open_shift вернёт неоднозначность.
            prev = core.storage.get_open_shift(operator_id)
            if prev is not None:
                core.storage.close_shift(prev["session_id"])

            core.storage.open_shift(str(uuid.uuid4()), operator_id, area_id)
            # Новая смена — новый рабочий контекст: сбрасываем активный заказ.
            _active_orders.pop(operator_id, None)

            return jsonify({
                "area": {"area_id": area_id, "area_name": _area_name(core, area_id)},
                "operations": core.area_operations(area_id),
            })
        finally:
            core.storage.close()

    @app.post("/api/scan")
    def api_scan():
        """
        Обработать скан. Тело: {qr_code, area_id, operator_id, operation_1c}.
        Резолвит QR → заказ, обрабатывает через ядро, возвращает результат
        с крупной обратной связью и — при ошибке распознавания — fallback-
        списком деталей заказа.
        """
        data = request.get_json(force=True, silent=True) or {}
        return _process_scan(
            _core(),
            qr_code=str(data.get("qr_code", "")).strip(),
            area_id=data.get("area_id", ""),
            operator_id=data.get("operator_id", ""),
            operation_1c=data.get("operation_1c", ""),
            confirm_switch=bool(data.get("switch", False)),
        )

    @app.post("/api/scan-qty")
    def api_scan_qty():
        """
        Скан пачкой: оператор пикнул одну бирку стопки одинаковых деталей и
        ввёл количество. Тело: {qr_code, count, area_id, operator_id, operation_1c}.
        Заносит сразу count экземпляров, окно дублей не проверяется.
        """
        data = request.get_json(force=True, silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        return _process_scan(
            _core(),
            qr_code=str(data.get("qr_code", "")).strip(),
            area_id=data.get("area_id", ""),
            operator_id=data.get("operator_id", ""),
            operation_1c=data.get("operation_1c", ""),
            count=max(1, count),
            check_duplicate=False,
            confirm_switch=bool(data.get("switch", False)),
        )

    @app.post("/api/pick-detail")
    def api_pick_detail():
        """
        Оператор пальцем выбрал деталь из fallback-списка. Засчитываем по ней:
        восстанавливаем QR детали из её UID и прогоняем как обычный скан.
        """
        data = request.get_json(force=True, silent=True) or {}
        detail_uid = data.get("detail_uid", "")
        return _process_scan(
            _core(),
            qr_code=qr_of(detail_uid),
            area_id=data.get("area_id", ""),
            operator_id=data.get("operator_id", ""),
            operation_1c=data.get("operation_1c", ""),
        )

    @app.post("/api/new-order")
    def api_new_order():
        """Сбросить активный заказ смены: следующий скан задаст новый."""
        data = request.get_json(force=True, silent=True) or {}
        _active_orders.pop(data.get("operator_id", ""), None)
        return jsonify({"ok": True})

    @app.get("/api/status")
    def api_status():
        """Сводка план/факт по операциям заказа — для панели прогресса."""
        try:
            order_num = int(request.args.get("order_num", ""))
        except (TypeError, ValueError):
            return jsonify({"error": "order_num обязателен"}), 400

        core = _core()
        try:
            ctx = core.load_order(order_num)
            # Операции берём из справочника всех участков: покажем каждую, по
            # которой в заказе есть плановые детали.
            operations = _all_operations(core)
            statuses = core.order_status(ctx, operations)
            rows = [
                {
                    "operation_1c": s.operation_1c,
                    "status": s.status.value,
                    "planned_total": s.planned_total,
                    "scanned_total": s.scanned_total,
                    "progress_pct": s.progress_pct,
                }
                for s in statuses if s.planned_total > 0
            ]
            return jsonify({"order_num": order_num, "operations": rows})
        finally:
            core.storage.close()

    # --- Дашборд участка упаковки -------------------------------------------

    @app.get("/packing")
    def packing_page():
        return render_template("packing.html")

    @app.get("/api/packing")
    def api_packing():
        """
        Данные дашборда упаковки: текущий заказ (прогресс + спецификация деталей
        по цветам + флаги маршрута) и очередь заказов на упаковку по сроку.
        """
        core = _core()
        try:
            today = date.today().isoformat()
            orders = []
            for num in core.storage.get_order_numbers():
                details = core.storage.get_details_by_order(num)
                if not details:
                    continue
                facts = {f["detail_uid"]: f["scanned_count"]
                         for f in core.storage.get_facts_by_order(num)
                         if f["operation_1c"] == PACK_OP}
                losses = core.storage.get_losses(num, PACK_OP)
                spec, planned_total, scanned_total = [], 0, 0
                all_covered = True
                for d in details:
                    qty = d["qty"] or 0
                    sc = min(facts.get(d["detail_uid"], 0), qty)
                    miss = losses.get(d["detail_uid"], 0)
                    planned_total += qty
                    scanned_total += sc
                    covered = sc + miss >= qty          # набрано или списано в недостачу
                    if qty and not covered:
                        all_covered = False
                    color = ("green" if qty and sc >= qty
                             else "missing" if miss > 0 and covered
                             else "yellow" if sc > 0 else "red")
                    spec.append({
                        "uid": d["detail_uid"],
                        "pos": d["pos_no"],
                        "size": f'{int(d["length"] or 0)}×{int(d["width"] or 0)}'
                                f'×{int(d["thickness"] or 0)}',
                        "material": d["material_name"] or "",
                        "qty": qty, "scanned": sc, "missing": miss, "color": color,
                    })
                link = core.storage.get_order_link(num)
                deadline = (link["deadline"] if link else "") or ""
                complete = planned_total > 0 and all_covered
                flags = (link["route_flags"].split(",")
                         if link and link["route_flags"] else [])
                orders.append({
                    "order_num": num,
                    "order_full_num": (link["order_full_num"] if link else "") or str(num),
                    "client": (link["client_name"] if link else "") or "",
                    "deadline": deadline,
                    "overdue": bool(deadline) and deadline < today and not complete,
                    "route_flags": flags,
                    "scanned_total": scanned_total, "planned_total": planned_total,
                    "pct": round(scanned_total / planned_total * 100) if planned_total else 0,
                    "complete": complete,
                    "in_progress": 0 < scanned_total < planned_total and not complete,
                    "spec": spec,
                })

            def dl_key(o):
                return (o["deadline"] or "9999-99-99", o["order_num"])

            by_num = {o["order_num"]: o for o in orders}
            # Текущий = заказ последнего скана упаковки (следует за работой),
            # если он ещё не закрыт; иначе — ближайший по сроку незакрытый.
            last = core.storage.get_last_active_order(PACK_OP)
            current = by_num.get(last) if (last in by_num and not by_num[last]["complete"]) else None
            if current is None:
                inprog = sorted([o for o in orders if o["in_progress"]], key=dl_key)
                notdone = sorted([o for o in orders if not o["complete"]], key=dl_key)
                current = inprog[0] if inprog else (notdone[0] if notdone else None)
            cur_num = current["order_num"] if current else None

            def light(o):
                return {k: v for k, v in o.items() if k != "spec"}

            # Отложенные — начатые, но не законченные (ждут остальные детали).
            deferred = [light(o) for o in sorted(
                (o for o in orders if o["in_progress"] and o["order_num"] != cur_num),
                key=dl_key)]
            # Очередь — ещё не начатые на упаковке.
            queue = [light(o) for o in sorted(
                (o for o in orders if not o["complete"] and o["scanned_total"] == 0
                 and o["order_num"] != cur_num), key=dl_key)]
            return jsonify({"current": current, "deferred": deferred,
                            "queue": queue, "today": today})
        finally:
            core.storage.close()

    @app.post("/api/mark-missing")
    def api_mark_missing():
        """
        Отметить недостачу детали на упаковке (не дошла / потеряна). Позволяет
        закрыть заказ с недостачей; запись идёт в отчёт по потерям.
        """
        data = request.get_json(force=True, silent=True) or {}
        detail_uid = str(data.get("detail_uid", "")).strip()
        try:
            order_num = int(data.get("order_num"))
        except (TypeError, ValueError):
            return jsonify({"error": "order_num обязателен"}), 400
        if not detail_uid:
            return jsonify({"error": "detail_uid обязателен"}), 400
        try:
            count = max(1, int(data.get("count", 1)))
        except (TypeError, ValueError):
            count = 1
        core = _core()
        try:
            core.storage.mark_loss(
                order_num, data.get("operation_1c") or PACK_OP, detail_uid,
                count, operator_id=data.get("operator_id", ""),
                note=data.get("note", ""))
            return jsonify({"ok": True})
        finally:
            core.storage.close()

    # --- Монитор: заказы в работе и степень закрытия ------------------------

    @app.get("/board")
    def board_page():
        return render_template("board.html")

    @app.get("/api/board")
    def api_board():
        """Все заказы в базе с прогрессом по операциям — для экрана-монитора."""
        core = _core()
        try:
            operations = _all_operations(core)
            out = []
            for num in core.storage.get_order_numbers():
                ctx = core.load_order(num)
                statuses = [s for s in core.order_status(ctx, operations)
                            if s.planned_total > 0]
                link = core.storage.get_order_link(num)
                ops = [{
                    "operation": s.operation_1c,
                    "scanned": s.scanned_total,
                    "planned": s.planned_total,
                    "pct": s.progress_pct,
                    "status": s.status.value,
                } for s in statuses]
                tot_p = sum(s.planned_total for s in statuses)
                tot_s = sum(min(s.scanned_total, s.planned_total) for s in statuses)
                out.append({
                    "order_num": num,
                    "order_full_num": (link["order_full_num"] if link else "") or str(num),
                    "client": (link["client_name"] if link else "") or "",
                    "order_pct": round(tot_s / tot_p * 100) if tot_p else 0,
                    "done": tot_s, "total": tot_p,
                    "operations": ops,
                })
            # незакрытые сверху (по возрастанию % готовности)
            out.sort(key=lambda o: (o["order_pct"], o["order_num"]))
            return jsonify({"orders": out})
        finally:
            core.storage.close()

    return app


# --- Вспомогательное ---------------------------------------------------------

def _areas_payload(core: ProductionCore) -> list[dict]:
    return [
        {"area_id": a["area_id"], "area_name": a["area_name"]}
        for a in core.storage.get_all_areas()
    ]


def _area_name(core: ProductionCore, area_id: str) -> str:
    for a in core.storage.get_all_areas():
        if a["area_id"] == area_id:
            return a["area_name"]
    return area_id


def _all_operations(core: ProductionCore) -> list[str]:
    """Все операции всех участков (без дублей), для расчёта статуса заказа."""
    seen: list[str] = []
    for a in core.storage.get_all_areas():
        for op in core.storage.get_operations_by_area(a["area_id"]):
            if op not in seen:
                seen.append(op)
    return seen


def _detail_view(core: ProductionCore, qr_code: str) -> dict | None:
    """Компактное представление детали по QR — для строки обратной связи."""
    row = core.storage.get_detail_by_qr(qr_code)
    if row is None:
        return None
    return {
        "detail_uid": row["detail_uid"],
        "pos_no": row["pos_no"],
        "length": row["length"],
        "width": row["width"],
        "thickness": row["thickness"],
        "material": row["material_name"],
        "qty": row["qty"],
    }


def _process_scan(core: ProductionCore, *, qr_code: str, area_id: str,
                  operator_id: str, operation_1c: str,
                  count: int = 1, check_duplicate: bool = True,
                  confirm_switch: bool = False):
    """
    Общее тело обработки скана и «выбора детали пальцем».

    Заказ определяется так:
      - если у оператора уже есть активный заказ смены — работаем в нём
        (посторонний QR отсеется ядром и покажет fallback этого заказа);
      - иначе резолвим QR → заказ и, если скан примут, фиксируем активный;
      - если QR не резолвится и активного заказа ещё нет — сказать оператору
        отсканировать деталь известного заказа (заказ определить не по чему).
    """
    try:
        # Бирка Базиса может нести в QR сам GUID детали, а не MD5(GUID)[:10].
        # Приводим к каноническому qr_code, дальше всё работает как обычно.
        code = (qr_code or "").strip()
        if _GUID_RE.match(code):
            row = core.storage.get_detail_by_uid(code)
            if row is not None:
                code = row["qr_code"]
        qr_code = code

        active = _active_orders.get(operator_id)
        detail_row = core.storage.get_detail_by_qr(qr_code)

        # Защита от чужого паллета: деталь известна, но из ДРУГОГО заказа, чем
        # тот, что оператор пакует сейчас. Не переключаемся молча — просим
        # подтверждение, иначе деталь уйдёт в чужой паллет.
        if (not confirm_switch and active is not None and detail_row is not None
                and detail_row["order_num"] != active):
            other = detail_row["order_num"]
            link = core.storage.get_order_link(other)
            full = (link["order_full_num"] if link else "") or str(other)
            act_link = core.storage.get_order_link(active)
            act_full = (act_link["order_full_num"] if act_link else "") or str(active)
            return jsonify({
                "status": "other_order",
                "message": f"Деталь из заказа {full}, а вы пакуете {act_full}. "
                           f"Перейти к заказу {full}?",
                "switch_order_num": other,
                "switch_order_full": full,
                "detail": _detail_view(core, qr_code),
                "scanned_count": 0, "planned_qty": 0, "order_num": active,
                "suggest": [],
            })

        if confirm_switch and detail_row is not None:
            order_num = detail_row["order_num"]      # подтверждённый переход
        elif active is not None:
            order_num = active
        elif detail_row is not None:
            order_num = detail_row["order_num"]
        else:
            return jsonify({
                "status": "no_order",
                "message": "Активный заказ не задан. Отсканируйте деталь заказа.",
                "detail": None,
                "scanned_count": 0,
                "planned_qty": 0,
                "order_num": None,
                "suggest": [],
            })

        ctx = core.load_order(order_num)
        event = ScanEvent(
            scan_id=str(uuid.uuid4()),
            qr_code=qr_code,
            area_id=area_id,
            operator_id=operator_id,
            operation_1c=operation_1c,
            scanned_at=datetime.now(),
        )
        result = core.handle_scan(event, ctx, count=count,
                                  check_duplicate=check_duplicate)

        # Принятый скан фиксирует текущий заказ (в т.ч. после подтверждённого
        # перехода на другой заказ) — дальнейшие сканы идут в него.
        if result.status == FactStatus.ACCEPTED:
            _active_orders[operator_id] = order_num

        suggest = (
            suggest_details(ctx, operation_1c)
            if result.suggest_detail_list else []
        )

        return jsonify({
            "status": result.status.value,
            "message": result.message,
            "detail": _detail_view(core, qr_code),
            "detail_uid": result.detail_uid,
            "scanned_count": result.scanned_count,
            "planned_qty": result.planned_qty,
            "order_num": order_num,
            "operation_1c": operation_1c,
            "anomaly": result.anomaly,
            "suggest": suggest,
        })
    finally:
        core.storage.close()


def _ensure_cert(cert_dir: Path) -> tuple[str, str] | None:
    """
    Гарантировать наличие самоподписанного TLS-сертификата.

    Камера телефона доступна из браузера ТОЛЬКО по HTTPS (требование
    безопасности браузеров: getUserMedia работает лишь в защищённом
    контексте). Для пилота в локальной сети цеха достаточно самоподписанного
    сертификата — он вызовет предупреждение браузера один раз, оператор
    нажимает «всё равно продолжить», дальше работает.

    Возвращает (cert, key) или None, если cryptography недоступна — тогда
    сервер поднимется по HTTP (камера не заработает, но API останется живым
    для отладки со сканером-клавиатурой).
    """
    cert = cert_dir / "operator_cert.pem"
    key = cert_dir / "operator_key.pem"
    if cert.exists() and key.exists():
        return str(cert), str(key)

    try:
        import datetime as _dt
        import ipaddress
        import socket
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        return None

    # Собираем все IPv4 машины. iOS/Safari требует, чтобы адрес, по которому
    # заходит телефон (напр. 192.168.1.86), был вписан в SubjectAlternativeName.
    # Без этого браузер блокирует наглухо, даже без кнопки «перейти».
    ips: set[str] = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if ":" not in addr:
                ips.add(addr)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass

    san = [x509.DNSName("localhost")]
    for ip in sorted(ips):
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue

    now = _dt.datetime.now(_dt.timezone.utc)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "detail-accounting-pilot"),
    ])
    cert_obj = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        # iOS доверяет TLS-сертификатам сроком не больше 825 дней — берём 397.
        .not_valid_after(now + _dt.timedelta(days=397))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False)
        .sign(private_key, hashes.SHA256())
    )
    cert_dir.mkdir(parents=True, exist_ok=True)
    key.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    cert.write_bytes(cert_obj.public_bytes(serialization.Encoding.PEM))
    return str(cert), str(key)


# Запуск: python3 src/operator_app.py [db_path] [port] [--http]
if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    _db = args[0] if len(args) > 0 else "prod.db"
    _port = int(args[1]) if len(args) > 1 else 5001
    _force_http = "--http" in sys.argv

    def _lan_ip() -> str:
        import socket as _s
        try:
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "<IP-этого-ПК>"

    ssl_ctx = None
    if not _force_http:
        pair = _ensure_cert(Path(_db).resolve().parent / "certs")
        if pair:
            ssl_ctx = pair
        else:
            print("cryptography не установлена — запуск по HTTP, камера НЕ "
                  "заработает. Поставьте: pip install cryptography")

    _ip = _lan_ip()
    _scheme = "https" if ssl_ctx else "http"
    print("=" * 54)
    print("  Адреса (та же Wi-Fi, Android + Chrome):")
    print(f"    {_scheme}://{_ip}:{_port}/         — рабочее место оператора")
    print(f"    {_scheme}://{_ip}:{_port}/packing  — дашборд упаковки")
    print(f"    {_scheme}://{_ip}:{_port}/admin    — админка")
    if ssl_ctx:
        print("  Сертификат самоподписанный — принять предупреждение в браузере.")
    print("=" * 54)

    # threaded=True: телефонов несколько, запросы должны обслуживаться
    # параллельно. Соединение sqlite открывается на каждый запрос заново.
    create_app(_db).run(host="0.0.0.0", port=_port, debug=False,
                        threaded=True, ssl_context=ssl_ctx)
