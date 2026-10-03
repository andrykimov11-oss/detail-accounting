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

ПРАВА И ВХОД — ЕСТЬ (SR-96 — SR-100)
    Оператор больше не поле на экране: он входит сканом личного бейджа
    (SR-97), и каждое действие проверяется по справочнику прав (SR-96).
    Проверка идёт через `access.require`, который тем же вызовом
    записывает автора привилегированного действия (SR-99).

    В выгрузки и отчёты уходит КОД исполнителя, не ФИО (SR-100). Имя
    видно только на экране цеха: «ИСП-0147» у станка никого не узнаёт.

ЧЕГО ЗДЕСЬ НЕТ
    Бригадной отметки (SR-98) — она относится к событию операции и
    живёт в подетальном учёте, а не в позаказном рабочем месте.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent))

from access import (  # noqa: E402
    ACT_ONEC_REPORT,
    ACT_ORDER_CLOSE,
    ACT_ORDER_OPEN,
    Access,
    AccessDenied,
    BadgeNotRecognized,
    PersonInactive,
)
from area_measures import AreaMeasures, MeasureNotSet  # noqa: E402
from one_c_sync import KIND_NAMES, OneCSync  # noqa: E402
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
from raskroy_nav import (  # noqa: E402
    BazisRootNotSet,
    LinkNotConfirmed,
    OrderFolderNotFound,
    RaskroyNavigator,
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


def _access(storage) -> Access:
    return Access(storage)


RASKROY = "raskroy"


def _shift_task_scope(storage):
    """
    Область поиска заказа — из действующего задания, а не от клиента.

    SR-108 требует искать В ПРЕДЕЛАХ сменного задания. Первая редакция
    брала список заказов из тела запроса: экран мог прислать пустую
    область, и заказ находился бы по всему потоку из 12 054. Требование
    выполнялось бы ровно до первого невежливого клиента.

    Пока выгрузки задания нет (пункт 0.8 шага 0), задания в базе нет
    тоже, и область не ограничивается — но это видно по ответу поля
    `task_id`, а не спрятано.
    """
    task = storage.active_shift_task()
    if task is None:
        return None, ""
    return storage.shift_task_orders(task["task_id"]), task["task_id"]


def _nav_payload(storage, order_num: int, task_id: str) -> dict:
    """Навигация раскроя: декоры, листы, нужно ли подтверждение."""
    try:
        return RaskroyNavigator(storage).navigate(order_num,
                                                  task_id).to_report()
    except (OrderFolderNotFound, BazisRootNotSet) as exc:
        return {"order_num": order_num, "error": str(exc)}


def _current(storage, data=None):
    """
    Кто сейчас работает. Токен берётся из заголовка либо из тела запроса.

    Возвращает Session либо None. Решение о доступе принимает не эта
    функция, а `access.require`: здесь только опознание, там — право.
    """
    token = request.headers.get("X-PSR-Token") or \
        ((data or {}).get("token") if isinstance(data, dict) else None) or \
        request.args.get("token", "")
    return _access(storage).session(token) if token else None


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

    # Кто отмечает — определяется входом по бейджу, а не полем формы.
    # Прежняя редакция брала `operator` из тела запроса: кто угодно мог
    # назваться кем угодно, и отметка не значила ничего (SR-96, SR-97).
    try:
        person = _access(st).require(_current(st, data), ACT_ORDER_OPEN)
    except AccessDenied as exc:
        return jsonify(ok=False, error=str(exc)), 200
    operator_id = person.person_code

    reg, resolver, measures = _services(st)
    scope, task_id = _shift_task_scope(st)
    try:
        order_num = resolver.resolve(raw, area_id, shift_task=scope)
    except (ScanNotRecognized, OrderNotInShiftTask) as exc:
        return jsonify(ok=False, error=str(exc)), 200

    existing = st.get_open_order_session(order_num, area_id)
    card = _order_card(st, measures, order_num, area_id)

    # На раскрое тот же скан даёт сверх отметки навигацию: декоры,
    # число листов и вопрос о подтверждении (CHG-001). На прочих
    # участках навигации нет — там программ станка не существует.
    nav = _nav_payload(st, order_num, task_id) if area_id == RASKROY else None

    if existing is None:
        s = reg.open_order(order_num, area_id, operator_id)
        return jsonify(ok=True, action="opened", order=card, nav=nav,
                       task_id=task_id, session_id=s.session_id,
                       message=f"Заказ {order_num} взят в работу")
    return jsonify(ok=True, action="already_open", order=card, nav=nav,
                   task_id=task_id, session_id=existing["session_id"],
                   message=f"Заказ {order_num} уже в работе — закончить?")


@psr.route("/api/close", methods=["POST"])
def api_close():
    st = _storage()
    data = request.get_json(silent=True) or {}
    try:
        person = _access(st).require(_current(st, data), ACT_ORDER_CLOSE)
    except AccessDenied as exc:
        return jsonify(ok=False, error=str(exc)), 200
    reg, _, _ = _services(st)
    try:
        s = reg.close_order(int(data["order"]), data["area"],
                            operator_id=person.person_code)
    except (OrderAlreadyClosed, OrderNotOpen) as exc:
        return jsonify(ok=False, error=str(exc)), 200
    return jsonify(ok=True,
                   duration_minutes=s.duration_minutes,
                   measure_name=s.measure_name,
                   measure_value=s.measure_value,
                   message=f"Заказ {s.order_num} закончен")


@psr.route("/api/login", methods=["POST"])
def api_login():
    """
    Вход по бейджу (SR-97). Пароль не запрашивается — его не существует.

    Экран отвечает кодом и именем: имя нужно человеку, чтобы убедиться,
    что вошёл он, а не сосед по смене. Наружу из системы уходит только
    код (SR-100).
    """
    st = _storage()
    data = request.get_json(silent=True) or {}
    try:
        s = _access(st).login_by_badge(data.get("badge", ""),
                                       data.get("area", ""))
    except (BadgeNotRecognized, PersonInactive) as exc:
        return jsonify(ok=False, error=str(exc)), 200
    return jsonify(ok=True, token=s.token,
                   person_code=s.person.person_code,
                   person_name=s.person.full_name,
                   role=s.person.role, role_name=s.person.role_name)


@psr.route("/api/confirm", methods=["POST"])
def api_confirm():
    """
    Подтверждение соответствия декоров и числа листов (SR-111, SR-113).

    Подтверждает ОПЕРАТОР, а не экран: сверяет то, что на бумаге, с тем,
    что физически привезли. Поэтому право проверяется, а запись несёт
    его код и момент.
    """
    st = _storage()
    data = request.get_json(silent=True) or {}
    try:
        person = _access(st).require(_current(st, data), ACT_ORDER_OPEN)
    except AccessDenied as exc:
        return jsonify(ok=False, error=str(exc)), 200
    nav = RaskroyNavigator(st)
    order_num = int(data["order"])
    if data.get("revoke"):
        nav.revoke(order_num, person.person_code, data.get("note", ""))
        return jsonify(ok=True, confirmed=False,
                       message=f"Подтверждение по заказу {order_num} отозвано")
    nav.confirm(order_num, person.person_code, data.get("note", ""))
    return jsonify(ok=True, confirmed=True,
                   message=f"Заказ {order_num} подтверждён")


@psr.route("/api/prepare", methods=["POST"])
def api_prepare():
    """
    Подготовить рабочую папку станка (SR-114).

    Копирование, а не запуск: на рабочем месте оператора ничего не
    устанавливается (решение A-09а). Оператор открывает программу сам —
    просто она уже лежит там, где надо.
    """
    st = _storage()
    data = request.get_json(silent=True) or {}
    try:
        _access(st).require(_current(st, data), ACT_ORDER_OPEN)
    except AccessDenied as exc:
        return jsonify(ok=False, error=str(exc)), 200
    _, task_id = _shift_task_scope(st)
    try:
        copied = RaskroyNavigator(st).prepare_machine_folder(
            int(data["order"]), decor_name=data.get("decor", ""),
            task_id=task_id)
    except (LinkNotConfirmed, OrderFolderNotFound, BazisRootNotSet) as exc:
        return jsonify(ok=False, error=str(exc)), 200
    # Наружу — только счёт: в путях фамилия клиента (SR-100).
    return jsonify(ok=True, programs=len(copied),
                   message=f"Программы готовы: {len(copied)} файлов")


@psr.route("/api/logout", methods=["POST"])
def api_logout():
    st = _storage()
    data = request.get_json(silent=True) or {}
    _access(st).logout(data.get("token", ""))
    return jsonify(ok=True)


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
            # D-236: на двух участках измеритель сегодня занижает
            # выработку. Экран обязан сказать это человеку: число,
            # про которое известно, что оно неполно, без пометки будет
            # сравнено с планом как точное.
            занижен = measures.is_lower_bound(area_id)
            почему = measures.lower_bound_reason(area_id)
        except MeasureNotSet:
            measure, занижен, почему = None, False, ""
        areas.append({
            "area_id": area_id,
            "name": name,
            "measure": measure,
            "measure_lower_bound": занижен,
            "measure_note": почему,
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


@psr.route("/api/onec/queue")
def api_onec_queue():
    """
    SR-48, BR-81: состояние очереди обмена с 1С.

    Отчёт нужен не «для полноты». Пока цех отмечает и здесь, и в 1С,
    проект добавил работы и не убрал ни одной; выключить старую
    регистрацию разрешено при расхождении меньше 2 % (SR-49, D-163).
    Значит кто-то обязан видеть, доходят ли сообщения вообще, — и
    требование называет этого человека поимённо: руководитель
    производства, а не «ответственный».
    """
    st = _storage()
    try:
        _access(st).require(_current(st), ACT_ONEC_REPORT)
    except AccessDenied as exc:
        return jsonify(ok=False, error=str(exc)), 200
    отчёт = OneCSync(st).stuck_queue()
    return jsonify(ok=True, stuck=отчёт.stuck, alarm=отчёт.is_alarm,
                   oldest_at=отчёт.oldest_at,
                   oldest_age_hours=round(отчёт.oldest_age_hours, 1),
                   by_kind={KIND_NAMES.get(k, k): v
                            for k, v in отчёт.by_kind.items()},
                   addressee=отчёт.addressee,
                   text=отчёт.as_text())


@psr.route("/api/onec/reconcile")
def api_onec_reconcile():
    """
    SR-49: расхождение за месяц и вывод о старой регистрации.

    Число 1С приходит ПАРАМЕТРОМ `theirs`, а не добывается: формат
    выгрузки операционной истории выясняется вопросами OQ-114 и OQ-115.
    Пока ответа нет, руководитель производства вводит число из 1С
    руками — и это честнее, чем разбирать формат, которого ещё нет.
    """
    st = _storage()
    try:
        _access(st).require(_current(st), ACT_ONEC_REPORT)
    except AccessDenied as exc:
        return jsonify(ok=False, error=str(exc)), 200
    месяц = request.args.get("month") or datetime.now().strftime("%Y-%m")
    if request.args.get("theirs") is None:
        return jsonify(ok=False, error=(
            "не задано число закрытых операций по данным 1С за месяц "
            "(параметр theirs). Без него расхождение не считается: "
            "сравнивать не с чем"), ours=OneCSync(st).closed_operations(месяц),
            month=месяц), 200
    r = OneCSync(st).reconcile(месяц, theirs=int(request.args["theirs"]))
    return jsonify(ok=True, month=r.month, ours=r.ours, theirs=r.theirs,
                   diff=r.diff,
                   rate=None if r.rate == float("inf") else round(r.rate, 4),
                   threshold=r.threshold, may_switch_off=r.may_switch_off,
                   text=r.as_text())
