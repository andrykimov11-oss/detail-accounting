"""
Тесты резолвера связки «заказ Базис ↔ документ 1С» (src/order_resolver.py).

Это защита от корневого риска проекта: числовой номер из .xbir может
соответствовать нескольким документам 1С, и ошибка здесь означает запись
статуса операции в чужой заказ.

Основной блок тестов построен на **реальных 13 неоднозначных заказах**,
найденных в массиве (см. docs/dataset-validation-report.md, H2). Имена
клиентов взяты из выгрузки 1С как есть — это делает тесты регрессией
на фактических данных цеха, а не на выдуманных примерах.
"""
from __future__ import annotations

import pytest
from datetime import date, timedelta

from one_c_loader import OneCOrder, parse_order_number
from order_resolver import (
    MAX_ORDER_AGE_DAYS,
    export_reference_date,
    names_match,
    LinkStatus,
    extract_client,
    name_similarity,
    normalize_name,
    resolve_batch,
    resolve_order_link,
    format_resolve_report,
)


def order(full: str, client: str, status: str = "К выполнению") -> OneCOrder:
    prefix, num = parse_order_number(full)
    return OneCOrder(order_full_num=full, order_prefix=prefix, order_num=num,
                     client_name=client, order_status=status)


# --- Разбор номера документа 1С ---------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("ЛД00-006564", ("ЛД00", 6564)),
    ("ПС00-007730", ("ПС00", 7730)),
    ("0Ч00-000936", ("0Ч00", 936)),
    ("ЛД00-000015", ("ЛД00", 15)),      # ведущие нули срезаются
    ("6564", ("", 6564)),                # номер без префикса
    ("", None),
    ("мусор", None),
])
def test_parse_order_number(raw, expected):
    assert parse_order_number(raw) == expected


def test_leading_zeros_match_basis_form():
    """
    В Базис номер попадает без ведущих нулей. Разбор должен давать то же
    число, иначе связка не найдётся.
    """
    assert parse_order_number("ЛД00-006564")[1] == 6564


# --- Извлечение клиента из .xbir --------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("7936 Коновалов", "Коновалов"),
    ("6564 Спецторг ООО", "Спецторг ООО"),
    ("2050 Мустакимов", "Мустакимов"),
    ("7846", ""),                        # имени нет — уйдёт технологу
    ("", ""),
])
def test_extract_client(raw, expected):
    assert extract_client(raw) == expected


# --- Нормализация наименований ----------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Сарапулова Светлана Викторовна ИП", "сарапулова светлана викторовна"),
    ("ЛАДЬЯ ООО", "ладья"),
    ("Спецторг ООО", "спецторг"),
    ('БОНУС ООО', "бонус"),
    ("Булатов М.Г.", "булатов м г"),
])
def test_normalize_name_strips_legal_forms(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_handles_yo():
    """Ё и Е пишут вперемешку — для сопоставления они должны совпадать."""
    assert normalize_name("Алёшин") == normalize_name("Алешин")


# --- Совпадение имён: строгое, пословное ------------------------------------

def test_surname_matches_full_name():
    """В Базис пишут фамилию, в 1С хранится ФИО целиком."""
    assert names_match("Коновалов", "Коновалов Дмитрий Геннадьевич")


def test_different_surnames_do_not_match():
    assert not names_match("Коновалов", "Ахсанов Динур Нафисович ИП")


def test_company_short_name_matches():
    assert names_match("Спецторг", "Спецторг ООО")


def test_empty_name_never_matches():
    assert not names_match("", "Коновалов Дмитрий Геннадьевич")


@pytest.mark.parametrize("xbir,one_c", [
    ("Петров", "Кузнецов Пётр Петрович"),      # «петров» ⊂ «петрович» как подстрока
    ("Иванов", "Иванова Мария Сергеевна"),      # мужская/женская фамилия
    ("Сидор", "Сидоров Иван Иванович"),         # усечённая фамилия
    ("Ков", "Коновалов Дмитрий Геннадьевич"),   # случайный фрагмент
])
def test_substring_does_not_count_as_match(xbir, one_c):
    """
    Защита от ложных связок: сравнение идёт по целым словам.

    Подстрочное сравнение засчитывало «Петров» ↔ «Кузнецов Пётр Петрович»
    как полное совпадение — заказ ушёл бы к чужому клиенту автоматически,
    ровно тот тихий сбой, который проект должен исключить.
    """
    assert not names_match(xbir, one_c)


def test_hyphen_in_company_name():
    """
    Реальный случай 6869: в Базисе «С групп», в 1С «Компания С-Групп, ООО».
    Дефис должен разбивать слова, иначе заказ уходит в ручной разбор зря.
    """
    assert names_match("С групп", "Компания С-Групп, ООО")


def test_initials_alone_are_not_enough():
    """
    Совпадение только по инициалам — не совпадение. Иначе «М.Г.» поймает
    любого клиента с такими же буквами в отчестве.
    """
    assert not names_match("М Г", "Рычков Михаил Георгиевич")


def test_significant_word_required():
    assert names_match("Булатов", "Булатов М.Г.")
    assert not names_match("М", "Булатов М.Г.")


def test_similarity_is_advisory_only():
    """Оценка схожести идёт в аудит, но решение принимает names_match."""
    assert name_similarity("Петров", "Кузнецов Пётр Петрович") < 1.0
    assert name_similarity("Коновалов", "Коновалов Дмитрий Геннадьевич") == 1.0


# --- Однозначные случаи ------------------------------------------------------

def test_single_candidate_resolves_without_client():
    """Один кандидат в 1С — имя клиента не требуется."""
    r = resolve_order_link(6564, "6564", [order("ЛД00-006564", "Спецторг ООО")])
    assert r.status is LinkStatus.UNIQUE
    assert r.order_full_num == "ЛД00-006564"
    assert r.is_resolved


def test_order_absent_in_1c():
    r = resolve_order_link(9999, "9999 Иванов", [])
    assert r.status is LinkStatus.NOT_FOUND
    assert r.needs_operator
    assert not r.is_resolved


# --- Реальные неоднозначные заказы из массива --------------------------------
# Данные взяты из выгрузки 1С «производственные операции New_1».

AMBIGUOUS_CASES = [
    # (число, строка .xbir, [(полный номер, клиент)], ожидаемый номер)
    (1991, "1991 Беляев",
     [("0Ч00-001991", "Беляев Александр Владимирович"),
      ("ПС00-001991", "Килин Алексей Васильевич")], "0Ч00-001991"),
    (2050, "2050 Мустакимов",
     [("0Ч00-002050", "Мустакимов Вильдан Рашитович"),
      ("ЛД00-002050", "БОНУС ООО")], "0Ч00-002050"),
    (2065, "2065 Булатов",
     [("0Ч00-002065", "Булатов М.Г."),
      ("ПС00-002065", "Рычков Александр Викторович")], "0Ч00-002065"),
    (4931, "4931 Сарапулова",
     [("ЛД00-004931", "Сарапулова Светлана Викторовна ИП"),
      ("ПС00-004931", "Пономарев Сергей Викторович")], "ЛД00-004931"),
    (6597, "6597 Сарапулова",
     [("ЛД00-006597", "Сарапулова Светлана Викторовна ИП"),
      ("ПС00-006597", "Старков Виталий Анатольевич")], "ЛД00-006597"),
    (7750, "7750 Поливанов",
     [("ЛД00-007750", "АМИС ООО"),
      ("ПС00-007750", "Поливанов Александр Анатольевич")], "ПС00-007750"),
    (7792, "7792 Ведянин",
     [("ЛД00-007792", "Рублевский Андрей Владимирович ИП"),
      ("ПС00-007792", "Ведянин Алексей Федорович")], "ПС00-007792"),
    (7843, "7843 Коровина",
     [("ЛД00-007843", "ЛАДЬЯ ООО"),
      ("ПС00-007843", "Коровина Лариса Ивановна ИП")], "ПС00-007843"),
    (7908, "7908 Чашин",
     [("ЛД00-007908", "МД ООО"),
      ("ПС00-007908", "Чашин Сергей Викторович")], "ПС00-007908"),
    (7936, "7936 Коновалов",
     [("ЛД00-007936", "Ахсанов Динур Нафисович ИП"),
      ("ПС00-007936", "Коновалов Дмитрий Геннадьевич")], "ПС00-007936"),
    (7941, "7941 Берлизова",
     [("ЛД00-007941", "ЛАДЬЯ ООО"),
      ("ПС00-007941", "Берлизова Анна Геннадьевна ИП")], "ПС00-007941"),
    (7947, "7947 Чашин",
     [("ЛД00-007947", "ЛАДЬЯ ООО"),
      ("ПС00-007947", "Чашин Сергей Викторович")], "ПС00-007947"),
]


@pytest.mark.parametrize("num,raw,cands,expected", AMBIGUOUS_CASES,
                         ids=[str(c[0]) for c in AMBIGUOUS_CASES])
def test_real_ambiguous_orders_resolved_by_client(num, raw, cands, expected):
    """Каждый реальный спорный заказ разрешается именем клиента."""
    r = resolve_order_link(num, raw, [order(f, c) for f, c in cands])
    assert r.status is LinkStatus.RESOLVED_BY_CLIENT
    assert r.order_full_num == expected
    assert r.is_resolved


def test_order_7846_goes_to_technologist():
    """
    Реальный случай 7846: в .xbir нет имени клиента, только число.
    Резолвер обязан отказаться и отдать заказ технологу — а не выбрать
    «более вероятного» кандидата.
    """
    r = resolve_order_link(7846, "7846", [
        order("ЛД00-007846", "ЛАДЬЯ ООО"),
        order("ПС00-007846", "Торсунов Владимир Сергеевич"),
    ])
    assert r.status is LinkStatus.MANUAL_REQUIRED
    assert r.needs_operator
    assert r.order_full_num == ""
    assert "нет имени клиента" in r.reason


# --- Отказ вместо угадывания -------------------------------------------------

def test_no_match_goes_manual_not_guessed():
    """Клиент не совпал ни с кем — технологу, а не ближайшему кандидату."""
    r = resolve_order_link(5000, "5000 Петров", [
        order("ЛД00-005000", "Сидоров Иван Иванович"),
        order("ПС00-005000", "Кузнецов Пётр Петрович"),
    ])
    assert r.status is LinkStatus.MANUAL_REQUIRED
    assert r.order_full_num == ""


def test_two_matching_clients_go_manual():
    """
    Один клиент, два заказа с одинаковым числом и разными префиксами —
    выбрать невозможно, решает технолог.
    """
    r = resolve_order_link(6000, "6000 Чашин", [
        order("ЛД00-006000", "Чашин Сергей Викторович"),
        order("ПС00-006000", "Чашин Сергей Викторович"),
    ])
    assert r.status is LinkStatus.MANUAL_REQUIRED
    assert "совпал сразу с несколькими" in r.reason


def test_scores_recorded_for_audit():
    """Схожести сохраняются — технолог видит, почему система засомневалась."""
    r = resolve_order_link(7936, "7936 Коновалов", [
        order("ЛД00-007936", "Ахсанов Динур Нафисович ИП"),
        order("ПС00-007936", "Коновалов Дмитрий Геннадьевич"),
    ])
    assert r.scores["ПС00-007936"] == 1.0
    assert r.scores["ЛД00-007936"] < 0.82


# --- Окно срока исполнения (45 дней) ----------------------------------------

def dated(full: str, client: str, order_date: date) -> OneCOrder:
    prefix, num = parse_order_number(full)
    return OneCOrder(order_full_num=full, order_prefix=prefix, order_num=num,
                     client_name=client, order_status="К выполнению",
                     order_date=order_date)


REF = date(2026, 6, 22)          # опорная дата выгрузки
CUTOFF = REF - timedelta(days=MAX_ORDER_AGE_DAYS)


def test_window_resolves_when_client_missing():
    """
    Реальный случай 7846: имени клиента в .xbir нет, но по сроку исполнения
    подходит только один заказ — старый физически не может резаться сейчас.
    Независимо подтверждено номенклатурой 1С: материал «Орех Кария 16 мм»
    есть только у ПС00-007846.
    """
    r = resolve_order_link(7846, "7846", [
        dated("ЛД00-007846", "ЛАДЬЯ ООО", date(2025, 7, 4)),
        dated("ПС00-007846", "Торсунов Владимир Сергеевич", date(2026, 6, 16)),
    ], cutoff_date=CUTOFF)

    assert r.status is LinkStatus.RESOLVED_BY_WINDOW
    assert r.order_full_num == "ПС00-007846"
    assert r.is_resolved


def test_window_and_client_agree():
    """Оба признака указывают на один заказ — связка подтверждена дважды."""
    r = resolve_order_link(7097, "7097 Никитин", [
        dated("ПС00-007097", "Катаев Алексей Юрьевич", date(2025, 3, 29)),
        dated("ПС00-007097", "Никитин Олег Анатольевич", date(2026, 5, 20)),
    ], cutoff_date=CUTOFF)

    assert r.status is LinkStatus.RESOLVED_BY_CLIENT
    assert r.client_name == "Никитин Олег Анатольевич"


def test_window_and_client_disagree_goes_manual():
    """
    Срок указывает на один заказ, а клиент — на другой. Расхождение
    признаков означает, что данные неверны: решает человек, а не система.
    """
    r = resolve_order_link(5555, "5555 Старый", [
        dated("ЛД00-005555", "Старый Иван Петрович", date(2025, 1, 10)),
        dated("ПС00-005555", "Новиков Пётр Сергеевич", date(2026, 6, 10)),
    ], cutoff_date=CUTOFF)

    assert r.status is LinkStatus.MANUAL_REQUIRED
    assert r.order_full_num == ""
    assert "не совпал" in r.reason


def test_all_candidates_stale_falls_back_to_client():
    """
    Выгрузка 1С устарела — все кандидаты вне окна. Заказ не теряется:
    разбираем по клиенту среди всех кандидатов.
    """
    r = resolve_order_link(7335, "7335 Фадеев", [
        dated("ПС00-007335", "Чечегов Игорь Анатольевич", date(2025, 4, 1)),
        dated("ПС00-007335", "Фадеев Александр Анатольевич", date(2025, 4, 20)),
    ], cutoff_date=CUTOFF)

    assert r.status is LinkStatus.RESOLVED_BY_CLIENT
    assert r.client_name == "Фадеев Александр Анатольевич"
    assert "старше срока исполнения" in r.reason


def test_window_keeps_several_candidates_client_decides():
    """Два заказа попали в окно — различает клиент."""
    r = resolve_order_link(6000, "6000 Чашин", [
        dated("ЛД00-006000", "Иванов Иван Иванович", date(2026, 6, 1)),
        dated("ПС00-006000", "Чашин Сергей Викторович", date(2026, 6, 10)),
    ], cutoff_date=CUTOFF)

    assert r.status is LinkStatus.RESOLVED_BY_CLIENT
    assert r.order_full_num == "ПС00-006000"


def test_no_window_falls_back_to_client_only():
    """Без окна (window_days=None) работает прежняя логика по клиенту."""
    results = resolve_batch(
        {7936: "7936 Коновалов"},
        {7936: [dated("ЛД00-007936", "Ахсанов Динур Нафисович ИП", date(2026, 6, 1)),
                dated("ПС00-007936", "Коновалов Дмитрий Геннадьевич", date(2026, 6, 2))]},
        window_days=None,
    )
    assert results[0].status is LinkStatus.RESOLVED_BY_CLIENT


def test_reference_date_is_export_not_today():
    """
    Опорная дата окна — самая свежая дата в выгрузке, а не сегодня.
    Выгрузка 1С делается не каждый день: отсчёт от сегодняшнего числа
    выбрасывал из окна живые заказы месячной давности.
    """
    one_c = {
        7341: [dated("ПС00-007341", "Доливец Сергей Евгеньевич", date(2026, 5, 29))],
    }
    assert export_reference_date(one_c) == date(2026, 5, 29)

    results = resolve_batch({7341: "7341 Доливец"}, one_c)
    assert results[0].is_resolved


# --- Пакетная обработка и отчёт ---------------------------------------------

def test_resolve_batch_mixed():
    xbir = {6564: "6564 Спецторг ООО", 7936: "7936 Коновалов", 7846: "7846"}
    one_c = {
        6564: [order("ЛД00-006564", "Спецторг ООО")],
        7936: [order("ЛД00-007936", "Ахсанов Динур Нафисович ИП"),
               order("ПС00-007936", "Коновалов Дмитрий Геннадьевич")],
        7846: [order("ЛД00-007846", "ЛАДЬЯ ООО"),
               order("ПС00-007846", "Торсунов Владимир Сергеевич")],
    }
    results = resolve_batch(xbir, one_c)
    by_num = {r.order_num: r for r in results}

    assert by_num[6564].status is LinkStatus.UNIQUE
    assert by_num[7936].status is LinkStatus.RESOLVED_BY_CLIENT
    assert by_num[7846].status is LinkStatus.MANUAL_REQUIRED
    assert sum(1 for r in results if r.is_resolved) == 2


def test_report_lists_manual_queue():
    results = resolve_batch(
        {7846: "7846"},
        {7846: [order("ЛД00-007846", "ЛАДЬЯ ООО"),
                order("ПС00-007846", "Торсунов Владимир Сергеевич")]},
    )
    report = format_resolve_report(results)
    assert "ТРЕБУЕТСЯ РУЧНОЕ СОПОСТАВЛЕНИЕ" in report
    assert "ЛД00-007846" in report and "ПС00-007846" in report
