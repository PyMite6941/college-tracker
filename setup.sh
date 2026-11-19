#!/usr/bin/env bash
# College Tracker setup (macOS / Linux)
set -euo pipefail

echo "[1/4] Checking uv..."
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found. Install from https://github.com/astral-sh/uv"; exit 1; }

echo "[2/4] Creating virtual environment (.venv)..."
uv venv

echo "[3/4] Installing dependencies..."
uv pip install -r requirements.txt --python .venv

echo "[4/4] Installing Playwright browsers..."
.venv/bin/python -m playwright install chromium

if [ ! -f .env ]; then
    echo "Creating .env from .env.example (fill in your credentials before running)..."
    cp .env.example .env
else
    echo ".env already exists, leaving it untouched."
fi

echo
echo "Setup complete. Run:  .venv/bin/python run.py"
echo "Then open:            http://127.0.0.1:5000"
