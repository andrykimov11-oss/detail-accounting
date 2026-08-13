#!/bin/bash
# Засеять ТЕСТОВЫЕ данные на сервере: участки + тестовый оператор + заказ 6564
# (образец лежит в репозитории). Для проверки psr.rascroi.ru до реальных данных.
# Запуск на сервере из корня репозитория:  bash deploy/seed_test.sh
set -e
cd "$(dirname "$0")/.."

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "== участки =="
$PY main.py --db prod.db init

echo "== тестовый оператор =="
$PY main.py --db prod.db add-operator --name "Тестовый Оператор" --id test_op

echo "== образец заказа 6564 (детали для сканирования) =="
$PY main.py --db prod.db import samples/6564-Spectorg-OOO/*/.xbir

echo "== операторы в базе =="
$PY main.py --db prod.db operators

echo ""
echo "Готово. Открой https://psr.rascroi.ru"
echo "Оператор «Тестовый Оператор» → участок «Упаковка» → «Упаковка раскроя»"
echo "  (контроль комплектности, план = все детали заказа)"
echo "или участок «Кромление» → «Облицовывание кромки 19/0,8»."
