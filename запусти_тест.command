#!/bin/bash
# Разворачивает MES подетального учёта на этом Маке для теста с телефона.
# Двойной клик по файлу — Терминал откроется и всё сделает сам.

cd "$(dirname "$0")" || exit 1

echo "======================================================"
echo " Подетальный учёт — тестовый запуск на этом Маке"
echo "======================================================"
echo ""

echo "Проверяю Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "!! python3 не найден. Если всплыло окно про 'command line"
  echo "   developer tools' — нажми 'Установить', дождись и запусти снова."
  echo "Нажми Enter, чтобы закрыть."; read _; exit 1
fi
python3 --version

echo ""
echo "[1/4] Создаю окружение (.venv)... это может занять до минуты"
python3 -m venv .venv || { echo "!! не удалось создать venv"; read _; exit 1; }
source .venv/bin/activate

echo ""
echo "[2/4] Ставлю зависимости (Flask, openpyxl, cryptography)..."
echo "       идёт установка, подожди — вывод ниже:"
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "[3/4] База: участки + оператор + образец заказа 6564..."
python main.py --db prod.db init
python main.py --db prod.db add-operator --name "Тестовый Оператор" --id test_op
python main.py --db prod.db import samples/6564-Spectorg-OOO/*/.xbir
echo "   Операторы в базе:"
python main.py --db prod.db operators

echo ""
echo "[4/4] Определяю адрес в сети..."
IP=$(ipconfig getifaddr en0 2>/dev/null)
[ -z "$IP" ] && IP=$(ipconfig getifaddr en1 2>/dev/null)
[ -z "$IP" ] && IP="<IP-этого-Мака>"

echo ""
echo "======================================================"
echo "  НА ТЕЛЕФОНЕ (в той же Wi-Fi) открой в браузере:"
echo ""
echo "        https://$IP:5001/"
echo ""
echo "  Прими предупреждение о сертификате:"
echo "  «Дополнительно» -> «Всё равно перейти»."
echo "  Если Мак спросит про входящие соединения — «Разрешить»."
echo "======================================================"
echo ""
echo "  Оператор «Тестовый Оператор» -> участок «Кромление»"
echo "  -> операция «Облицовывание кромки 19/0,8» -> наведи"
echo "     камеру на тестовый QR (b5b3e59ba8) с экрана Мака."
echo ""
echo "  Остановить сервер: Ctrl+C в этом окне."
echo "======================================================"
echo ""

python src/operator_app.py prod.db 5001
