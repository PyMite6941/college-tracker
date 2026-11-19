"""
College tracker website.

Localhost Flask app. Shows tracked college sites and the latest
registration-checklist data gathered by scraper.py. Also lets you save
the websites the bots scrape through the web UI (sites.json is updated
on disk).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    redirect,
    render_template_string,
    request,
    url_for,
)

from scraper import RESULTS_FILE, load_sites, run_scrape, save_sites

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
  button.danger { background: #a33; }
  button.danger:hover { background: #822; }
  .notice { background: #fff7e6; border: 1px solid #e3d29b; padding: 10px 14px;
            border-radius: 6px; margin-top: 16px; font-size: 0.9rem; }
  pre { background: #f5f5f5; padding: 10px 14px; border-radius: 6px;
        overflow: auto; font-size: 0.85rem; margin-top: 8px; }
  form.add-site { margin-top: 20px; padding: 14px 16px; background: #fafafa;
                  border: 1px solid #ddd; border-radius: 8px; }
  form.add-site input { padding: 6px 10px; margin-right: 8px; font-size: 0.9rem; }
  form.add-site button { padding: 6px 12px; font-size: 0.9rem; }
  .flash { background: #e7f3ff; border: 1px solid #bcd4f2; padding: 10px 14px;
           border-radius: 6px; margin-top: 10px; font-size: 0.9rem; }
</style>
</head>
<body>
<h1>College Tracker</h1>
<p class="muted">Localhost dashboard &bull; registration-checklist data via Playwright</p>

{% if flash %}
<div class="flash">{{ flash }}</div>
{% endif %}

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

<form class="add-site" method="post" action="{{ url_for('add_site') }}">
  <h3 style="margin-top:0;font-size:1.1rem;">Add a website to track</h3>
  <input name="id" placeholder="site id (e.g. registrar)" required>
  <input name="name" placeholder="display name" required>
  <input name="login_url" placeholder="login URL" required>
  <input name="checklist_url" placeholder="registration-checklist URL" required>
  <input name="user_env" placeholder="username env var (e.g. COLLEGE_USER)" required>
  <input name="pass_env" placeholder="password env var (e.g. COLLEGE_PASS)" required>
  <button type="submit">Save site</button>
  <a href="{{ url_for('index') }}" class="secondary" style="text-decoration:none;color:#666;margin-left:8px;">Cancel</a>
</form>
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
         max-width: 800px; margin: 40px auto; padding: 0 16px; color: #111; }
  a.back { color: #0a7; text-decoration: none; }
  a.back:hover { text-decoration: underline; }
  .notice { background: #fff7e6; border: 1px solid #e3d29b; padding: 10px 14px;
            border-radius: 6px; margin-top: 16px; font-size: 0.9rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
  th { background: #f5f5f5; }
  form.del { display: inline; }
  button.danger { background: #a33; color: #fff; border: none; padding: 4px 10px;
                  border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button.danger:hover { background: #822; }
</style>
</head>
<body>
<h1>Sites</h1>
<p><a class="back" href="{{ url_for('index') }}">&larr; back to dashboard</a></p>
<p class="muted">These websites are saved to <code>sites.json</code> and scraped by the bots.</p>

<table>
  <thead><tr><th>Name</th><th>ID</th><th>Checklist URL</th><th>User env</th><th>Action</th></tr></thead>
  <tbody>
  {% for s in sites %}
  <tr>
    <td>{{ s.name }}</td>
    <td><code>{{ s.id }}</code></td>
    <td><code>{{ s.checklist_url }}</code></td>
    <td>{{ s.user_env }}</td>
    <td>
      <form class="del" method="post" action="{{ url_for('delete_site', site_id=s.id) }}"
            onsubmit="return confirm('Delete {{ s.name }}?')">
        <button type="submit" class="danger">Delete</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>

{% if not sites %}
<p class="notice">No sites configured yet. Add one from the dashboard.</p>
{% endif %}

<div class="notice">
Add or remove sites above. Changes are saved to <code>sites.json</code> immediately.
Credentials (username/password) still live in <code>.env</code> (gitignored).
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
    flash = request.args.get("flash")
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
    return render_template_string(
        HOME_TEMPLATE,
        results=results,
        sites=load_sites(),
        headless=headless,
        flash=flash,
    )


@app.post("/scan")
def scan():
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
    run_scrape(headless=headless)
    return redirect(url_for("index", flash="Scan complete."))


@app.get("/sites")
def sites():
    return render_template_string(
        SITES_TEMPLATE, sites=load_sites()
    )


@app.post("/sites/add")
@app.post("/sites/add/")
def add_site():
    site_id = (request.form.get("id") or "").strip()
    name = (request.form.get("name") or "").strip()
    login_url = (request.form.get("login_url") or "").strip()
    checklist_url = (request.form.get("checklist_url") or "").strip()
    user_env = (request.form.get("user_env") or "").strip()
    pass_env = (request.form.get("pass_env") or "").strip()

    if not all([site_id, name, login_url, checklist_url, user_env, pass_env]):
        return redirect(url_for("index", flash="All fields are required."))

    sites = load_sites()
    if any(s.get("id") == site_id for s in sites):
        return redirect(url_for("index", flash=f"Site id '{site_id}' already exists."))

    new_site = {
        "id": site_id,
        "name": name,
        "login_url": login_url,
        "checklist_url": checklist_url,
        "user_env": user_env,
        "pass_env": pass_env,
        "selectors": {
            "username": 'input[type="email"], input[name="username"], input[name="email"]',
            "password": 'input[type="password"]',
            "submit": 'button[type="submit"], input[type="submit"]',
        },
        "checklist_selectors": ["main", "[role=\"main\"]", ".checklist", ".registration-checklist", "body"],
    }
    sites.append(new_site)
    save_sites(sites)
    return redirect(url_for("index", flash=f"Saved site '{name}'."))


@app.post("/sites/delete/<site_id>")
def delete_site(site_id):
    sites = load_sites()
    filtered = [s for s in sites if s.get("id") != site_id]
    if len(filtered) == len(sites):
        return redirect(url_for("sites", flash=f"Site '{site_id}' not found."))
    save_sites(filtered)
    return redirect(url_for("sites", flash=f"Deleted site '{site_id}'."))


def json_load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1",
    )
