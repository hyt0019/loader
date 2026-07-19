@echo off
REM ============================================================
REM  Build the ORIGINAL program (packer.py) into a small, fast
REM  standalone FOLDER version.
REM  Output name: ContainerPacker_Original  (kept separate from
REM  the optimized build, so the two never overwrite each other)
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
echo [Step 3] Building the FOLDER version of packer.py (a few minutes)...
python -m PyInstaller --onedir --console --clean --noconfirm --name ContainerPacker_Original ^
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
    packer.py
if errorlevel 1 ( echo  ERROR: build failed, see messages above & pause & exit /b 1 )

call build_env\Scripts\deactivate.bat 2>nul

echo.
echo ============================================================
echo  DONE.  Original program folder:  dist\ContainerPacker_Original\
echo  Inside it is  ContainerPacker_Original.exe  (double-click to run).
echo.
echo  To deliver: put a data .txt (and the customer guide) inside that
echo  folder, ZIP the whole folder, and send the zip.
echo  Do NOT send only the .exe - it needs the files next to it.
echo ============================================================
echo.
pause
