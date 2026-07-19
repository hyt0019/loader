@echo off
REM ============================================================
REM  Container Packer - build a SMALL, FAST standalone program
REM  Strategy:
REM    * build inside a CLEAN virtual env (only needed libs) -> small
REM    * build as a FOLDER (onedir) instead of one file       -> fast start
REM  Run this ON A WINDOWS PC that has Python installed.
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo [Step 0] Checking Python...
python --version
if errorlevel 1 (
    echo  ERROR: Python not found. Install from https://www.python.org/downloads/
    echo  and tick "Add python.exe to PATH", then rerun this file.
    pause
    exit /b 1
)

echo.
echo [Step 1] Creating a clean build environment (build_env)...
if exist build_env rmdir /s /q build_env
python -m venv build_env
if errorlevel 1 ( echo  ERROR: could not create venv & pause & exit /b 1 )
call build_env\Scripts\activate.bat

echo.
echo [Step 2] Installing ONLY the needed libraries into the clean env...
python -m pip install --upgrade pip
python -m pip install pyinstaller matplotlib numpy openpyxl
if errorlevel 1 ( echo  ERROR: pip install failed (check internet) & pause & exit /b 1 )

echo.
echo [Step 3] Building the FOLDER version (this can take a few minutes)...
python -m PyInstaller --onedir --console --clean --noconfirm --name ContainerPacker ^
    --hidden-import tkinter ^
    --hidden-import matplotlib.backends.backend_tkagg ^
    --hidden-import mpl_toolkits.mplot3d ^
    --collect-all openpyxl ^
    --exclude-module PyQt5 --exclude-module PyQt6 ^
    --exclude-module PySide2 --exclude-module PySide6 ^
    --exclude-module scipy --exclude-module pandas ^
    --exclude-module IPython --exclude-module jupyter ^
    --exclude-module notebook --exclude-module tornado ^
    --exclude-module wx --exclude-module pytest --exclude-module sphinx ^
    packer_optimized.py
if errorlevel 1 ( echo  ERROR: build failed, see messages above & pause & exit /b 1 )

call build_env\Scripts\deactivate.bat 2>nul

echo.
echo ============================================================
echo  DONE.  Your program is the FOLDER:   dist\ContainerPacker\
echo  Inside it is  ContainerPacker.exe  (double-click to run).
echo.
echo  To deliver to the customer:
echo    1) ZIP the whole  dist\ContainerPacker  folder
echo    2) also put a data .txt and the customer guide inside it
echo    3) send the zip; customer unzips and runs ContainerPacker.exe
echo.
echo  Do NOT send only the .exe - it needs the files next to it.
echo ============================================================
echo.
pause
