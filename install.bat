@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing FunPay & Playerok Castor...
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo Python not found. Install from https://www.python.org/downloads/ and tick "Add python.exe to PATH" & pause & exit /b 1)
if not exist venv %PY% -m venv venv
venv\Scripts\python -m pip install -U pip
venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Done. Run start.bat
pause
