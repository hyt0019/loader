#!/bin/bash
# ============================================================
#  Run the Container Packer WEB APP locally (macOS).
#  Double-click this file (or: bash run_web_app.command).
#  First run installs everything into web_env (a few minutes).
#  Then a browser opens at http://localhost:8501
# ============================================================
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install from https://www.python.org/downloads/macos/"
    read -r -p "Press Enter to close..." _
    exit 1
fi

if [ ! -d web_env ]; then
    echo "[setup] Creating environment web_env ..."
    python3 -m venv web_env
fi
# shellcheck disable=SC1091
source web_env/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements_web.txt || { echo "install failed"; read -r -p "Enter..." _; exit 1; }

echo ""
echo "[run] Starting web app. Keep this window open while using it."
echo "      Browser opens at http://localhost:8501"
echo ""
streamlit run app.py
