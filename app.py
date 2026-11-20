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

from scraper import (
    RESULTS_FILE,
    detect_chrome_profiles,
    import_chrome_passwords_csv,
    is_chrome_running,
    load_credentials,
    load_sites,
    run_scrape,
    save_credentials,
    save_sites,
)

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
  button.secondary:hover { background: #444; }
  button.save { background: #2a7; }
  button.save:hover { background: #1a5; }
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
<li><b>{{ s.name }}</b> ({{ s.id }}) &mdash; <code>{{ s.checklist_url }}</code>
  <span class="{% if s.creds_saved %}cred-saved{% else %}cred-missing{% endif %}">
    {% if s.creds_saved %}[credentials saved]{% else %}[no credentials]{% endif %}
  </span></li>
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
  button.save { background: #2a7; color: #fff; border: none; padding: 4px 10px;
                border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button.save:hover { background: #1a5; }
  .cred-saved { color: #2a7; font-weight: bold; }
  .cred-missing { color: #a33; font-weight: bold; }
</style>
</head>
<body>
<h1>Sites</h1>
<p><a class="back" href="{{ url_for('index') }}">&larr; back to dashboard</a></p>
<p class="muted">These websites are saved to <code>sites.json</code> and scraped by the bots.</p>

<table>
  <thead><tr><th>Name</th><th>ID</th><th>Checklist URL</th><th>User env</th><th>Credentials</th><th>Action</th></tr></thead>
  <tbody>
  {% for s in sites %}
  <tr>
    <td>{{ s.name }}</td>
    <td><code>{{ s.id }}</code></td>
    <td><code>{{ s.checklist_url }}</code></td>
    <td>{{ s.user_env }}</td>
    <td>
      {% if s.creds_saved %}
      <span class="cred-saved">saved</span>
      {% else %}
      <span class="cred-missing">not saved</span>
      {% endif %}
    </td>
    <td>
      <form class="del" method="post" action="{{ url_for('delete_site', site_id=s.id) }}"
            onsubmit="return confirm('Delete {{ s.name }}?')">
        <button type="submit" class="danger">Delete</button>
      </form>
      <form class="save-creds" method="post" action="{{ url_for('save_site_credentials', site_id=s.id) }}"
            style="display:inline;margin-left:8px;">
        <input type="text" name="username" placeholder="username" required>
        <input type="password" name="password" placeholder="password" required>
        <button type="submit" class="save">Save creds</button>
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

<form class="add-site" method="post" action="{{ url_for('import_chrome_passwords') }}" enctype="multipart/form-data">
  <h3 style="margin-top:0;font-size:1.1rem;">Import passwords from Chrome</h3>
  <p class="muted">Export from <code>chrome://settings/passwords</code>, then upload the CSV here.</p>
  <input type="file" name="csv_file" accept=".csv" required>
  <button type="submit">Import</button>
  <a href="{{ url_for('index') }}" class="secondary" style="text-decoration:none;color:#666;margin-left:8px;">Cancel</a>
</form>

<div class="notice">
  <h3 style="margin-top:0;font-size:1.1rem;">Chrome profile for saved passwords</h3>
  <p class="muted">Reuse Chrome's saved passwords/autofill by launching Playwright with your Chrome profile.</p>
  <form id="chrome-profile-form" method="post" action="{{ url_for('chrome_profiles_select') }}">
    <label style="display:block;margin:8px 0;">Chrome User Data directory</label>
    <input type="text" name="user_data_dir" id="user_data_dir" placeholder="e.g. C:/Users/<you>/AppData/Local/Google/Chrome/User Data" style="width:100%;padding:6px;">
    <label style="display:block;margin:8px 0;">Profile</label>
    <select name="profile" id="profile_select" style="width:100%;padding:6px;">
      <option value="">-- detect profiles --</option>
    </select>
    <p class="muted" id="profile_status" style="margin:8px 0;font-size:0.85rem;"></p>
    <button type="button" onclick="detectChromeProfiles()">Auto-detect Chrome profiles</button>
    <button type="submit">Select profile</button>
    <a href="{{ url_for('index') }}" class="secondary" style="text-decoration:none;color:#666;margin-left:8px;">Cancel</a>
  </form>
  <p class="muted" style="margin-top:8px;font-size:0.85rem;">
    Chrome must be closed for Playwright to use its profile. If Chrome is running, close it first.
  </p>
</div>

<script>
async function detectChromeProfiles() {
  const status = document.getElementById('profile_status');
  const select = document.getElementById('profile_select');
  status.textContent = 'Detecting...';
  select.innerHTML = '<option value="">Loading...</option>';
  try {
    const resp = await fetch('{{ url_for("chrome_profiles") }}');
    const data = await resp.json();
    select.innerHTML = '<option value="">-- select a profile --</option>';
    if (data.profiles && data.profiles.length) {
      data.profiles.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.dir_name;
        opt.textContent = p.name + (p.is_default ? ' (default)' : '') + (p.user_name ? ` (${p.user_name})` : '');
        select.appendChild(opt);
      });
      status.textContent = `Found ${data.profiles.length} profile${data.profiles.length !== 1 ? 's' : ''}`;
      if (data.user_data_dir) {
        document.getElementById('user_data_dir').value = data.user_data_dir;
      }
      if (data.chrome_running) {
        status.textContent += ' — Chrome is running; close it before using the profile.';
      }
    } else {
      status.textContent = 'No Chrome profiles found. Set CHROME_USER_DATA_DIR in .env manually.';
    }
  } catch (e) {
    status.textContent = 'Detection failed: ' + e.message;
  }
}
</script>
</body>
</html>
"""

IMPORT_RESULT_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Import Results - College Tracker</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 800px; margin: 40px auto; padding: 0 16px; color: #111; }
  a.back { color: #0a7; text-decoration: none; }
  a.back:hover { text-decoration: underline; }
  .notice { background: #fff7e6; border: 1px solid #e3d29b; padding: 10px 14px;
            border-radius: 6px; margin-top: 16px; font-size: 0.9rem; }
  .success { background: #e7f3ff; border: 1px solid #bcd4f2; padding: 10px 14px;
             border-radius: 6px; margin-top: 16px; font-size: 0.9rem; }
  pre { background: #f5f5f5; padding: 12px 16px; border-radius: 6px;
        overflow: auto; font-size: 0.85rem; white-space: pre-wrap; }
  .btn { background: #0a7; color: #fff; text-decoration: none; padding: 10px 16px;
         border-radius: 6px; display: inline-block; margin-top: 12px; }
  .btn:hover { background: #085; }
</style>
</head>
<body>
<h1>Import Results</h1>
<p><a class="back" href="{{ url_for('sites') }}">&larr; back to sites</a></p>

<div class="success">
  <strong>Imported {{ imported_count }} new site{{ 's' if imported_count != 1 else '' }}</strong>
  {% if skipped_count %} ({{ skipped_count }} skipped as duplicates){% endif %}
</div>

<div class="notice">
  Add the following lines to your <code>.env</code> file and fill in the real
  credentials. Never commit <code>.env</code> — it is gitignored.
</div>

<pre>{{ env_lines }}</pre>

<a class="btn" href="{{ url_for('sites') }}">Done</a>
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
    sites = load_sites()
    creds = load_credentials()
    # Attach creds_saved flag to each site for the template
    for s in sites:
        s["creds_saved"] = s.get("id") in creds
    return render_template_string(
        HOME_TEMPLATE,
        results=results,
        sites=sites,
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
    site_list = load_sites()
    creds = load_credentials()
    for s in site_list:
        s["creds_saved"] = s.get("id") in creds
    return render_template_string(
        SITES_TEMPLATE, sites=site_list
    )


@app.post("/sites/save-credentials/<site_id>")
def save_site_credentials(site_id):
    """Save username/password for a site to credentials.toml."""
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    profile = (request.form.get("profile") or "").strip() or None

    if not username or not password:
        return redirect(url_for("sites", flash="Username and password are required."))

    sites = load_sites()
    site = next((s for s in sites if s.get("id") == site_id), None)
    if not site:
        return redirect(url_for("sites", flash=f"Site '{site_id}' not found."))

    save_credentials(site_id, username, password, profile)
    return redirect(url_for("sites", flash=f"Credentials saved for '{site['name']}'."))


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


@app.post("/sites/import-chrome-passwords")
def import_chrome_passwords():
    """Import Chrome's exported passwords CSV into sites.json.

    Upload the CSV exported from chrome://settings/passwords.
    New sites are added to sites.json; .env lines are shown so you can
    add the corresponding credentials to .env.
    """
    csv_file = request.files.get("csv_file")
    if not csv_file or csv_file.filename == "":
        return redirect(url_for("sites", flash="No CSV file selected."))

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        imported = import_chrome_passwords_csv(tmp_path)
    except Exception as exc:
        return redirect(url_for("sites", flash=f"Import failed: {exc}"))
    finally:
        os.unlink(tmp_path)

    if not imported:
        return redirect(url_for("sites", flash="No valid passwords found in CSV."))

    sites = load_sites()
    existing_ids = {s.get("id") for s in sites}
    new_sites = [s for s in imported if s.get("id") not in existing_ids]
    sites.extend(new_sites)
    save_sites(sites)

    skipped = len(imported) - len(new_sites)

    # Build .env lines for the new sites
    env_lines = []
    for s in new_sites:
        env_lines.append(f"# {s['name']} ({s['login_url']})")
        env_lines.append(f"{s['user_env']}=your_username_here")
        env_lines.append(f"{s['pass_env']}=your_password_here")
        env_lines.append("")

    return render_template_string(
        IMPORT_RESULT_TEMPLATE,
        imported_count=len(new_sites),
        skipped_count=skipped,
        env_lines="\n".join(env_lines),
    )


@app.get("/chrome-profiles")
def chrome_profiles():
    """List detected Chrome profiles and current selection."""
    user_data_dir = os.getenv("CHROME_USER_DATA_DIR", "")
    current_profile = os.getenv("CHROME_PROFILE", "")
    profiles = detect_chrome_profiles(user_data_dir if user_data_dir else None)
    return {
        "profiles": profiles,
        "user_data_dir": user_data_dir,
        "current_profile": current_profile,
        "chrome_running": is_chrome_running(user_data_dir if user_data_dir else None),
    }


@app.post("/chrome-profiles/select")
def chrome_profiles_select():
    """Save the selected Chrome user data dir and profile to .env."""
    user_data_dir = (request.form.get("user_data_dir") or "").strip()
    profile = (request.form.get("profile") or "").strip()

    if not user_data_dir or not profile:
        return {"error": "user_data_dir and profile are required"}, 400

    if not Path(user_data_dir).exists():
        return {"error": f"Directory not found: {user_data_dir}"}, 400

    local_state = Path(user_data_dir) / "Local State"
    if not local_state.exists():
        return {"error": "Not a Chrome user data directory (missing Local State)"}, 400

    profile_dir = Path(user_data_dir) / profile
    if not profile_dir.exists():
        return {"error": f"Profile not found: {profile}"}, 400

    env_path = BASE_DIR / ".env"
    lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    def set_or_append(lines, key, value):
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
                lines[i] = f"{key}={value}"
                return lines
        lines.append(f"{key}={value}")
        return lines

    lines = set_or_append(lines, "CHROME_USER_DATA_DIR", user_data_dir)
    lines = set_or_append(lines, "CHROME_PROFILE", profile)

    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # Reload dotenv so the running app picks up the change
    load_dotenv(env_path, override=True)

    return {
        "ok": True,
        "user_data_dir": user_data_dir,
        "profile": profile,
        "flash": f"Chrome profile '{profile}' selected.",
    }


def json_load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1",
    )
