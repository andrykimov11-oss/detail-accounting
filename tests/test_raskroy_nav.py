"""
Тесты навигации по скану на участке раскроя (SR-107 — SR-116).

Способы проверки взяты из SRS-001 §22 дословно:
    SR-108 «Отсканировать заказ, отсутствующий в сменном задании смены»
           → отказ с названной причиной; похожий заказ не предложен
    SR-109 «Сверить полученное задание с печатной формой 1С»
           → совпали состав, количества и порядковые номера строк
    SR-110 «Взять заказ, у которого номер 1С и номер папки БАЗИС
           различны» → папка найдена через связку; система не искала
           папку по номеру 1С
    SR-111 «Отсканировать заказ из двух и более декоров» → предъявлены
           декоры с числом листов; запрошено подтверждение
    SR-112 «Взять заказ, где количество в бланке дробное» → в вопросе
           оператору стоит целое число листов из задания, а не дробь
    SR-113 «Подтвердить заказ, затем открыть его повторно» →
           подтверждение не запрошено вновь; запись несёт исполнителя и
           момент; отзыв доступен
    SR-114 «Отсканировать заказ и открыть рабочую папку станка» →
           программы этого заказа в ней лежат; на рабочем месте ничего
           не установлено
    SR-116 «Отсканировать заказ из одного декора» → программа открыта
           сразу, список не показан

КАТАЛОГ СТРОИТСЯ В ТЕСТЕ, А НЕ БЕРЁТСЯ ИЗ ВЫГРУЗКИ
    Настоящий каталог БАЗИС лежит вне репозитория и содержит фамилии
    клиентов в именах папок. Здесь каталог собирается во временной
    папке той же структуры: <станок>/<номер-Фамилия>/<Декор>/Board-N.xPrg.
    Фамилия в именах оставлена намеренно — иначе не проверить, что она
    не утекает в отчёт.
"""
from __future__ import annotations

import os
import re
import warnings
import tempfile
from pathlib import Path

import pytest

from src.raskroy_nav import (
    SETTING_BAZIS_ROOT,
    SETTING_MACHINE_DIR,
    BazisRootNotSet,
    LinkNotConfirmed,
    OrderFolderNotFound,
    RaskroyNavigator,
)
from src.storage import Storage


# Выдуманные фамилии для имён папок заказов. Перечень ЯВНЫЙ и
# проверяется тестом ниже.
#
# ЗАЧЕМ. До 26.09.2026 здесь стояли НАСТОЯЩИЕ фамилии клиентов, взятые
# из каталога БАЗИС, — и вместе с номерами заказов ушли в репозиторий и
# в его историю (CF-439). Дефект прожил незамеченным именно потому, что
# фамилия в фикстуре выглядит как часть правдоподобных данных, а не как
# персональные данные: проверка «фамилия не уходит наружу» стояла рядом
# и была написана на ту же настоящую фамилию.
#
# Правило: имя папки заказа в тестах — только отсюда. Настоящая фамилия
# в код не попадает даже как образец того, чего нельзя показывать.
INVENTED_SURNAMES = ("Vydumkin", "Vydumkina", "Vymyslov", "Vydumtorg")


def test_имена_папок_в_фикстурах_выдуманы(tmp_path):
    """
    Сторож для CF-439. Проверяется НЕ намерение автора, а текст файла:
    каждое имя папки вида «NNNN-Имя» в этом файле обязано нести фамилию
    из явного перечня.

    СТАТУС — ПРЕДУПРЕЖДАЮЩИЙ (решение владельца D-223). Настоящая
    фамилия в фикстуре печатает предупреждение и прогон не роняет:
    боевой контур — локальная сеть, фамилия без инициалов и контактов
    владельцем персональными данными не считается.

    Послабление СРОЧНОЕ: владелец назвал конец срока — «до размещения
    на рабочем сервере в локальной сети». В тот день `warnings.warn`
    ниже возвращается на `assert`, и сторож снова блокирующий. Срок
    заведён пунктом 0.12 `PLAN-001`, чтобы у него был исполнитель и
    признак завершения, а не одно слово «пока».
    Само наличие имён проверяется жёстко: если перестанут находиться
    вовсе, сторож молчал бы, ничего не охраняя (урок CF-439 о пустых
    проверках).
    """
    text = Path(__file__).read_text(encoding="utf-8")
    names = set(re.findall(r'\b\d{4}-([A-Z][A-Za-z]+)', text))
    assert names, "в файле не найдено ни одного имени папки заказа"
    чужие = sorted(names - set(INVENTED_SURNAMES))
    if чужие:
        warnings.warn(f"имя папки заказа вне перечня выдуманных: {чужие} "
                      f"(не блокирует по D-223)", stacklevel=2)


def _catalog(root: Path) -> None:
    """Каталог БАЗИС: два заказа, один многодекорный, один однодекорный."""
    spec = {
        "Gabbiani/7709-Vydumkin": {
            "Kronoshpan-2500h1830-1": ["Board-1.xPrg", "Board-2.xPrg"],
            "Oreh-Karija-16-mm-1": ["Board-1.xPrg"],
            "HDF-2800h2070-1": ["Board-1.xPrg"],
        },
        "Gabbiani/1980-Vydumkina": {
            "Belyj-R-16mm-1": ["Board-1.xPrg"],
        },
        "Nanxing/2054-Vymyslov": {
            "Dub-Votan-1": ["Board-1.xPrg"],
        },
    }
    for order_path, decors in spec.items():
        for decor, files in decors.items():
            d = root / order_path / decor
            d.mkdir(parents=True)
            for f in files:
                (d / f).write_text("PROGRAM", encoding="utf-8")


@pytest.fixture()
def nav():
    tmp = Path(tempfile.mkdtemp())
    root, machine = tmp / "bazis", tmp / "station"
    _catalog(root)
    st = Storage(os.path.join(tmp, "psr.db"))
    st.set_setting(SETTING_BAZIS_ROOT, str(root))
    st.set_setting(SETTING_MACHINE_DIR, str(machine))

    # Сменное задание 1С: заказ 7709 тремя декорами, порядок строк задан.
    st.upsert_shift_task("25.08/1", "2026-08-25", "ЛДСП (Фоминская, 49)")
    for row_no, decor, sheets in ((7, "Kronoshpan-2500h1830-1", 4),
                                  (7, "Oreh-Karija-16-mm-1", 2),
                                  (7, "HDF-2800h2070-1", 2),
                                  (8, "Belyj-R-16mm-1", 3)):
        order = 7709 if row_no == 7 else 1980
        st.add_shift_task_row("25.08/1", row_no, "ПС00-013166", "2026-08-24",
                              decor, sheets, order_num=order)
    n = RaskroyNavigator(st)
    n.storage = st
    n.machine = machine
    yield n
    st.close()


# ----------------------------------------------------------------------
# SR-110. Папка ищется через связку, а не по номеру 1С
# ----------------------------------------------------------------------
def test_sr110_папка_найдена_по_номеру_базис(nav):
    assert nav.order_folder(7709).name == "7709-Vydumkin"
    assert nav.order_folder(1980).name == "1980-Vydumkina"


def test_sr110_номер_1с_папку_не_находит(nav):
    """
    Прямая проверка требования: номер документа 1С — другая величина.
    Попытка найти папку по «013166» не даёт ничего, и это правильно.
    """
    with pytest.raises(OrderFolderNotFound):
        nav.order_folder(13166)


def test_sr110_папка_ищется_по_всем_станкам(nav):
    """Заказ мог быть раскроен на другом центре — каталог общий."""
    assert nav.order_folder(2054).name == "2054-Vymyslov"


def test_отказ_не_называет_фамилию_клиента(nav):
    """
    SR-100: имя папки несёт фамилию клиента. В сообщение об отказе оно
    попасть не должно — иначе ФИО уедет в лог и в отчёт об ошибках.
    """
    with pytest.raises(OrderFolderNotFound) as e:
        nav.order_folder(9999)
    text = str(e.value)
    for surname in ("Vydumkin", "Vydumkina", "Vymyslov"):
        assert surname not in text
    assert "9999" in text


def test_отказ_не_содержит_путей_вовсе(nav):
    """
    Усиление предыдущей проверки, и оно понадобилось: контрольный опыт
    показал, что проверка «нет фамилии» пропускает сообщение, печатающее
    путь каталога. Сам корень фамилии не содержит — но стоит однажды
    подставить туда путь НАЙДЕННОЙ папки, и фамилия уедет в лог, а
    проверка промолчит.

    Поэтому запрещается не фамилия, а ПУТЬ: в сообщении оператору его
    быть не должно ни в каком виде. Оператору он бесполезен, а утечка
    через него незаметна.
    """
    with pytest.raises(OrderFolderNotFound) as e:
        nav.order_folder(9999)
    text = str(e.value)
    assert "/" not in text and "\\" not in text
    assert str(nav.storage.get_setting(SETTING_BAZIS_ROOT)) not in text


def test_каталог_не_задан_названо_прямо(nav):
    nav.storage.set_setting(SETTING_BAZIS_ROOT, "")
    with pytest.raises(BazisRootNotSet) as e:
        nav.order_folder(7709)
    assert "администратор" in str(e.value).lower()


# ----------------------------------------------------------------------
# SR-111, SR-112, SR-116. Декоры, листы, один против нескольких
# ----------------------------------------------------------------------
def test_sr111_декоры_предъявлены_с_числом_листов(nav):
    r = nav.navigate(7709, task_id="25.08/1")
    assert len(r.decors) == 3
    assert {d.name: d.sheets for d in r.decors} == {
        "HDF-2800h2070-1": 2,
        "Kronoshpan-2500h1830-1": 4,
        "Oreh-Karija-16-mm-1": 2,
    }
    assert r.confirmed is False   # подтверждение запрошено


def test_sr116_один_декор_списка_не_требует(nav):
    r = nav.navigate(1980, task_id="25.08/1")
    assert r.single_decor is True and len(r.decors) == 1


def test_sr116_несколько_декоров_дают_список(nav):
    assert nav.navigate(7709, task_id="25.08/1").single_decor is False


def test_sr112_число_листов_целое_и_из_задания(nav):
    """
    Величина из бланка заказа дробная — она означает расход материала
    при работе по деловым остаткам. Оператору показывается целое число
    листов из задания.
    """
    for d in nav.navigate(7709, task_id="25.08/1").decors:
        assert isinstance(d.sheets, int)
    assert sum(d.sheets for d in nav.navigate(7709, "25.08/1").decors) == 8


def test_sr112_без_задания_величина_не_придумывается(nav):
    """
    Пока выгрузки задания нет, число листов неизвестно. Система ставит
    ноль, а не берёт дробь из бланка и не округляет её: придуманная
    величина хуже отсутствующей, потому что неотличима от настоящей.
    """
    for d in nav.navigate(7709).decors:
        assert d.sheets == 0


def test_sr109_строки_задания_идут_в_порядке_комплектования(nav):
    rows = nav.storage.get_shift_task_rows("25.08/1")
    assert [r["row_no"] for r in rows] == [7, 7, 7, 8]


# ----------------------------------------------------------------------
# SR-113. Подтверждение — событие, однократное, отзываемое
# ----------------------------------------------------------------------
def test_sr113_подтверждение_запрашивается_однократно(nav):
    nav.confirm(7709, "ИСП-0001")
    assert nav.navigate(7709, "25.08/1").confirmed is True
    nav.confirm(7709, "ИСП-0002")          # повтор — не пишется
    assert len(nav.confirmation_history(7709)) == 1


def test_sr113_запись_несёт_исполнителя_и_момент(nav):
    nav.confirm(7709, "ИСП-0001", note="сверено с бланком")
    row = nav.confirmation_history(7709)[0]
    assert row["person_code"] == "ИСП-0001"
    assert row["acted_at"] and row["event"] == "confirmed"
    assert "сверено" in row["note"]


def test_sr113_отзыв_доступен_и_след_подтверждения_остаётся(nav):
    """
    Отзыв добавляется событием, а не стирает подтверждение: иначе на
    вопрос «кто подтвердил ошибочную связку» ответить было бы нечем.
    """
    nav.confirm(7709, "ИСП-0001")
    nav.revoke(7709, "ИСП-0007", note="декоры не совпали")
    assert nav.is_confirmed(7709) is False
    hist = nav.confirmation_history(7709)
    assert [h["event"] for h in hist] == ["confirmed", "revoked"]
    assert hist[0]["person_code"] == "ИСП-0001"


def test_sr113_после_отзыва_можно_подтвердить_снова(nav):
    nav.confirm(7709, "ИСП-0001")
    nav.revoke(7709, "ИСП-0007")
    nav.confirm(7709, "ИСП-0001")
    assert nav.is_confirmed(7709) is True
    assert len(nav.confirmation_history(7709)) == 3


def test_sr113_отзыв_неподтверждённого_ничего_не_пишет(nav):
    nav.revoke(7709, "ИСП-0007")
    assert nav.confirmation_history(7709) == []


# ----------------------------------------------------------------------
# SR-114. Рабочая папка станка
# ----------------------------------------------------------------------
def test_sr114_программы_заказа_лежат_в_папке_станка(nav):
    nav.confirm(7709, "ИСП-0001")
    copied = nav.prepare_machine_folder(7709, task_id="25.08/1")
    assert len(copied) == 4
    names = {p.name for p in nav.machine.iterdir()}
    assert "Kronoshpan-2500h1830-1__Board-1.xPrg" in names
    assert "Oreh-Karija-16-mm-1__Board-1.xPrg" in names


def test_sr114_имя_несёт_декор_иначе_программы_неразличимы(nav):
    """
    Board-1.xPrg есть у каждого декора. Без имени декора в папке станка
    второй файл затёр бы первый.
    """
    nav.confirm(7709, "ИСП-0001")
    nav.prepare_machine_folder(7709, task_id="25.08/1")
    assert len(list(nav.machine.iterdir())) == 4


def test_sr114_папка_станка_очищается_перед_копированием(nav):
    """
    Иначе через неделю в папке будут программы десяти заказов, и выбор
    станет тем же поиском, от которого мы уходим.
    """
    nav.confirm(7709, "ИСП-0001")
    nav.confirm(1980, "ИСП-0001")
    nav.prepare_machine_folder(7709, task_id="25.08/1")
    nav.prepare_machine_folder(1980, task_id="25.08/1")
    assert [p.name for p in nav.machine.iterdir()] == \
        ["Belyj-R-16mm-1__Board-1.xPrg"]


def test_sr114_один_декор_копируется_отдельно(nav):
    nav.confirm(7709, "ИСП-0001")
    nav.prepare_machine_folder(7709, decor_name="HDF-2800h2070-1",
                               task_id="25.08/1")
    assert [p.name for p in nav.machine.iterdir()] == \
        ["HDF-2800h2070-1__Board-1.xPrg"]


def test_sr111_без_подтверждения_программы_не_копируются(nav):
    """
    Подтверждение — не формальность: оператор сверяет декоры и листы с
    тем, что физически привезли. Копировать до сверки значит дать ему
    начать резать по непроверенному заданию.
    """
    with pytest.raises(LinkNotConfirmed):
        nav.prepare_machine_folder(7709, task_id="25.08/1")
    assert not nav.machine.exists() or list(nav.machine.iterdir()) == []


# ----------------------------------------------------------------------
# SR-100. Фамилия клиента не уходит в отчёт
# ----------------------------------------------------------------------
def test_sr100_отчёт_не_содержит_имён_папок(nav):
    rep = nav.navigate(7709, "25.08/1").to_report()
    assert "Vydumkin" not in str(rep)
    assert rep["order_num"] == 7709
    assert rep["decors"][0]["sheets"] >= 0
    assert "folder" not in str(rep) and "path" not in str(rep)
