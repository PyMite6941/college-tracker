@echo off
:: College Tracker - run the website on localhost (Windows)
setlocal

if not exist .venv\Scripts\python.exe (
    echo ERROR: .venv not found. Run setup.bat first.
    exit /b 1
)

echo Starting College Tracker on http://127.0.0.1:5000
.venv\Scripts\python run.py
