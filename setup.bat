@echo off
:: College Tracker setup (Windows)
setlocal enabledelayedexpansion

echo [1/4] Checking uv...
where uv >nul 2>&1 || (echo ERROR: uv not found. Install from https://github.com/astral-sh/uv & exit /b 1)

echo [2/4] Creating virtual environment (.venv)...
uv venv

echo [3/4] Installing dependencies...
.venv\Scripts\uv pip install -r requirements.txt --python .venv

echo [4/4] Installing Playwright browsers...
.venv\Scripts\python -m playwright install chromium

if not exist .env (
    echo Creating .env from .env.example (fill in your credentials before running)...
    copy .env.example .env
) else (
    echo .env already exists, leaving it untouched.
)

echo.
echo Setup complete. Run:  .venv\Scripts\python run.py
echo Then open:            http://127.0.0.1:5000
