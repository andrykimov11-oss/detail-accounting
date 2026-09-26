"""
Навигация по скану на участке раскроя (SR-107 — SR-116, `CHG-001`).

ЧТО ЭТО МЕНЯЕТ В ЦЕХЕ
    Сегодня оператор раскроя ищет задание руками: цвет верхнего листа →
    бумажный список по декору → ФИО менеджера → папка менеджера → номер
    заказа → декор → программа. Семь действий, из которых шесть — поиск.

    Становится: отсканировал бланк — система нашла папку, показала
    декоры с числом листов, спросила подтверждение и положила программы
    в рабочую папку станка.

    Это первое, что система ОТДАЁТ цеху, а не просит у него. Решение
    D-197: минус шесть действий поиска взамен двух отметок.

ЧЕТЫРЕ ПРИНЦИПА, ЗАЛОЖЕННЫЕ ЗДЕСЬ

    SR-108. Отказ вместо угадывания. Заказ ищется В ПРЕДЕЛАХ сменного
    задания; нет в задании — отказ с названной причиной, а не подбор
    похожего. Похожий заказ, поданный как найденный, дороже отказа:
    отказ виден оператору сразу, подмена — через смену, в браке.

    SR-110. Номер 1С и номер папки БАЗИС — РАЗНЫЕ величины. Папка
    ищется через связку, а не сопоставлением номеров. Прямого совпадения
    между ними нет, и попытка искать папку по номеру 1С даст либо
    пустоту, либо чужой заказ.

    SR-112. Число листов берётся ИЗ ЗАДАНИЯ, где оно целое. В бланке
    заказа клиента величина дробная — она означает расход материала при
    работе по деловым остаткам. Показать оператору «2,4 листа» значит
    задать ему вопрос, на который нет ответа.

    SR-113. Подтверждение — событие, а не флаг. Отзыв не стирает
    подтверждение, а добавляется следующим событием: иначе на вопрос
    «кто подтвердил ошибочную связку» ответить было бы нечем.

ОБ ИМЕНАХ ПАПОК — ЭТО ПЕРСОНАЛЬНЫЕ ДАННЫЕ
    Каталог БАЗИС именует папки заказов как «1980-Petrjaeva»: номер и
    ФАМИЛИЯ КЛИЕНТА. Путь нужен, чтобы скопировать программы, и он
    возвращается вызывающему коду. Но наружу — в отчёты, выгрузки и
    журналы — идёт только номер заказа: см. `Decor.to_report()` и
    `NavResult.to_report()`. SR-100 запрещает ФИО клиентов в отчётах, а
    имя папки есть ФИО клиента.

ЧЕГО ЗДЕСЬ НЕТ
    Локального агента на рабочем месте оператора (SR-114, решение A-09а):
    система готовит рабочую папку станка, на компьютере оператора не
    ставится ничего. Поэтому здесь копирование файлов, а не запуск
    программ: открывает их оператор сам, привычным образом.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Имя папки заказа в каталоге БАЗИС: ведущая группа цифр — номер заказа,
# дальше может идти что угодно, включая фамилию клиента.
ORDER_DIR_RE = re.compile(r"^(\d{1,6})(?:[-_].*)?$")

# Управляющие программы раскроя.
PROGRAM_SUFFIXES = (".xprg", ".scm", ".scx")

SETTING_BAZIS_ROOT = "bazis_root"        # каталог заданий БАЗИС
SETTING_MACHINE_DIR = "machine_dir"      # рабочая папка станка

CONFIRMED = "confirmed"
REVOKED = "revoked"


class OrderFolderNotFound(Exception):
    """
    Папка заказа не найдена в каталоге БАЗИС.

    Сообщение НЕ содержит имени папки: в нём фамилия клиента. Называется
    номер заказа — этого достаточно, чтобы разобраться.
    """

    def __init__(self, order_num: int, root: str):
        self.order_num = order_num
        super().__init__(
            f"Задание раскроя по заказу {order_num} не найдено в каталоге "
            f"БАЗИС. Проверьте, выгружен ли заказ, либо обратитесь "
            f"к технологу."
        )


class BazisRootNotSet(Exception):
    """Каталог заданий БАЗИС не задан в настройках."""

    def __init__(self):
        super().__init__(
            "Не задан каталог заданий БАЗИС. Задаётся настройкой "
            f"«{SETTING_BAZIS_ROOT}» администратором системы."
        )


class LinkNotConfirmed(Exception):
    """Оператор ещё не подтвердил соответствие декоров и листов (SR-111)."""

    def __init__(self, order_num: int):
        super().__init__(
            f"По заказу {order_num} не подтверждено соответствие декоров "
            f"и числа листов. Подтвердите перед началом работы."
        )


@dataclass
class Decor:
    """Декор заказа: что показать оператору перед подтверждением."""
    name: str
    sheets: int                    # ЦЕЛОЕ, из сменного задания (SR-112)
    folder: Optional[Path] = None  # папка декора в каталоге БАЗИС
    programs: list = field(default_factory=list)

    @property
    def has_programs(self) -> bool:
        return bool(self.programs)

    def to_report(self) -> dict:
        """Представление без путей: в пути — фамилия клиента (SR-100)."""
        return {"decor": self.name, "sheets": self.sheets,
                "programs": len(self.programs)}


@dataclass
class NavResult:
    """Что система нашла по одному скану."""
    order_num: int
    decors: list
    confirmed: bool
    single_decor: bool             # SR-116: один декор — без списка

    def to_report(self) -> dict:
        return {"order_num": self.order_num,
                "confirmed": self.confirmed,
                "single_decor": self.single_decor,
                "decors": [d.to_report() for d in self.decors]}


class RaskroyNavigator:
    """
    Поиск задания раскроя по опознанному заказу и подготовка папки станка.

    Работает ПОСЛЕ `OrderScanResolver`: тот превращает код с бланка в
    номер заказа БАЗИС, этот — номер заказа в программы на станке.
    Разделено намеренно: опознание заказа нужно всем шести участкам,
    навигация по программам — только раскрою.
    """

    def __init__(self, storage):
        self.st = storage

    # -- настройки ----------------------------------------------------------
    def _root(self) -> Path:
        raw = self.st.get_setting(SETTING_BAZIS_ROOT)
        if not raw:
            raise BazisRootNotSet()
        return Path(raw)

    def machine_dir(self) -> Optional[Path]:
        raw = self.st.get_setting(SETTING_MACHINE_DIR)
        return Path(raw) if raw else None

    # -- поиск папки заказа (SR-110) ---------------------------------------
    def order_folder(self, order_num: int) -> Path:
        """
        Папка заказа в каталоге БАЗИС — ПО НОМЕРУ ЗАКАЗА БАЗИС.

        Номер приходит из связки (`order_links`), а не с бланка 1С:
        номер документа 1С и номер папки БАЗИС — разные величины, и
        прямого совпадения между ними нет (SR-110).

        Имя папки сверх номера может нести что угодно, включая фамилию
        клиента, — поэтому сравнивается только ведущая группа цифр.
        """
        root = self._root()
        if not root.exists():
            raise OrderFolderNotFound(order_num, str(root))
        for machine_dir in sorted(root.iterdir()):
            if not machine_dir.is_dir():
                continue
            for folder in sorted(machine_dir.iterdir()):
                if not folder.is_dir():
                    continue
                m = ORDER_DIR_RE.match(folder.name)
                if m and int(m.group(1)) == int(order_num):
                    return folder
        raise OrderFolderNotFound(order_num, str(root))

    # -- декоры и листы (SR-111, SR-112, SR-116) ---------------------------
    def decors(self, order_num: int, task_id: str = "") -> list:
        """
        Декоры заказа с числом листов.

        Число листов берётся ИЗ СМЕННОГО ЗАДАНИЯ (SR-112) — там оно
        целое. Если задания нет (до появления выгрузки, пункт 0.8), декор
        всё равно показывается, но с числом листов 0 и явно: система не
        придумывает величину, которой не знает.
        """
        sheets_by_decor: dict[str, int] = {}
        if task_id:
            for r in self.st.get_task_rows_for_order(task_id, order_num):
                sheets_by_decor[r["decor"]] = int(r["sheets"] or 0)

        folder = self.order_folder(order_num)
        out: list[Decor] = []
        for sub in sorted(folder.iterdir()):
            if not sub.is_dir():
                continue
            programs = sorted(
                f for f in sub.iterdir()
                if f.is_file() and f.suffix.lower() in PROGRAM_SUFFIXES)
            out.append(Decor(name=sub.name,
                             sheets=sheets_by_decor.get(sub.name, 0),
                             folder=sub, programs=programs))
        return out

    def navigate(self, order_num: int, task_id: str = "") -> NavResult:
        """Один скан — вся картина: декоры, листы, нужно ли подтверждение."""
        decors = self.decors(order_num, task_id)
        return NavResult(order_num=order_num, decors=decors,
                         confirmed=self.is_confirmed(order_num),
                         single_decor=len(decors) == 1)

    # -- подтверждение связки (SR-113) -------------------------------------
    def is_confirmed(self, order_num: int) -> bool:
        row = self.st.last_link_confirmation(order_num)
        return row is not None and row["event"] == CONFIRMED

    def confirm(self, order_num: int, person_code: str,
                note: str = "") -> None:
        """
        Подтвердить соответствие. Запрашивается ОДНОКРАТНО на заказ.

        Повторное подтверждение уже подтверждённого не пишет второго
        события: иначе журнал заполнился бы повторами, и «кто подтвердил»
        перестало бы читаться.
        """
        if self.is_confirmed(order_num):
            return
        self.st.log_link_confirmation(order_num, CONFIRMED, person_code, note)

    def revoke(self, order_num: int, person_code: str,
               note: str = "") -> None:
        """
        Отозвать подтверждение.

        Отзыв НЕ стирает подтверждение, а добавляется следующим событием:
        след того, кто подтвердил ошибочную связку, обязан сохраниться.
        """
        if not self.is_confirmed(order_num):
            return
        self.st.log_link_confirmation(order_num, REVOKED, person_code, note)

    def confirmation_history(self, order_num: int) -> list:
        return self.st.link_confirmation_history(order_num)

    # -- подготовка рабочей папки станка (SR-114) --------------------------
    def prepare_machine_folder(self, order_num: int, decor_name: str = "",
                               task_id: str = "",
                               require_confirmation: bool = True) -> list:
        """
        Скопировать программы заказа в рабочую папку станка.

        Копирование, а не запуск: на рабочем месте оператора ничего не
        устанавливается (SR-114, решение A-09а). Оператор открывает
        программу привычным образом, просто она уже лежит там, где надо.

        Папка станка ОЧИЩАЕТСЯ перед копированием — иначе через неделю
        в ней будут программы десяти заказов, и выбор станет таким же
        поиском, от которого мы уходим.
        """
        if require_confirmation and not self.is_confirmed(order_num):
            raise LinkNotConfirmed(order_num)

        dest = self.machine_dir()
        if dest is None:
            raise BazisRootNotSet()
        dest.mkdir(parents=True, exist_ok=True)
        for old in dest.iterdir():
            if old.is_file():
                old.unlink()

        copied: list[Path] = []
        for d in self.decors(order_num, task_id):
            if decor_name and d.name != decor_name:
                continue
            for src in d.programs:
                # Имя несёт декор: в папке станка иначе не различить
                # Board-1 одного декора от Board-1 другого.
                target = dest / f"{d.name}__{src.name}"
                shutil.copy2(src, target)
                copied.append(target)
        return copied
