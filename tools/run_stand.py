#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Запуск тестового стенда ПСР одной командой.

ЗАЧЕМ ОТДЕЛЬНЫЙ ЗАПУСКАТЕЛЬ, А НЕ `flask --app src.operator_app run`

    Потому что так — не работает, и это проверено, а не предположено.
    `operator_app.create_app(db_path="prod.db")` берёт путь к базе
    АРГУМЕНТОМ со значением по умолчанию и переменную окружения не
    читает. Команда `flask --app src.operator_app run` вызывает фабрику
    без аргументов, приложение открывает `prod.db` в текущей папке, и
    каждый запрос падает с «disk I/O error» либо тихо работает не с той
    базой. Первая редакция инструкции стенда советовала именно это
    (CF-428).

    `operator_app.py` принадлежит смежному направлению, и по контракту
    интеграции (п. 7) чужой app-слой не правится. Поэтому здесь свой
    запускатель: он поднимает ТОЛЬКО рабочие места ПСР, которые и нужны
    на стенде шага 1.

ЧТО ПОДНИМАЕТСЯ
    /psr/operator?area=<участок>   рабочее место оператора участка
    /psr/chief                     экран начальника цеха

Запуск:
    python3 tools/run_stand.py --root ./teststand
    python3 tools/run_stand.py --root ./teststand --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flask import Flask, redirect  # noqa: E402

from psr_app import AREA_NAMES, psr  # noqa: E402


def build_app(db_path: Path) -> Flask:
    """
    Приложение стенда: те же рабочие места, что в бою.

    Соединение с базой открывается на каждый запрос по `DB_PATH` —
    соглашение ядра; здесь оно не нарушается, иначе стенд вёл бы себя
    иначе, чем боевая система, и замечания собирались бы не о ней.
    """
    app = Flask(__name__,
                template_folder=str(ROOT / "src" / "templates"),
                static_folder=str(ROOT / "src" / "static"))
    app.config["DB_PATH"] = str(db_path)
    app.register_blueprint(psr)

    @app.get("/")
    def index():
        return redirect("/psr/chief")

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="./teststand",
                    help="папка стенда, собранная make_test_stand.py")
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 — чтобы открывалось с планшета в сети")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    db = Path(args.root).resolve() / "psr.db"
    if not db.exists():
        print(f"Базы стенда нет: {db}")
        print("Соберите стенд:  python3 tools/make_test_stand.py "
              f"--root {args.root}")
        return 1

    print(f"База стенда: {db}")
    print(f"Открывать:   http://{args.host}:{args.port}/psr/chief")
    for area, name in AREA_NAMES.items():
        print(f"             http://{args.host}:{args.port}"
              f"/psr/operator?area={area}   — {name}")
    print()
    build_app(db).run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
