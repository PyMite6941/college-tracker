#!/usr/bin/env bash
# College Tracker - run the website on localhost (macOS / Linux)
set -euo pipefail

if [ ! -f .venv/bin/python ]; then
    echo "ERROR: .venv not found. Run setup.sh first."
    exit 1
fi

echo "Starting College Tracker on http://127.0.0.1:5000"
.venv/bin/python run.py
