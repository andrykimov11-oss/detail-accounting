"""
Фиксация по коду с бланка заказа (шаг 1 ПСР).

ЧТО СКАНИРУЕТСЯ
    Код, напечатанный 1С на форме «Заказ клиента» (SETUP-002 §7а):

        ПС00-010109|2026-08-28
        └─ номер документа 1С ─┘ └─ дата документа ─┘

    Пара «номер + дата» выбрана не для красоты: номера документов 1С
    повторяются между годами, и один номер без даты адресует несколько
    заказов. Проверено на массиве: тройка «номер + дата + операция»
    уникальна, коллизий нет на 102 877 строках.

ПОЧЕМУ ЭТО НЕ ТО ЖЕ, ЧТО СКАН БИРКИ
    Бирка несёт код ДЕТАЛИ и появляется только на шаге 4. До него
    единственный машиночитаемый код в цехе — на бумажном бланке заказа,
    и фиксация идёт по нему. Один и тот же оператор на шаге 5 начнёт
    сканировать бирки, но заказ при этом никуда не денется: позаказное
    открытие остаётся началом отсчёта для первой детали (SR-34а).

ЧЕГО ЗДЕСЬ НЕТ
    Поиска по всему потоку. Заказ ищется В ПРЕДЕЛАХ СМЕННОГО ЗАДАНИЯ
    (SR-108): девять-одиннадцать заказов против 12 054. Пока выгрузки
    сменного задания нет (пункт 0.8 шага 0), область поиска задаётся
    вызывающим кодом явным списком — и это видно, а не спрятано.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Разделитель на усмотрение исполнителя 1С (SETUP-002 §7а), поэтому
# принимаем любой из вероятных, а не один жёстко заданный.
SEPARATORS = "|;,\t"

# Номер документа 1С: префикс из букв и цифр, дефис, цифры.
# Примеры из выгрузки: ПС00-010109, ЛД00-012594, 0Ч00-003266.
DOC_NUM_RE = re.compile(r"^[A-Za-zА-Яа-я0-9]{2,6}-\d{4,10}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ScanNotRecognized(Exception):
    """Код не разобран: это не бланк заказа."""

    def __init__(self, raw: str, why: str):
        self.raw = raw
        super().__init__(f"Код «{raw}» не распознан как бланк заказа: {why}")


class OrderNotInShiftTask(Exception):
    """
    Заказ распознан, но его нет в сменном задании участка.

    SR-108: выбор идёт только среди заказов сменного задания. Заказ
    прошлого года в него попасть не может, и отказ здесь — не придирка,
    а то, что ловит ошибку раньше цеха.
    """

    def __init__(self, doc_num: str, area_id: str):
        self.doc_num = doc_num
        super().__init__(
            f"Заказ {doc_num} не найден в сменном задании участка "
            f"«{area_id}». Проверьте, тот ли бланк, либо обратитесь "
            f"к начальнику цеха."
        )


@dataclass
class ScannedOrder:
    """Что прочитано с бланка."""

    doc_num: str          # «ПС00-010109» — номер документа 1С
    doc_date: str         # «2026-08-28» — дата документа
    raw: str

    @property
    def key(self) -> str:
        """Ключ операции в 1С — пара «номер + дата»."""
        return f"{self.doc_num}|{self.doc_date}"


def parse_order_code(raw: str) -> ScannedOrder:
    """
    Разобрать код с бланка заказа.

    Отказ подробен намеренно: оператор у станка должен понять из экрана,
    что он отсканировал не то, а не увидеть «ошибка».
    """
    if raw is None or not raw.strip():
        raise ScanNotRecognized(str(raw), "пустой код")

    s = raw.strip()
    parts = None
    for sep in SEPARATORS:
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            break
    if parts is None or len(parts) != 2:
        raise ScanNotRecognized(
            s, "ожидалась пара «номер документа и дата», "
               "например ПС00-010109|2026-08-28")

    doc_num, doc_date = parts
    if not DOC_NUM_RE.match(doc_num):
        raise ScanNotRecognized(
            s, f"«{doc_num}» не похоже на номер документа 1С")
    if not DATE_RE.match(doc_date):
        raise ScanNotRecognized(
            s, f"«{doc_date}» не похоже на дату в виде ГГГГ-ММ-ДД")

    return ScannedOrder(doc_num=doc_num, doc_date=doc_date, raw=s)


class OrderScanResolver:
    """
    Отсканированный бланк → номер заказа БАЗИС, с которым работает система.

    Связка «документ 1С ↔ заказ БАЗИС» уже построена смежным
    направлением и живёт в order_links. Здесь она только читается:
    заводить вторую связку нельзя, иначе об одном заказе появятся
    две истины.
    """

    def __init__(self, storage):
        self.st = storage

    def resolve(self, raw: str, area_id: str,
                shift_task: Optional[list[int]] = None) -> int:
        """
        Вернуть номер заказа БАЗИС по коду с бланка.

        shift_task — номера заказов сменного задания участка (SR-108).
        Если не задан, поиск идёт по всей связке: так работает до того,
        как появится выгрузка сменного задания (пункт 0.8 шага 0).
        """
        scanned = parse_order_code(raw)
        order_num = self.st.find_order_by_doc(scanned.doc_num, scanned.doc_date)
        if order_num is None:
            raise ScanNotRecognized(
                scanned.raw,
                f"документ {scanned.doc_num} от {scanned.doc_date} "
                f"не связан ни с одним заказом БАЗИС")

        if shift_task is not None and order_num not in shift_task:
            raise OrderNotInShiftTask(scanned.doc_num, area_id)

        return order_num
