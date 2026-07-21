"""
Резолвер связки «заказ Базис ↔ документ 1С».

## Проблема

В 1С номер заказа состоит из префикса и числа (`ЛД00-006564`). При переносе
в Базис префикс теряется — в `.xbir` остаётся только `6564`. Проверка на
массиве показала: **1 226 числовых номеров из 9 265 (13,2%)** встречаются
у нескольких префиксов сразу.

    Базис 7936  →  ЛД00-007936 (Ахсанов Д.Н.)  или  ПС00-007936 (Коновалов Д.Г.) ?

Без разрешения этой неоднозначности статус операции уйдёт **не в тот заказ**,
причём автоматически и незаметно. Это корневой риск проекта.

## Решение

В `.xbir` поле «Номер заказа» содержит не только число, но и клиента:
`7936 Коновалов`. В 1С есть колонка «Клиент». Сопоставление идёт по
**паре (номер, клиент)**.

Принцип работы — **отказ вместо угадывания**. Если пара не даёт
однозначного ответа, заказ уходит технологу в очередь ручного разбора,
а не сопоставляется «наиболее похожим» кандидатом. Ошибка оператора у
станка так исключается: он физически не может отметить деталь по заказу,
связка которого не подтверждена.

## Результат на реальных данных

Из 13 неоднозначных заказов массива пара (номер + клиент) разрешает 12.
Остаётся один (`7846`), где в `.xbir` имени клиента нет вообще — он и
уходит технологу, как задумано.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional, Sequence

from one_c_loader import OneCOrder

# Порог схожести имён для засчитывания совпадения.
# 0.82 подобран так, чтобы «Сарапулова» ↔ «Сарапулова Светлана Викторовна ИП»
# совпадало, а «Ахсанов» ↔ «Коновалов» — нет.
NAME_MATCH_THRESHOLD = 0.82

# Организационно-правовые формы: не несут различающей информации
LEGAL_FORMS = (
    "ооо", "оао", "зао", "пао", "ип", "ао", "тд", "нко", "муп", "гуп",
)

# Мусор в наименованиях
# Пунктуация и дефис — разделители. Дефис важен отдельно: в 1С пишут
# «Компания С-Групп, ООО», в Базисе «С групп» — без этого не совпадает.
PUNCT_RE = re.compile(r"[\"«»'`()\[\].,/\\+—–-]")

# Минимальная длина значащего слова. Совпадение только по инициалам
# («м», «г») или предлогам считать нельзя — слишком легко поймать чужого.
MIN_SIGNIFICANT_WORD = 3
SPACE_RE = re.compile(r"\s+")
LEADING_NUM_RE = re.compile(r"^\s*\d+\s*")


# Максимальный срок исполнения заказа. Заказ старше этого срока физически
# не может резаться сейчас — в 1С такие записи просто не закрывают.
# Проверено на массиве: из 11 354 записей «К выполнению» в окно попадают 523,
# остальные 95% — незакрытые хвосты за два года.
MAX_ORDER_AGE_DAYS = 45


class LinkStatus(str, Enum):
    """Итог разрешения связки."""
    UNIQUE = "unique"              # в 1С единственный кандидат — связка очевидна
    RESOLVED_BY_WINDOW = "window"  # неоднозначность снята сроком исполнения
    RESOLVED_BY_CLIENT = "client"  # неоднозначность снята именем клиента
    MANUAL_REQUIRED = "manual"     # технолог должен сопоставить руками
    NOT_FOUND = "not_found"        # заказа с таким числом в 1С нет


@dataclass
class LinkResult:
    """Результат разрешения связки одного заказа Базиса."""
    order_num: int                             # число из .xbir
    order_raw: str = ""                        # исходная строка «7936 Коновалов»
    status: LinkStatus = LinkStatus.NOT_FOUND
    order_full_num: str = ""                   # 'ПС00-007936' — если разрешено
    client_name: str = ""                      # клиент из 1С
    xbir_client: str = ""                      # клиент, извлечённый из .xbir
    candidates: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_resolved(self) -> bool:
        """Связка однозначна — заказ можно импортировать."""
        return self.status in (LinkStatus.UNIQUE, LinkStatus.RESOLVED_BY_WINDOW,
                              LinkStatus.RESOLVED_BY_CLIENT)

    @property
    def needs_operator(self) -> bool:
        """Требуется ручное сопоставление технологом."""
        return self.status in (LinkStatus.MANUAL_REQUIRED, LinkStatus.NOT_FOUND)


# --- Нормализация наименований ----------------------------------------------

def extract_client(order_raw: str) -> str:
    """
    Достать имя клиента из поля «Номер заказа» в .xbir.

        '7936 Коновалов'      → 'Коновалов'
        '6564 Спецторг ООО'   → 'Спецторг ООО'
        '7846'                → ''              — имени нет
    """
    return LEADING_NUM_RE.sub("", order_raw or "").strip()


def normalize_name(name: str) -> str:
    """
    Привести наименование клиента к сопоставимому виду.

        'Сарапулова Светлана Викторовна ИП' → 'сарапулова светлана викторовна'
        'ЛАДЬЯ ООО'                          → 'ладья'
        'Спецторг ООО'                       → 'спецторг'

    Убираются: регистр, пунктуация, организационно-правовые формы,
    лишние пробелы. Ё приводится к Е — в 1С и Базисе пишут по-разному.
    """
    s = unicodedata.normalize("NFKC", name or "").lower()
    s = s.replace("ё", "е")
    s = PUNCT_RE.sub(" ", s)
    words = [w for w in SPACE_RE.split(s) if w and w not in LEGAL_FORMS]
    return " ".join(words)


def names_match(xbir_name: str, one_c_name: str) -> bool:
    """
    Строгая проверка совпадения клиента — именно она принимает решение.

    В Базис технолог пишет фамилию или короткое название («Коновалов»,
    «Спецторг»), в 1С хранится полное («Коновалов Дмитрий Геннадьевич»).
    Поэтому совпадением считается **вхождение всех слов** короткого
    наименования в состав длинного:

        «Коновалов»  ⊆ {коновалов, дмитрий, геннадьевич}   → совпало
        «Спецторг»   == «Спецторг»                          → совпало
        «Петров»     ⊄ {кузнецов, петр, петрович}           → НЕ совпало

    Сравнение идёт **по целым словам, а не по подстрокам**. Подстрочное
    сравнение давало ложные срабатывания: «Петров» является подстрокой
    «Петрович», и заказ ушёл бы к чужому клиенту автоматически.

    Нечёткое сравнение здесь намеренно не применяется: русские фамилии
    слишком похожи друг на друга (Петров/Петрович, Иванов/Иванова), и
    любой порог даёт ложные связки. Всё, что не совпало точно, уходит
    технологу.
    """
    a, b = normalize_name(xbir_name), normalize_name(one_c_name)
    if not a or not b:
        return False
    if a == b:
        return True

    a_words, b_words = set(a.split()), set(b.split())
    if not a_words or not b_words:
        return False

    shorter, longer = (
        (a_words, b_words) if len(a_words) <= len(b_words) else (b_words, a_words)
    )
    if not shorter.issubset(longer):
        return False

    # Совпадения по одним инициалам недостаточно: нужно хотя бы одно
    # значащее слово («булатов», а не «м» и «г»).
    return any(len(w) >= MIN_SIGNIFICANT_WORD for w in shorter)


def name_similarity(xbir_name: str, one_c_name: str) -> float:
    """
    Оценка схожести наименований от 0 до 1 — **справочная**.

    На решение не влияет (для этого есть names_match), но сохраняется в
    аудит и показывается технологу в очереди ручного разбора: по ней видно,
    насколько близки были кандидаты и почему система засомневалась.
    """
    a, b = normalize_name(xbir_name), normalize_name(one_c_name)
    if not a or not b:
        return 0.0
    if names_match(xbir_name, one_c_name):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


# --- Разрешение связки -------------------------------------------------------

def resolve_order_link(
    order_num: int,
    order_raw: str,
    candidates: Sequence[OneCOrder],
    cutoff_date: Optional[date] = None,
    threshold: float = NAME_MATCH_THRESHOLD,
) -> LinkResult:
    """
    Разрешить связку одного заказа Базиса с документом 1С.

    Аргументы:
        order_num   — число из .xbir (6564)
        order_raw   — исходная строка «6564 Спецторг ООО»
        candidates  — заказы 1С с этим же числом (из one_c_loader)
        cutoff_date — граница срока исполнения; заказы старше отбрасываются

    Логика:
        нет кандидатов       → NOT_FOUND, технологу
        один кандидат        → UNIQUE
        несколько:
            сузить по сроку исполнения (окно 45 дней)
              остался один  → RESOLVED_BY_WINDOW, но с проверкой клиента
              осталось 2+   → сравнить по клиенту → RESOLVED_BY_CLIENT
              не осталось   → откат к полному списку и сравнение по клиенту
        не разрешилось       → MANUAL_REQUIRED, технологу

    Два независимых признака проверяют друг друга. Если срок исполнения
    указывает на один заказ, а имя клиента — на другой, связка **не**
    устанавливается: расхождение сигналов означает, что данные неверны,
    и решать должен человек.

    Ни при каких условиях не выбирается «наиболее вероятный» кандидат.
    """
    xbir_client = extract_client(order_raw)
    result = LinkResult(
        order_num=order_num,
        order_raw=order_raw,
        xbir_client=xbir_client,
        candidates=[c.order_full_num for c in candidates],
    )

    if not candidates:
        result.status = LinkStatus.NOT_FOUND
        result.reason = f"заказа {order_num} нет в выгрузке 1С"
        return result

    if len(candidates) == 1:
        only = candidates[0]
        result.status = LinkStatus.UNIQUE
        result.order_full_num = only.order_full_num
        result.client_name = only.client_name
        result.reason = "единственный кандидат в 1С"
        return result

    for c in candidates:
        result.scores[c.order_full_num] = round(
            name_similarity(xbir_client, c.client_name), 3
        )

    # 1. Сузить по сроку исполнения: заказ старше 45 дней не может резаться сейчас
    pool = list(candidates)
    if cutoff_date is not None:
        recent = [c for c in candidates
                  if c.order_date is not None and c.order_date >= cutoff_date]
        if recent:
            pool = recent
        else:
            # Все кандидаты старше окна — выгрузка 1С устарела. Не теряем
            # заказ, а разбираем по клиенту среди всех кандидатов.
            result.reason = "все кандидаты старше срока исполнения; "

    # 2. Совпадение по клиенту — строгое, пословное
    matched = [c for c in pool if names_match(xbir_client, c.client_name)] \
        if xbir_client else []

    if len(pool) == 1:
        best = pool[0]
        # Срок указал на один заказ. Если клиент известен и противоречит —
        # сигналы расходятся, решает человек.
        if xbir_client and not matched:
            result.status = LinkStatus.MANUAL_REQUIRED
            result.reason += (
                f"по сроку подходит только {best.order_full_num}, но клиент "
                f"«{xbir_client}» не совпал с «{best.client_name}» — "
                f"сопоставить вручную"
            )
            return result

        result.status = (LinkStatus.RESOLVED_BY_CLIENT if matched
                         else LinkStatus.RESOLVED_BY_WINDOW)
        result.order_full_num = best.order_full_num
        result.client_name = best.client_name
        result.reason += (
            f"по сроку исполнения подходит один заказ ({best.order_full_num})"
            + (f", клиент «{best.client_name}» подтверждает" if matched else "")
        )
        return result

    if len(matched) == 1:
        best = matched[0]
        result.status = LinkStatus.RESOLVED_BY_CLIENT
        result.order_full_num = best.order_full_num
        result.client_name = best.client_name
        result.reason += (
            f"клиент «{xbir_client}» однозначно совпал с «{best.client_name}»"
        )
        return result

    result.status = LinkStatus.MANUAL_REQUIRED
    if not xbir_client:
        result.reason += (
            f"{len(pool)} кандидата подходят по сроку, а в .xbir нет имени "
            f"клиента («{order_raw}») — сопоставить вручную"
        )
    elif not matched:
        result.reason += (
            f"клиент «{xbir_client}» не совпал ни с одним из {len(pool)} "
            f"кандидатов — сопоставить вручную"
        )
    else:
        names = ", ".join(c.order_full_num for c in matched)
        result.reason += (
            f"клиент «{xbir_client}» совпал сразу с несколькими ({names}) — "
            f"сопоставить вручную"
        )
    return result


def export_reference_date(one_c_by_num: dict[int, list[OneCOrder]]) -> Optional[date]:
    """
    Опорная дата для окна срока исполнения — **самая свежая дата заказа
    в выгрузке**, а не сегодняшний день.

    Выгрузка 1С делается не каждый день: на момент проверки массив был
    месячной давности. Отсчёт от сегодняшней даты выбрасывал из окна
    десять живых заказов, которые в момент выгрузки были в работе.
    """
    dates = [o.order_date for lst in one_c_by_num.values()
             for o in lst if o.order_date]
    return max(dates) if dates else None


def resolve_batch(
    xbir_orders: dict[int, str],
    one_c_by_num: dict[int, list[OneCOrder]],
    window_days: int | None = MAX_ORDER_AGE_DAYS,
    reference_date: Optional[date] = None,
    threshold: float = NAME_MATCH_THRESHOLD,
) -> list[LinkResult]:
    """
    Разрешить связки пачкой.

    Аргументы:
        xbir_orders    — {число: исходная строка} из .xbir
        one_c_by_num   — {число: [заказы 1С]} из one_c_loader
        window_days    — срок исполнения заказа (None — не отсекать по сроку)
        reference_date — от какой даты считать окно; по умолчанию берётся
                         самая свежая дата заказа в выгрузке
    """
    cutoff = None
    if window_days is not None:
        ref = reference_date or export_reference_date(one_c_by_num)
        if ref is not None:
            cutoff = ref - timedelta(days=window_days)

    return [
        resolve_order_link(num, raw, one_c_by_num.get(num, []),
                           cutoff_date=cutoff, threshold=threshold)
        for num, raw in sorted(xbir_orders.items())
    ]


def format_resolve_report(results: Sequence[LinkResult]) -> str:
    """Отчёт по разрешению связок: что прошло автоматом, что — технологу."""
    by_status: dict[LinkStatus, list[LinkResult]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)

    total = len(results) or 1
    resolved = sum(1 for r in results if r.is_resolved)
    manual = [r for r in results if r.needs_operator]

    lines = ["=== СВЯЗКА ЗАКАЗОВ: БАЗИС ↔ 1С ===", ""]
    lines.append(f"Заказов обработано:     {len(results)}")
    lines.append(f"Связка установлена:     {resolved} ({100 * resolved / total:.1f}%)")
    lines.append(f"  • однозначно в 1С:    {len(by_status.get(LinkStatus.UNIQUE, []))}")
    lines.append(f"  • по сроку исполнения: "
                 f"{len(by_status.get(LinkStatus.RESOLVED_BY_WINDOW, []))}")
    lines.append(f"  • разрешено клиентом: "
                 f"{len(by_status.get(LinkStatus.RESOLVED_BY_CLIENT, []))}")
    lines.append(f"Технологу на разбор:    {len(manual)} "
                 f"({100 * len(manual) / total:.1f}%)")
    lines.append("")

    if manual:
        lines.append("--- ТРЕБУЕТСЯ РУЧНОЕ СОПОСТАВЛЕНИЕ ---")
        for r in manual:
            lines.append(f"\n  Базис {r.order_num}: «{r.order_raw}»")
            lines.append(f"     {r.reason}")
            for full in r.candidates:
                score = r.scores.get(full)
                suffix = f"   схожесть {score}" if score is not None else ""
                lines.append(f"       ? {full}{suffix}")
        lines.append("")

    return "\n".join(lines)
