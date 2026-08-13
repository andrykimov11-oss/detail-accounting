#!/bin/bash
# Обновление приложения на сервере: подтянуть код, зависимости, перезапустить.
# Запуск:  bash deploy/update.sh   (из корня репозитория)
set -e
cd "$(dirname "$0")/.."

echo "== git pull =="
git pull --ff-only

echo "== зависимости =="
.venv/bin/pip install -q -r requirements.txt

echo "== тесты (быстрая проверка) =="
.venv/bin/python -m pytest -q || { echo "!! тесты упали — не перезапускаю"; exit 1; }

echo "== перезапуск службы =="
sudo systemctl restart psr
echo "готово: $(systemctl is-active psr)"
