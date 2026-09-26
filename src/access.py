"""
Роли, права и вход по бейджу (SR-96 — SR-100).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ
    До него в `psr_app` оператор был строкой в поле формы: кто угодно мог
    назваться кем угодно, и любое действие было доступно всем. В модуле
    об этом было написано прямо — «делать вид, что права есть, нельзя,
    поэтому их нет явно, а не наполовину». Здесь они появляются.

ЧЕТЫРЕ СВОЙСТВА, КОТОРЫЕ ЗДЕСЬ ОБЕСПЕЧИВАЮТСЯ

    SR-96. Действие разрешено ТОЛЬКО своей роли, и соответствие
    «роль → действия» лежит в справочнике КАК ДАННЫЕ, не в коде.
    Поэтому ниже нет ни одного `if role == "operator"`: проверка всегда
    идёт запросом к справочнику. Технолог меняет права, не трогая код
    (SR-83: справочники суть данные, а не код).

    SR-97. Вход — скан личного бейджа, БЕЗ ПАРОЛЯ. Роль определяется
    бейджем. Пароля нет не по недосмотру: у станка с деталью в руках
    пароль вводить нечем и некогда, и любой введённый один раз пароль
    в цехе немедленно становится общим.

    SR-99. Изменение справочника, норматива, правила запуска и
    ретроспективная отметка доступны только роли с правом изменения и
    СОХРАНЯЮТ АВТОРА И МОМЕНТ. Право и запись здесь неразделимы:
    разрешить, не записав, значит не выполнить требование.

    SR-100. Исполнитель обозначается КОДОМ, таблица соответствия кодов
    и ФИО хранится вне выгрузок. См. ниже про `operator_id`.

ПОЧЕМУ ЗДЕСЬ НОВЫЙ КОД ИСПОЛНИТЕЛЯ (CF-421)
    `import_operators` порождает `operator_id` ТРАНСЛИТЕРАЦИЕЙ ФИО:
    `ivanov_pi` читается человеком без всякой таблицы соответствия.
    Это не код, а обратимая запись того же ФИО, и SR-100 таким
    идентификатором не выполняется — замена ФИО на него в выгрузке лишь
    меняет кириллицу на латиницу.

    Поэтому вводится `person_code` вида `ИСП-0001` — порядковый и
    непрозрачный. Соответствие «код ↔ ФИО» живёт в таблице `persons`,
    и ни одна выгрузка её не читает. Прежний `operator_id` не удаляется:
    на него опирается накопленный факт смежного направления. Он
    остаётся внутренним ключом, а наружу идёт `person_code`.

ЧЕГО ЗДЕСЬ НЕТ
    Бригадной отметки (SR-98): она относится к событию операции, а не
    ко входу, и живёт в подетальном учёте. Здесь — только вход и права.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# --- Роли. Строки, а не Enum: справочник ведёт технолог (SR-83) ------------
OPERATOR = "operator"          # оператор участка
STOREKEEPER = "storekeeper"    # кладовщик склада готовой продукции
CHIEF = "chief"                # начальник цеха
PRODUCTION = "production"      # руководитель производства
TECHNOLOGIST = "technologist"  # технолог
MATERIALS = "materials"        # склад материалов
ADMIN = "admin"                # администратор системы

ROLE_NAMES = {
    OPERATOR: "Оператор участка",
    STOREKEEPER: "Кладовщик склада готовой продукции",
    CHIEF: "Начальник цеха",
    PRODUCTION: "Руководитель производства",
    TECHNOLOGIST: "Технолог",
    MATERIALS: "Склад материалов",
    ADMIN: "Администратор системы",
}

# --- Действия. Имя действия — то, что проверяется, а не экран --------------
# Перечень взят из §1.4 SRS: «какие события создаёт роль». Он НЕ является
# правами — права лежат в справочнике. Здесь только имена, чтобы вызывающий
# код не изобретал их по месту и опечатка не превращалась в тихое «разрешено».
ACT_ORDER_OPEN = "order.open"            # SR-20
ACT_ORDER_CLOSE = "order.close"          # SR-20а
ACT_DOWNTIME = "downtime.register"       # SR-19
ACT_DEFECT = "defect.register"           # SR-21
ACT_QUALITY = "quality.register"         # SR-22 (D-177)
ACT_RECEIPT = "receipt.register"         # SR-87, SR-88
ACT_CLAIM = "claim.register"             # SR-61 — SR-64
ACT_REWORK_SEND = "rework.send"          # CHG-002, D-214
ACT_REWORK_REDIRECT = "rework.redirect"  # D-216: только начальник цеха
ACT_LAUNCH_OVERRIDE = "launch.override"  # SR-76
ACT_REF_EDIT = "reference.edit"          # SR-83, SR-99
ACT_NORM_EDIT = "norm.edit"              # SR-79, SR-80, SR-99
ACT_RETRO_MARK = "mark.retrospective"    # SR-13, SR-99

# Действия, которые SR-99 требует сопровождать записью автора и момента.
AUDITED_ACTIONS = frozenset({
    ACT_REF_EDIT, ACT_NORM_EDIT, ACT_LAUNCH_OVERRIDE, ACT_RETRO_MARK,
    ACT_REWORK_REDIRECT,
})


class AccessDenied(Exception):
    """
    Действие не разрешено роли (SR-96).

    Сообщение называет роль и действие: оператор должен понять, что ему
    нужен другой человек, а не что «система сломалась».
    """

    def __init__(self, person_code: str, role: str, action: str):
        self.person_code, self.role, self.action = person_code, role, action
        super().__init__(
            f"Исполнителю {person_code} (роль «{ROLE_NAMES.get(role, role)}») "
            f"действие «{action}» не разрешено (SR-96). "
            f"Права задаются справочником ролей."
        )


class BadgeNotRecognized(Exception):
    """Бейдж не найден в справочнике исполнителей (SR-97)."""

    def __init__(self, badge_id: str):
        super().__init__(
            f"Бейдж не опознан. Проверьте, что он заведён в справочнике "
            f"исполнителей, либо обратитесь к администратору системы."
        )


class PersonInactive(Exception):
    """Исполнитель заведён, но работать в системе не вправе."""

    def __init__(self, person_code: str):
        super().__init__(
            f"Исполнитель {person_code} отключён в справочнике. "
            f"Вход невозможен."
        )


@dataclass
class Person:
    """
    Исполнитель. Наружу — код, внутрь — ФИО.

    `full_name` присутствует, потому что на экране цеха человек должен
    видеть себя по имени: «ИСП-0147» у станка никого не узнаёт. Но за
    пределы экрана имя не уходит — за этим следит `to_export()`.
    """
    person_code: str
    full_name: str
    role: str
    badge_id: str = ""
    operator_id: str = ""   # прежний ключ смежного направления, внутренний
    active: bool = True

    @property
    def role_name(self) -> str:
        return ROLE_NAMES.get(self.role, self.role)

    def to_export(self) -> dict:
        """
        Представление для отчётов и выгрузок (SR-100).

        ФИО здесь нет и быть не может. Метод существует именно затем,
        чтобы выгрузку нельзя было собрать из `Person.__dict__`, не
        заметив имени.
        """
        return {"executor_code": self.person_code, "role": self.role}


@dataclass
class Session:
    """Вход исполнителя по бейджу. Без пароля (SR-97)."""
    token: str
    person: Person
    opened_at: str
    area_id: str = ""


class Access:
    """Справочник ролей, вход по бейджу и проверка прав."""

    def __init__(self, storage):
        self.st = storage

    # -- справочник исполнителей -------------------------------------------
    def add_person(self, full_name: str, role: str, badge_id: str = "",
                   operator_id: str = "", person_code: str = "") -> Person:
        """
        Завести исполнителя. Код присваивается системой, а не человеком.

        Код порядковый (`ИСП-0001`) и НЕПРОЗРАЧНЫЙ: по нему нельзя
        восстановить ФИО без таблицы соответствия — в этом всё различие
        с прежним `operator_id`, который был транслитерацией имени
        (CF-421).
        """
        code = person_code or self._next_code()
        self.st.upsert_person(dict(
            person_code=code, full_name=full_name, role=role,
            badge_id=badge_id, operator_id=operator_id, active=1))
        return Person(code, full_name, role, badge_id, operator_id, True)

    def _next_code(self) -> str:
        return f"ИСП-{self.st.next_person_seq():04d}"

    def get_person(self, person_code: str) -> Optional[Person]:
        row = self.st.get_person(person_code)
        return self._row_to_person(row) if row else None

    @staticmethod
    def _row_to_person(row) -> Person:
        return Person(row["person_code"], row["full_name"], row["role"],
                      row["badge_id"] or "", row["operator_id"] or "",
                      bool(row["active"]))

    # -- вход по бейджу (SR-97) --------------------------------------------
    def login_by_badge(self, badge_id: str, area_id: str = "") -> Session:
        """
        Вход сканом бейджа. Пароль не запрашивается и не хранится.

        Почему без пароля — см. заголовок модуля: у станка его вводить
        нечем, а введённый однажды в цехе становится общим.
        """
        row = self.st.get_person_by_badge((badge_id or "").strip())
        if row is None:
            raise BadgeNotRecognized(badge_id)
        person = self._row_to_person(row)
        if not person.active:
            raise PersonInactive(person.person_code)

        token = secrets.token_urlsafe(16)
        opened_at = datetime.now().isoformat(timespec="seconds")
        self.st.open_access_session(token, person.person_code,
                                    area_id, opened_at)
        return Session(token, person, opened_at, area_id)

    def session(self, token: str) -> Optional[Session]:
        row = self.st.get_access_session(token)
        if row is None:
            return None
        person = self.get_person(row["person_code"])
        if person is None or not person.active:
            return None
        return Session(token, person, row["opened_at"], row["area_id"] or "")

    def logout(self, token: str) -> None:
        self.st.close_access_session(token)

    # -- права (SR-96) ------------------------------------------------------
    def grant(self, role: str, action: str) -> None:
        """Разрешить роли действие. Это запись в справочник, не в код."""
        self.st.set_role_permission(role, action, 1)

    def revoke(self, role: str, action: str) -> None:
        self.st.set_role_permission(role, action, 0)

    def may(self, role: str, action: str) -> bool:
        """
        Разрешено ли роли действие — ПО СПРАВОЧНИКУ.

        Умолчания «разрешено» нет намеренно: незаполненный справочник
        означает «ничего никому», а не «всем всё». Пустая таблица прав
        видна сразу — первым же отказом; молчаливое «всем всё» не видно
        никогда.
        """
        return self.st.get_role_permission(role, action) == 1

    def require(self, session: Optional[Session], action: str,
                note: str = "") -> Person:
        """
        Проверить право и — для действий SR-99 — записать, кто и когда.

        Право и запись сделаны одним вызовом намеренно: если бы запись
        была отдельным действием, её однажды забыли бы, и требование
        «сохраняет автора и момент изменения» выполнялось бы через раз.
        """
        if session is None:
            raise AccessDenied("—", "—", action)
        person = session.person
        if not self.may(person.role, action):
            raise AccessDenied(person.person_code, person.role, action)
        if action in AUDITED_ACTIONS:
            self.st.log_privileged_action(
                person.person_code, person.role, action,
                datetime.now().isoformat(timespec="seconds"), note)
        return person

    def audit_trail(self, action: str = "") -> list:
        """Журнал действий, требующих авторства (SR-99)."""
        return self.st.get_privileged_actions(action)


# --- Набор прав по умолчанию (SETUP-001, лист «Роли и права») --------------
# Это НЕ права в коде: технолог заполняет справочник сам, а функция ниже
# лишь раскладывает его лист в таблицу, чтобы пилот не начинался с пустого
# справочника и отказа на каждое действие. Вызывать её на рабочей базе,
# где права уже заданы, не нужно.
DEFAULT_PERMISSIONS: dict[str, tuple[str, ...]] = {
    OPERATOR: (ACT_ORDER_OPEN, ACT_ORDER_CLOSE, ACT_DOWNTIME,
               ACT_DEFECT, ACT_QUALITY),
    STOREKEEPER: (ACT_RECEIPT,),
    CHIEF: (ACT_CLAIM, ACT_REWORK_SEND, ACT_REWORK_REDIRECT,
            ACT_LAUNCH_OVERRIDE),
    PRODUCTION: (ACT_NORM_EDIT,),
    TECHNOLOGIST: (ACT_REF_EDIT,),
    MATERIALS: (),
    ADMIN: (ACT_REF_EDIT, ACT_NORM_EDIT, ACT_RETRO_MARK),
}


def seed_default_permissions(access: Access) -> int:
    """Разложить лист «Роли и права» SETUP-001 в справочник. Возвращает счёт."""
    n = 0
    for role, actions in DEFAULT_PERMISSIONS.items():
        for action in actions:
            access.grant(role, action)
            n += 1
    return n
