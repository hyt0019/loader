@echo off
REM ============================================================
REM  Run the Container Packer WEB APP locally (Windows).
REM  First run installs everything into web_env (a few minutes).
REM  Then a browser opens at http://localhost:8501
REM ============================================================
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install from https://www.python.org/downloads/
    echo  and tick "Add python.exe to PATH", then rerun this file.
    pause
    exit /b 1
)

if not exist web_env (
    echo [setup] Creating environment web_env ...
    python -m venv web_env
)
call web_env\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements_web.txt
if errorlevel 1 ( echo  ERROR: install failed (check internet) & pause & exit /b 1 )

echo.
echo [run] Starting web app. Keep this window open while using it.
echo       Your browser should open automatically at http://localhost:8501
echo.
streamlit run app.py
pause
