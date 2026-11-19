"""
College tracker website.

Localhost Flask app. Shows tracked college sites and the latest
registration-checklist data gathered by scraper.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, render_template_string, request, url_for

from scraper import run_scrape, RESULTS_FILE, load_sites

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

HOME_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>College Tracker</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 16px; color: #111; }
  h1 { font-size: 1.5rem; margin-bottom: 8px; }
  .muted { color: #666; font-size: 0.9rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
  th { background: #f5f5f5; }
  .ok { color: #0a7; } .error { color: #a33; } .pending { color: #999; }
  button { background: #0a7; color: #fff; border: none; padding: 8px 14px;
           border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
  button:hover { background: #085; }
  button.secondary { background: #666; margin-left: 8px; }
  button.secondary:hover { background: #444; }
  .notice { background: #fff7e6; border: 1px solid #e3d29b; padding: 10px 14px;
            border-radius: 6px; margin-top: 16px; font-size: 0.9rem; }
  pre { background: #f5f5f5; padding: 10px 14px; border-radius: 6px;
        overflow: auto; font-size: 0.85rem; margin-top: 8px; }
</style>
</head>
<body>
<h1>College Tracker</h1>
<p class="muted">Localhost dashboard &bull; registration-checklist data via Playwright</p>

<form method="post" action="{{ url_for('scan') }}" style="margin: 16px 0;">
  <button type="submit">Run check now</button>
  <button type="button" class="secondary" onclick="location.href='{{ url_for('sites') }}'">Manage sites</button>
  <span class="muted">Headless: {{ headless }}</span>
</form>

{% if results %}
<h2>Latest results ({{ results | length }} site{{ 's' if results|length != 1 else '' }})</h2>
<table>
  <thead><tr><th>Site</th><th>Status</th><th>Checked at</th><th>Page title</th></tr></thead>
  <tbody>
  {% for r in results %}
  <tr>
    <td>{{ r.name }}</td>
    <td class="{{ r.status }}">{{ r.status }}</td>
    <td>{{ r.checked_at or '-' }}</td>
    <td>{{ r.title or '-' }}</td>
  </tr>
  {% if r.error %}
  <tr><td colspan="4"><b>Error:</b> {{ r.error }}</td></tr>
  {% endif %}
  {% if r.excerpt %}
  <tr><td colspan="4"><pre>{{ r.excerpt }}</pre></td></tr>
  {% endif %}
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="notice">No results yet. Click "Run check now" to log into the configured sites
and pull their registration checklists.</p>
{% endif %}

<h2>Configured sites</h2>
<ul>
{% for s in sites %}
<li><b>{{ s.name }}</b> ({{ s.id }}) &mdash; <code>{{ s.checklist_url }}</code></li>
{% endfor %}
</ul>
</body>
</html>
"""

SITES_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sites - College Tracker</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 700px; margin: 40px auto; padding: 0 16px; color: #111; }
  a.back { color: #0a7; text-decoration: none; }
  a.back:hover { text-decoration: underline; }
  pre { background: #f5f5f5; padding: 12px 16px; border-radius: 6px;
        overflow: auto; font-size: 0.85rem; white-space: pre-wrap; }
  .notice { background: #fff7e6; border: 1px solid #e3d29b; padding: 10px 14px;
            border-radius: 6px; margin-top: 16px; font-size: 0.9rem; }
</style>
</head>
<body>
<h1>Sites</h1>
<p><a class="back" href="{{ url_for('index') }}">&larr; back to dashboard</a></p>
<pre>{{ sites_json }}</pre>
<div class="notice">
Edit <code>sites.json</code> to add or change the websites to log into and check.
Credentials live in <code>.env</code> (gitignored). Restart the app after editing.
</div>
</body>
</html>
"""


@app.get("/")
def index():
    results = []
    if RESULTS_FILE.exists():
        try:
            results = json_load(RESULTS_FILE)
        except Exception:
            results = []
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
    return render_template_string(
        HOME_TEMPLATE, results=results, sites=load_sites(), headless=headless
    )


@app.post("/scan")
def scan():
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
    run_scrape(headless=headless)
    return redirect(url_for("index"))


@app.get("/sites")
def sites():
    return render_template_string(SITES_TEMPLATE, sites_json=json.dumps(load_sites(), indent=2))


def json_load(path):
    import json

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1",
    )
