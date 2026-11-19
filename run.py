#!/usr/bin/env python3
"""Entrypoint: run the College Tracker website on localhost.

Reads HOST / PORT / FLASK_DEBUG from .env (loaded by app.py).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from app import app

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1",
    )
