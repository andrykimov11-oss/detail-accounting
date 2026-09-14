"""
Рабочие места первого этапа ПСР: оператор участка и начальник цеха.

ПОЧЕМУ ОТДЕЛЬНО ОТ operator_app
    `operator_app` — рабочее место ПОДЕТАЛЬНОГО учёта: «сканируй деталь,
    вот счётчик N из M». Оно останется и понадобится на шаге 5.

    Здесь — рабочее место ПОЗАКАЗНОГО учёта: «отсканируй бланк заказа,
    возьми в работу, закончи». Это разные экраны для разных моментов
    жизни предприятия, и склеивать их в один с переключателем значило бы
    сделать оба непонятными.

    Общее у них — данные: одна база, один справочник участков, одна
    очередь в 1С. Разделены рабочие места, а не система (D-208).

ДВА ЭКРАНА

    /psr/operator   — оператор участка. Скан бланка, список взятых
                      в работу заказов, кнопка «закончил».
    /psr/chief      — начальник цеха. Позаказная картина по шести
                      участкам, справочный состав заказа из .xbir,
                      накопленная статистика.

СОЕДИНЕНИЕ С БД — НА КАЖДЫЙ ЗАПРОС
    Так же, как в `operator_app`: Storage открывается заново на каждый
    запрос по пути из config["DB_PATH"]. Общее соединение sqlite между
    потоками dev-сервера небезопасно, и ядро это соглашение уже приняло.
    Первая редакция этого модуля держала одно соединение в config —
    ошибка найдена при подключении экранов, до запуска.

ЧЕГО ЗДЕСЬ НЕТ
    Прав по ролям (SR-96 — SR-100) и входа по бейджу. Это следующая
    задача шага 1; пока оператор называется полем на экране. Делать
    вид, что права есть, нельзя — поэтому их нет явно, а не наполовину.
"""
from __future__ import annotations

import sys
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent))

from area_measures import AreaMeasures, MeasureNotSet  # noqa: E402
from storage import Storage  # noqa: E402
from order_registration import (  # noqa: E402
    OrderAlreadyClosed,
    OrderNotOpen,
    OrderRegistration,
)
from order_scan import (  # noqa: E402
    OrderNotInShiftTask,
    OrderScanResolver,
    ScanNotRecognized,
)

psr = Blueprint("psr", __name__, url_prefix="/psr")

AREA_NAMES = {
    "raskroy": "Раскрой",
    "kromlenie": "Кромление",
    "frezerovanie": "Фрезерование",
    "prisadka": "Присадка",
    "sborka": "Сборка",
    "sklad": "Склад готовой продукции",
}


def _storage():
    """Свежий Storage на текущий запрос — соглашение ядра."""
    from flask import current_app
    return Storage(current_app.config["DB_PATH"])


def _services(storage):
    measures = AreaMeasures(storage)
    return (OrderRegistration(storage, measures=measures),
            OrderScanResolver(storage),
            measures)


def _order_card(storage, measures, order_num: int, area_id: str) -> dict:
    """
    Справочная карточка заказа: то, что видно оператору после скана.

    Состав подтягивается из спецификации БАЗИС (.xbir). Он СПРАВОЧНЫЙ:
    на шаге 1 оператор ничего по деталям не отмечает, но видеть, сколько
    их и на сколько метров кромки, ему нужно — и это же готовит цех
    к шагу 5, где детали станут предметом отметки.
    """
    rows = storage.get_details_by_order(order_num)
    link = storage.get_order_link(order_num)
    card = {
        "order_num": order_num,
        "doc_num": link["order_full_num"] if link else "",
        "doc_date": (link["order_date"] or "")[:10] if link else "",
        "client": link["client_name"] if link else "",
        "unique_details": len({r["detail_uid"] for r in rows}),
        "total_items": sum((r["qty"] or 0) for r in rows),
        "measure_name": None,
        "measure_value": None,
        "measure_note": None,
    }
    try:
        m = measures.get_measure(area_id)
        card["measure_name"] = m.measure_name
        card["measure_value"] = measures.value_for_order(area_id, order_num)
    except MeasureNotSet:
        card["measure_note"] = "измеритель участка не задан технологом"
    except Exception as exc:
        card["measure_note"] = str(exc)
    return card


# ----------------------------------------------------------------------
# Экран оператора участка
# ----------------------------------------------------------------------
@psr.route("/operator")
def operator_screen():
    st = _storage()
    area_id = request.args.get("area", "kromlenie")
    reg, _, measures = _services(st)
    open_sessions = [
        {**s.__dict__, **_order_card(st, measures, s.order_num, area_id)}
        for s in reg.open_sessions(area_id)
    ]
    return render_template("psr_operator.html",
                           area_id=area_id,
                           area_name=AREA_NAMES.get(area_id, area_id),
                           areas=AREA_NAMES,
                           open_sessions=open_sessions)


@psr.route("/api/scan", methods=["POST"])
def api_scan():
    """
    Скан бланка заказа.

    Одно действие — два возможных смысла: если заказ на участке не
    открыт, скан ОТКРЫВАЕТ его; если открыт, скан предлагает закрыть.
    Оператору не надо помнить, какую кнопку нажать: он делает то же
    движение, что и всегда, а решение принимает система по состоянию.
    """
    st = _storage()
    data = request.get_json(silent=True) or {}
    raw = data.get("code", "")
    area_id = data.get("area", "kromlenie")
    operator_id = data.get("operator", "")

    reg, resolver, measures = _services(st)
    try:
        order_num = resolver.resolve(raw, area_id,
                                     shift_task=data.get("shift_task"))
    except (ScanNotRecognized, OrderNotInShiftTask) as exc:
        return jsonify(ok=False, error=str(exc)), 200

    existing = st.get_open_order_session(order_num, area_id)
    card = _order_card(st, measures, order_num, area_id)
    if existing is None:
        s = reg.open_order(order_num, area_id, operator_id)
        return jsonify(ok=True, action="opened", order=card,
                       session_id=s.session_id,
                       message=f"Заказ {order_num} взят в работу")
    return jsonify(ok=True, action="already_open", order=card,
                   session_id=existing["session_id"],
                   message=f"Заказ {order_num} уже в работе — закончить?")


@psr.route("/api/close", methods=["POST"])
def api_close():
    st = _storage()
    data = request.get_json(silent=True) or {}
    reg, _, _ = _services(st)
    try:
        s = reg.close_order(int(data["order"]), data["area"],
                            operator_id=data.get("operator"))
    except (OrderAlreadyClosed, OrderNotOpen) as exc:
        return jsonify(ok=False, error=str(exc)), 200
    return jsonify(ok=True,
                   duration_minutes=s.duration_minutes,
                   measure_name=s.measure_name,
                   measure_value=s.measure_value,
                   message=f"Заказ {s.order_num} закончен")


# ----------------------------------------------------------------------
# Экран начальника цеха
# ----------------------------------------------------------------------
@psr.route("/chief")
def chief_screen():
    """
    Позаказная картина по шести участкам.

    Показывает ровно то, чего сегодня нет ни в каком виде: что где
    в работе, сколько времени уже идёт, что закончено за смену.
    Нормативов и подсветки здесь НЕТ — они появляются на шаге 2,
    когда накопится медиана за 30 смен.
    """
    st = _storage()
    reg, _, measures = _services(st)

    areas = []
    for area_id, name in AREA_NAMES.items():
        sessions = reg.open_sessions(area_id)
        try:
            measure = measures.get_measure(area_id).measure_name
        except MeasureNotSet:
            measure = None
        areas.append({
            "area_id": area_id,
            "name": name,
            "measure": measure,
            "in_work": [
                {**s.__dict__,
                 **_order_card(st, measures, s.order_num, area_id)}
                for s in sessions
            ],
        })
    return render_template("psr_chief.html", areas=areas)


@psr.route("/api/order/<int:order_num>")
def api_order(order_num: int):
    """Состав заказа и его путь по участкам — для разбора начальником цеха."""
    st = _storage()
    reg, _, measures = _services(st)
    history = [
        {"area": AREA_NAMES.get(s.area_id, s.area_id),
         "opened_at": s.opened_at,
         "closed_at": s.closed_at,
         "duration_minutes": s.duration_minutes,
         "measure_name": s.measure_name,
         "measure_value": s.measure_value,
         "retro": s.retro_open or s.retro_close}
        for s in reg.order_history(order_num)
    ]
    return jsonify(order=_order_card(st, measures, order_num, ""),
                   history=history)
