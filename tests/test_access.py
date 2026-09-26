"""
Тесты ролей, прав и входа по бейджу (SR-96 — SR-100).

Способы проверки взяты из SRS-001 §22 дословно:
    SR-96  «Попытка выполнить действие чужой роли» → отклонена;
           соответствие прочитано из справочника
    SR-97  «Вход исполнителя» → скан бейджа; пароль не запрошен
    SR-99  «Изменение справочника и ретроспективная отметка» → доступны
           только роли с правом; автор и момент сохранены
    SR-100 «Выгрузка и три отчёта» → ФИО и телефонов нет; исполнитель
           обозначен кодом

SR-98 (бригадная отметка) здесь не проверяется: она относится к событию
операции, а не ко входу.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.access import (
    ACT_NORM_EDIT,
    ACT_ORDER_CLOSE,
    ACT_ORDER_OPEN,
    ACT_REF_EDIT,
    ACT_RETRO_MARK,
    ACT_REWORK_REDIRECT,
    ADMIN,
    AUDITED_ACTIONS,
    CHIEF,
    OPERATOR,
    TECHNOLOGIST,
    Access,
    AccessDenied,
    BadgeNotRecognized,
    PersonInactive,
    seed_default_permissions,
)
from src.storage import Storage


@pytest.fixture()
def st():
    s = Storage(os.path.join(tempfile.mkdtemp(), "psr.db"))
    yield s
    s.close()


@pytest.fixture()
def ac(st):
    return Access(st)


# ----------------------------------------------------------------------
# SR-97. Вход — скан бейджа, без пароля
# ----------------------------------------------------------------------
def test_sr97_вход_по_бейджу_без_пароля(ac):
    ac.add_person("Иванов Пётр Сергеевич", OPERATOR, badge_id="B-0001")
    s = ac.login_by_badge("B-0001", area_id="kromlenie")
    assert s.person.role == OPERATOR
    assert s.area_id == "kromlenie"
    assert ac.session(s.token).person.person_code == s.person.person_code


def test_sr97_пароля_нет_нигде_в_схеме(st):
    """
    Требование «без пароля» проверяется не отсутствием поля ввода, а тем,
    что хранить пароль негде: ни столбца, ни хеша в схеме нет.
    """
    tables = [r[0] for r in st._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        cols = {r["name"].lower()
                for r in st._conn.execute(f"PRAGMA table_info({t})")}
        assert not {c for c in cols if "password" in c or "passwd" in c}
        assert "pwd_hash" not in cols


def test_sr97_неопознанный_бейдж_назван_прямо(ac):
    with pytest.raises(BadgeNotRecognized) as e:
        ac.login_by_badge("B-9999")
    assert "справочник" in str(e.value).lower()


def test_sr97_отключённый_исполнитель_не_входит(ac, st):
    p = ac.add_person("Петров Иван", OPERATOR, badge_id="B-0002")
    st.upsert_person(dict(person_code=p.person_code, full_name=p.full_name,
                          role=p.role, badge_id="B-0002", active=0))
    with pytest.raises(PersonInactive):
        ac.login_by_badge("B-0002")


# ----------------------------------------------------------------------
# SR-96. Действие разрешено только своей роли, права — из справочника
# ----------------------------------------------------------------------
def test_sr96_чужое_действие_отклонено(ac):
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-1")
    s = ac.login_by_badge("B-1")
    ac.require(s, ACT_ORDER_OPEN)          # своё — можно
    with pytest.raises(AccessDenied) as e:
        ac.require(s, ACT_NORM_EDIT)       # чужое — нельзя
    assert "не разрешено" in str(e.value)


def test_sr96_права_лежат_в_справочнике_а_не_в_коде(ac):
    """
    Ключевая проверка требования: права меняются ДАННЫМИ. Технолог даёт
    оператору право, которого у него не было, — и оно действует, без
    единой правки кода.
    """
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-1")
    s = ac.login_by_badge("B-1")
    with pytest.raises(AccessDenied):
        ac.require(s, ACT_REF_EDIT)

    ac.grant(OPERATOR, ACT_REF_EDIT)
    assert ac.require(s, ACT_REF_EDIT).role == OPERATOR

    ac.revoke(OPERATOR, ACT_REF_EDIT)
    with pytest.raises(AccessDenied):
        ac.require(s, ACT_REF_EDIT)


def test_sr96_пустой_справочник_запрещает_всё_а_не_разрешает(ac):
    """
    Умолчание выбрано в сторону отказа намеренно. Незаполненный справочник
    виден первым же отказом; молчаливое «всем всё» не видно никогда.
    """
    ac.add_person("Иванов Пётр", ADMIN, badge_id="B-1")
    s = ac.login_by_badge("B-1")
    for action in (ACT_ORDER_OPEN, ACT_ORDER_CLOSE, ACT_REF_EDIT):
        with pytest.raises(AccessDenied):
            ac.require(s, action)


def test_sr96_без_входа_действие_невозможно(ac):
    seed_default_permissions(ac)
    with pytest.raises(AccessDenied):
        ac.require(None, ACT_ORDER_OPEN)


# ----------------------------------------------------------------------
# SR-99. Привилегированное действие сохраняет автора и момент
# ----------------------------------------------------------------------
def test_sr99_автор_и_момент_сохранены(ac):
    seed_default_permissions(ac)
    p = ac.add_person("Сидорова Анна", TECHNOLOGIST, badge_id="B-2")
    s = ac.login_by_badge("B-2")
    ac.require(s, ACT_REF_EDIT, note="измеритель сборки → детали")

    trail = ac.audit_trail(ACT_REF_EDIT)
    assert len(trail) == 1
    assert trail[0]["person_code"] == p.person_code
    assert trail[0]["acted_at"]
    assert "измеритель" in trail[0]["note"]


def test_sr99_обычное_действие_в_журнал_не_пишется(ac):
    """Журнал авторства — про изменения правил, а не про работу цеха."""
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-1")
    s = ac.login_by_badge("B-1")
    ac.require(s, ACT_ORDER_OPEN)
    ac.require(s, ACT_ORDER_CLOSE)
    assert ac.audit_trail() == []


def test_sr99_отказ_в_праве_записи_не_оставляет(ac):
    """
    Журнал — про совершённое, а не про попытки. Иначе отказ выглядел бы
    в нём как изменение, которого не было.
    """
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-1")
    s = ac.login_by_badge("B-1")
    with pytest.raises(AccessDenied):
        ac.require(s, ACT_RETRO_MARK)
    assert ac.audit_trail() == []


def test_sr99_переадресация_переделки_под_авторством(ac):
    """D-216: переадресует начальник цеха, и смена записывается."""
    assert ACT_REWORK_REDIRECT in AUDITED_ACTIONS
    seed_default_permissions(ac)
    ac.add_person("Хворостов Сергей", CHIEF, badge_id="B-3")
    s = ac.login_by_badge("B-3")
    ac.require(s, ACT_REWORK_REDIRECT, note="кромление → фрезерование")
    trail = ac.audit_trail(ACT_REWORK_REDIRECT)
    assert len(trail) == 1 and "фрезерование" in trail[0]["note"]


def test_sr99_оператор_переадресовать_не_может(ac):
    """D-216: иначе возврат кочует по цеху без следа."""
    seed_default_permissions(ac)
    ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-1")
    s = ac.login_by_badge("B-1")
    with pytest.raises(AccessDenied):
        ac.require(s, ACT_REWORK_REDIRECT)


# ----------------------------------------------------------------------
# SR-100. Код исполнителя, а не ФИО (находка CF-421)
# ----------------------------------------------------------------------
def test_sr100_код_непрозрачен_и_не_выводится_из_фио(ac):
    """
    Прежний `operator_id` был ТРАНСЛИТЕРАЦИЕЙ ФИО: `ivanov_pi` читается
    без таблицы соответствия, то есть кодом не является. Новый код не
    содержит ни одной части имени — ни кириллицей, ни латиницей.
    """
    p = ac.add_person("Иванов Пётр Сергеевич", OPERATOR, badge_id="B-1",
                      operator_id="ivanov_ps")
    code = p.person_code.lower()
    for part in ("иванов", "пётр", "сергеевич", "ivanov", "petr", "ps"):
        assert part not in code
    assert p.person_code.startswith("ИСП-")


def test_sr100_в_выгрузку_фио_не_попадает(ac):
    p = ac.add_person("Иванов Пётр Сергеевич", OPERATOR, badge_id="B-1")
    exp = p.to_export()
    assert exp["executor_code"] == p.person_code
    assert "Иванов" not in str(exp)
    assert "full_name" not in exp


def test_sr100_коды_порядковые_и_не_повторяются(ac):
    codes = [ac.add_person(f"Работник {i}", OPERATOR,
                           badge_id=f"B-{i}").person_code
             for i in range(1, 6)]
    assert codes == ["ИСП-0001", "ИСП-0002", "ИСП-0003",
                     "ИСП-0004", "ИСП-0005"]
    assert len(set(codes)) == 5


def test_sr100_код_не_выдаётся_повторно_после_удаления(ac, st):
    """
    Счёт по максимуму выданных, а не по числу строк: иначе после удаления
    исполнителя код достался бы другому человеку, и в старых отчётах один
    код означал бы двоих.
    """
    ac.add_person("Первый", OPERATOR, badge_id="B-1")
    ac.add_person("Второй", OPERATOR, badge_id="B-2")
    st._conn.execute("DELETE FROM persons WHERE person_code='ИСП-0002'")
    st._conn.commit()
    assert ac.add_person("Третий", OPERATOR, badge_id="B-3").person_code \
        == "ИСП-0003"


def test_sr100_связь_с_прежним_ключом_сохранена(ac):
    """
    `operator_id` смежного направления не удаляется: на него опирается
    накопленный факт. Он остаётся внутренним ключом, наружу идёт код.
    """
    p = ac.add_person("Иванов Пётр", OPERATOR, badge_id="B-1",
                      operator_id="ivanov_p")
    assert ac.get_person(p.person_code).operator_id == "ivanov_p"
    assert "operator_id" not in p.to_export()
