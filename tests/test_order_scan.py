"""
Тесты фиксации по коду с бланка заказа (шаг 1 ПСР).

Код печатает 1С на форме «Заказ клиента» (SETUP-002 §7а):
«ПС00-010109|2026-08-28» — номер документа и дата.

SR-108: заказ ищется в пределах сменного задания, а не по всему потоку.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.order_scan import (
    OrderNotInShiftTask,
    OrderScanResolver,
    ScanNotRecognized,
    parse_order_code,
)
from src.storage import Storage


@pytest.fixture()
def st():
    s = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    s.upsert_order_link(8952, "confirmed", order_full_num="ПС00-010109",
                        order_date="2026-08-28")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Разбор кода
# ----------------------------------------------------------------------
def test_разбирает_номер_и_дату():
    o = parse_order_code("ПС00-010109|2026-08-28")
    assert o.doc_num == "ПС00-010109"
    assert o.doc_date == "2026-08-28"
    assert o.key == "ПС00-010109|2026-08-28"


def test_разделитель_не_обязан_быть_вертикальной_чертой():
    """Разделитель оставлен на усмотрение исполнителя 1С (SETUP-002 §7а)."""
    for raw in ("ПС00-010109;2026-08-28", "ПС00-010109,2026-08-28",
                "ПС00-010109\t2026-08-28"):
        assert parse_order_code(raw).doc_num == "ПС00-010109"


def test_принимает_разные_префиксы_документов():
    """В выгрузке встречаются ПС00, ЛД00, 0Ч00 — все допустимы."""
    for num in ("ПС00-010109", "ЛД00-012594", "0Ч00-003266"):
        assert parse_order_code(f"{num}|2026-08-28").doc_num == num


def test_отказ_объясняет_что_не_так():
    """
    Оператор у станка должен понять из экрана, что он отсканировал
    не то, а не увидеть слово «ошибка».
    """
    with pytest.raises(ScanNotRecognized) as e:
        parse_order_code("8952")
    # Отказ обязан назвать ОБА годных вида кода: оператор у станка
    # не знает, какой у него бланк, и «ожидался GUID» завело бы его
    # в тупик на бланке старого образца.
    assert "GUID" in str(e.value) and "номер документа и дата" in str(e.value)

    with pytest.raises(ScanNotRecognized) as e:
        parse_order_code("ПС00-010109|28.08.2026")
    assert "ГГГГ-ММ-ДД" in str(e.value)

    with pytest.raises(ScanNotRecognized) as e:
        parse_order_code("")
    assert "пустой" in str(e.value)


# ----------------------------------------------------------------------
# Связка с заказом БАЗИС
# ----------------------------------------------------------------------
def test_находит_заказ_базис_по_бланку(st):
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie") == 8952


def test_несвязанный_документ_назван_прямо(st):
    r = OrderScanResolver(st)
    with pytest.raises(ScanNotRecognized) as e:
        r.resolve("ПС00-999999|2026-08-28", "kromlenie")
    assert "не связан ни с одним заказом" in str(e.value)


def test_дата_различает_одинаковые_номера_разных_лет(st):
    """
    Номера документов 1С повторяются между годами. Один номер без даты
    адресует несколько заказов — поэтому ключ парный.
    """
    st.upsert_order_link(7001, "confirmed", order_full_num="ПС00-010109",
                         order_date="2025-08-28")
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie") == 8952
    assert r.resolve("ПС00-010109|2025-08-28", "kromlenie") == 7001


# ----------------------------------------------------------------------
# SR-108. Поиск в пределах сменного задания
# ----------------------------------------------------------------------
def test_sr108_заказ_вне_сменного_задания_отклоняется(st):
    r = OrderScanResolver(st)
    with pytest.raises(OrderNotInShiftTask) as e:
        r.resolve("ПС00-010109|2026-08-28", "kromlenie", shift_task=[8953, 8954])
    assert "сменном задании" in str(e.value)


def test_sr108_заказ_из_сменного_задания_принимается(st):
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie",
                     shift_task=[8952, 8953]) == 8952


def test_без_сменного_задания_поиск_идёт_по_всей_связке(st):
    """
    Пока выгрузки сменного задания нет (пункт 0.8 шага 0), область
    поиска не ограничена — и это видно в вызове, а не спрятано.
    """
    r = OrderScanResolver(st)
    assert r.resolve("ПС00-010109|2026-08-28", "kromlenie",
                     shift_task=None) == 8952


# ----------------------------------------------------------------------
# GUID в QR на бланке (D-229). Исправление допущения CF-449
# ----------------------------------------------------------------------
GUID = "a3f1c0de-4b2e-4d11-9f77-0c2b5a8e1234"


def test_guid_разбирается_в_любом_обрамлении():
    """
    1С пишет ссылку по-разному: в фигурных скобках, заглавными, без
    дефисов, с префиксом. Настоящего бланка проект не видел (CF-449),
    и отвергнуть верный код из-за фигурной скобки было бы ровно той же
    ошибкой, что уже случилась: отказ по форме при верном существе.
    """
    from src.order_scan import parse_order_code
    for вид in (GUID,
                GUID.upper(),
                "{" + GUID.upper() + "}",
                GUID.replace("-", ""),
                "1c:" + GUID,
                "  " + GUID + "  "):
        s = parse_order_code(вид)
        assert s.by_guid, вид
        assert s.doc_guid == GUID, вид        # приведён к одному виду


def test_похожее_на_guid_но_не_guid_отвергается():
    """Снисходительность к обрамлению не означает снисходительности к существу."""
    from src.order_scan import ScanNotRecognized, parse_order_code
    for мусор in ("a3f1c0de-4b2e-4d11-9f77-0c2b5a8e123",      # знаком меньше
                  "a3f1c0de-4b2e-4d11-9f77-0c2b5a8e1234a",    # знаком больше
                  "z3f1c0de-4b2e-4d11-9f77-0c2b5a8e1234"):    # не шестнадцатеричный
        with pytest.raises(ScanNotRecognized):
            parse_order_code(мусор)


def test_ключ_отдачи_статуса_по_guid_не_строится():
    """
    Скан опознаёт заказ, ключ адресует строку маршрута в 1С — это разные
    вещи. Пара «номер + дата» берётся из связки заказов (D-227), и
    выдумать её из GUID нельзя: статус ушёл бы не в ту строку, тихо.
    """
    from src.order_scan import parse_order_code
    s = parse_order_code(GUID)
    with pytest.raises(ValueError) as e:
        _ = s.key
    assert "D-227" in str(e.value)


def test_старая_форма_номер_и_дата_не_отменена():
    """Стенды, отладка и бланки без кода — D-229 её не отменяет."""
    from src.order_scan import parse_order_code
    s = parse_order_code("ПС00-010109|2026-08-28")
    assert not s.by_guid
    assert s.key == "ПС00-010109|2026-08-28"


def _связка(tmp_path, **kw):
    from src.storage import Storage
    st = Storage(str(tmp_path / "t.db"))
    st.upsert_order_link(7709, "confirmed", order_full_num="ПС00-007709",
                         order_date="2026-09-21", **kw)
    return st


def test_скан_по_guid_находит_заказ(tmp_path):
    from src.order_scan import OrderScanResolver
    st = _связка(tmp_path, doc_guid=GUID)
    assert OrderScanResolver(st).resolve(GUID, "raskroy") == 7709
    st.close()


def test_guid_в_базе_в_чужом_виде_всё_равно_находится(tmp_path):
    """
    Запись связки приводит GUID к одному виду, но `order_links` — таблица
    ОБЩАЯ (КОНТРАКТ интеграции §5), и писать в неё может соседнее
    направление или миграция, не знающая о приведении. Поэтому
    снисходительность нужна и на чтении.

    Запись идёт НАПРЯМУЮ в таблицу намеренно: через `upsert_order_link`
    опыт был бы ложным — тот приводит значение сам, и проверка чтения
    ничего бы не проверяла. Первая редакция теста именно так и была
    написана: в ней стоял `doc_guid=GUID.upper()` с комментарием «в базе
    заглавными», а в базу при этом ложились строчные. Контрольный опыт
    это и вскрыл.
    """
    from src.order_scan import OrderScanResolver
    st = _связка(tmp_path)
    st._conn.execute("UPDATE order_links SET doc_guid=? WHERE order_num=7709",
                     ("{" + GUID.upper() + "}",))
    st._conn.commit()
    assert OrderScanResolver(st).resolve(GUID, "raskroy") == 7709
    st.close()


def test_guid_известен_а_в_базе_его_нет_отказ_называет_обе_причины(tmp_path):
    """
    Причин ДВЕ, и они требуют разных действий: либо бланк чужой — дело
    оператора; либо GUID не пришёл в выгрузке 1С — дело настройки, и
    оператор тут бессилен. «Заказ не найден» свалило бы их в одно, и
    месяц искали бы не там.
    """
    from src.order_scan import ScanNotRecognized, OrderScanResolver
    st = _связка(tmp_path)                            # связка БЕЗ GUID
    with pytest.raises(ScanNotRecognized) as e:
        OrderScanResolver(st).resolve(GUID, "raskroy")
    текст = str(e.value)
    assert "бланк не от этого цеха" in текст
    assert "без колонки с кодом документа" in текст
    st.close()


def test_повторное_разрешение_связки_не_стирает_guid(tmp_path):
    """
    Выгрузка 1С может прийти без колонки GUID, пока программист её не
    добавил. Если бы повторная запись связки затирала GUID пустым, скан
    ломался бы на заказе, который вчера работал, — и причину искали бы
    в сканере.
    """
    from src.order_scan import OrderScanResolver
    st = _связка(tmp_path, doc_guid=GUID)
    st.upsert_order_link(7709, "confirmed", order_full_num="ПС00-007709",
                         order_date="2026-09-21")     # выгрузка без GUID
    assert OrderScanResolver(st).resolve(GUID, "raskroy") == 7709
    st.close()
