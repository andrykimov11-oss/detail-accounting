@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Подетальный учёт — локальный запуск

echo ======================================================
echo  Подетальный учёт — локальный запуск (Windows)
echo ======================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден. Установите Python 3.10+ с python.org
  echo     и на установке отметьте "Add Python to PATH". Затем запустите снова.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Создаю окружение .venv ...
  python -m venv .venv
)
set PY=.venv\Scripts\python.exe

echo [2/4] Ставлю зависимости ...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt

echo [3/4] База: участки + оператор + образец заказа ...
"%PY%" main.py --db prod.db init
"%PY%" main.py --db prod.db add-operator --name "Тестовый Оператор" --id test_op
if exist "samples\6564-Spectorg-OOO" (
  "%PY%" main.py --db prod.db import "samples\6564-Spectorg-OOO"
)
"%PY%" main.py --db prod.db operators

echo.
echo [4/4] Запускаю сервер. Адреса напечатаны ниже.
echo    Реальные данные: в админке (/admin, PIN 0000) укажите локальные пути
echo    к папке с .xbir и файлу "производственные операции" и нажмите
echo    "Связать и импортировать". FTP локально не нужен.
echo    Остановить сервер: закрыть это окно.
echo.

"%PY%" src\operator_app.py prod.db 5001
pause
