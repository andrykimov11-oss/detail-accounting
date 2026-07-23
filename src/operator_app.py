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

import sys
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, current_app, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent))

from pipeline import ProductionCore, qr_of  # noqa: E402
from scan_processor import FactStatus, ScanEvent, suggest_details  # noqa: E402
from storage import Storage  # noqa: E402


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

    def _core() -> ProductionCore:
        """Свежий ProductionCore на текущий запрос (своё соединение sqlite)."""
        return ProductionCore(Storage(current_app.config["DB_PATH"]))

    # --- Страница -----------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("operator.html")

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
                  operator_id: str, operation_1c: str):
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
        active = _active_orders.get(operator_id)
        detail_row = core.storage.get_detail_by_qr(qr_code)

        if active is not None:
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
        result = core.handle_scan(event, ctx)

        # Первый принятый скан фиксирует заказ смены.
        if result.status == FactStatus.ACCEPTED and active is None:
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
        import ipaddress  # noqa: F401
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None

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
        .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
        ]), critical=False)
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

    ssl_ctx = None
    if not _force_http:
        pair = _ensure_cert(Path(_db).resolve().parent / "certs")
        if pair:
            ssl_ctx = pair
            print(f"HTTPS включён (камера телефона работает). "
                  f"Открыть на телефоне: https://<IP-этого-ПК>:{_port}/")
        else:
            print("cryptography не установлена — запуск по HTTP, камера НЕ "
                  "заработает. Поставьте: pip install cryptography")
    if ssl_ctx is None:
        print(f"HTTP-режим. http://<IP-этого-ПК>:{_port}/  (только для отладки)")

    # threaded=True: телефонов несколько, запросы должны обслуживаться
    # параллельно. Соединение sqlite открывается на каждый запрос заново.
    create_app(_db).run(host="0.0.0.0", port=_port, debug=False,
                        threaded=True, ssl_context=ssl_ctx)
