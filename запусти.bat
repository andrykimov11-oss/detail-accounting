@echo off
cd /d "%~dp0"
title Podetal accounting - local launch

echo ==================================================
echo  Podetal accounting - local launch
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python not found. Install Python 3.12 from python.org
  echo     and check "Add Python to PATH". Then run again.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Detected Python %PYVER%
echo %PYVER%| findstr /b "3.12" >nul
if errorlevel 1 (
  echo [!] Offline package bundle is built for Python 3.12, found %PYVER%.
  echo     Install Python 3.12 from python.org ^(Add to PATH^), then run again.
  echo     ^(3.14 is too new - prebuilt packages are not available.^)
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual env .venv ...
  python -m venv .venv
)
set PY=.venv\Scripts\python.exe

echo [2/3] Installing dependencies ...
if exist "vendor\wheels" (
  echo    offline mode: from vendor\wheels ^(no internet needed^)
  "%PY%" -m pip install --no-index --find-links vendor\wheels Flask openpyxl cryptography
) else (
  echo    online mode: from PyPI
  "%PY%" -m pip install --timeout 120 --retries 10 -r requirements.txt
)
if errorlevel 1 (
  echo.
  echo [!] Dependency install failed - see messages above.
  echo     If online mode timed out: no internet to PyPI. Use the offline
  echo     bundle ^(folder vendor\wheels^) with Python 3.12.
  pause
  exit /b 1
)

echo [3/3] Seeding DB: areas + operator + sample order ...
"%PY%" main.py --db prod.db init
"%PY%" main.py --db prod.db add-operator --name "Test Operator" --id test_op
if exist "samples\6564-Spectorg-OOO" "%PY%" main.py --db prod.db import "samples\6564-Spectorg-OOO"
"%PY%" main.py --db prod.db operators

echo.
echo ==================================================
echo  Starting server. Addresses are printed below.
echo  Operator: /   Dashboard: /packing   Admin: /admin (PIN 0000)
echo  Open on phone (Android + Chrome), same Wi-Fi.
echo  Stop: close this window.
echo ==================================================
echo.

"%PY%" src\operator_app.py prod.db 5001
pause
