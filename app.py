"""
College Tracker website (localhost only).

Dashboard for read-only portal checks, check history, application tasks,
college profiles and campus-visit planning. See SECURITY.md for the trust
boundary: the app is for the person at this computer and binds to loopback.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import history
import inbox
import jobs
import planner
import scheduler
import scraper
from security import (
    ConfigError,
    bind_host_ok,
    csrf_ok,
    env_flag,
    host_header_ok,
    new_csrf_token,
    safe_next,
)
from storage import BASE_DIR, secret_key

load_dotenv(BASE_DIR / ".env")

STATUS_LABELS = {
    "ok": "OK",
    "reauth_required": "Sign-in needed",
    "login_failed": "Login failed",
    "network_error": "Network error",
    "config_error": "Configuration error",
    "error": "Error",
    "pending": "Pending",
}


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        TESTING=testing,
    )
    extra_hosts = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}

    # ------------------------------------------------------------------ guards
    @app.before_request
    def guard():
        if not host_header_ok(request.host, extra_hosts) and not env_flag("ALLOW_REMOTE"):
            abort(400, "This app only answers on localhost.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not csrf_ok(session.get("csrf_token"), supplied):
                abort(400, "Your form expired or came from another site. Reload the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        if "csrf_token" not in session:
            session["csrf_token"] = new_csrf_token()
        return {
            "csrf_token": session["csrf_token"],
            "job": jobs.status(),
            "STATUS_LABELS": STATUS_LABELS,
            "P": planner,
            "today": planner.today(),
            "nav": request.endpoint or "",
            "open_alert_count": len(history.open_alerts()),
            "cred_store": scraper.credential_store(),
        }

    @app.template_filter("when")
    def when(value):
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return value
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%b %d, %Y %H:%M")

    @app.template_filter("day")
    def day(value):
        if not value:
            return "—"
        try:
            return date.fromisoformat(value).strftime("%a %b %d, %Y")
        except ValueError:
            return value

    @app.template_filter("days_from_today")
    def days_from_today(value):
        try:
            delta = (date.fromisoformat(value) - planner.today()).days
        except (TypeError, ValueError):
            return ""
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        if delta == -1:
            return "yesterday"
        return f"in {delta} days" if delta > 0 else f"{-delta} days ago"

    @app.errorhandler(400)
    def bad_request(err):
        return render_template("error.html", code=400, message=getattr(err, "description", "Bad request")), 400

    @app.errorhandler(404)
    def not_found(err):
        return render_template("error.html", code=404, message="Not found."), 404

    @app.errorhandler(413)
    def too_large(err):
        return render_template("error.html", code=413, message="That upload is too large (2 MB max)."), 413

    @app.errorhandler(ConfigError)
    def config_error(err):
        # Any validation/config problem not handled by a route: show it, don't 500.
        flash(str(err), "error")
        return back(request.referrer and safe_next(request.referrer.split(request.host, 1)[-1], "/") or "/")

    def back(default: str = "/"):
        return redirect(safe_next(request.form.get("next") or request.args.get("next"), default))

    def headless() -> bool:
        return os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"

    def site_or_404(site_id: str) -> dict:
        site = scraper.get_site(site_id)
        if not site:
            abort(404)
        return site

    # --------------------------------------------------------------- dashboard
    @app.get("/")
    def index():
        results = scraper.load_results()
        sites = scraper.load_sites()
        data = planner.load()
        return render_template(
            "dashboard.html",
            results=results,
            sites=sites,
            alerts=history.open_alerts()[:20],
            summary=planner.summary(data),
            reminders=planner.due_reminders(data),
            headless=headless(),
            notify=history.notify_config(),
            mail=inbox.summary(),
        )

    @app.post("/checks/run")
    @app.post("/scan")
    def run_checks():
        site_id = request.form.get("site_id") or None
        if site_id:
            site_or_404(site_id)
        label = f"Checking {site_id}" if site_id else "Checking all sites"
        if not jobs.start("check", label, scraper.run_checks, headless=headless(),
                          site_ids=[site_id] if site_id else None, site_id=site_id):
            flash("Another job is still running; wait for it to finish.", "warn")
        else:
            flash("Check started. Results appear here when it finishes.", "info")
        return back(url_for("index"))

    @app.get("/jobs/status")
    def job_status():
        return jsonify(jobs.status())

    @app.post("/jobs/finish-sign-in")
    def finish_sign_in():
        jobs.request_stop()
        flash("Saving your session…", "info")
        return back(url_for("sites"))

    @app.post("/alerts/<alert_id>/dismiss")
    def dismiss_alert(alert_id):
        history.dismiss_alert(alert_id)
        return back(url_for("index"))

    @app.get("/history/<site_id>/<check_id>")
    def check_history(site_id, check_id):
        site = site_or_404(site_id)
        check = next((c for c in site["checks"] if c["id"] == check_id), None)
        if not check:
            abort(404)
        return render_template("history.html", site=site, check=check,
                               runs=history.history_with_diffs(site_id, check_id),
                               max_runs=os.getenv("HISTORY_MAX_RUNS", "50"),
                               max_days=os.getenv("HISTORY_MAX_DAYS", "90"))

    # ------------------------------------------------------------------- sites
    def site_rows():
        creds = scraper.load_credentials()
        rows = []
        for s in scraper.load_sites():
            rows.append({**s, "creds_saved": s["id"] in creds or scraper.has_credentials(s),
                         "creds_in_toml": s["id"] in creds,
                         "session": scraper.session_info(s),
                         "errors": scraper.validate_site(s)})
        return rows

    @app.get("/sites")
    def sites():
        return render_template("sites.html", sites=site_rows(), auth_modes=scraper.AUTH_MODES,
                               colleges=planner.load()["colleges"],
                               chrome_dir=os.getenv("CHROME_USER_DATA_DIR", ""),
                               chrome_profile=os.getenv("CHROME_PROFILE", ""))

    @app.post("/sites/add")
    @app.post("/sites/add/")
    def add_site():
        existing = scraper.load_sites()
        try:
            site = scraper.site_from_form(request.form)
            if any(s["id"] == site["id"] for s in existing):
                raise ConfigError(f"Site id '{site['id']}' already exists.")
        except ConfigError as exc:
            flash(str(exc), "error")
            return back(url_for("sites"))
        existing.append(site)
        scraper.save_sites(existing)
        flash(f"Saved site '{site['name']}'.", "ok")
        return redirect(url_for("edit_site", site_id=site["id"]))

    @app.route("/sites/<site_id>/edit", methods=["GET", "POST"])
    def edit_site(site_id):
        site = site_or_404(site_id)
        if request.method == "POST":
            try:
                updated = scraper.site_from_form(request.form, existing=site)
                updated["id"] = site_id
            except ConfigError as exc:
                flash(str(exc), "error")
                return redirect(url_for("edit_site", site_id=site_id))
            scraper.save_sites([updated if s["id"] == site_id else s for s in scraper.load_sites()])
            flash("Site updated.", "ok")
            return redirect(url_for("edit_site", site_id=site_id))
        row = next(r for r in site_rows() if r["id"] == site_id)
        return render_template("site_edit.html", site=row, auth_modes=scraper.AUTH_MODES,
                               colleges=planner.load()["colleges"],
                               results={r["check_id"]: r for r in scraper.load_results() if r["site_id"] == site_id})

    @app.post("/sites/<site_id>/delete")
    @app.post("/sites/delete/<site_id>")
    def delete_site(site_id):
        sites_ = scraper.load_sites()
        filtered = [s for s in sites_ if s["id"] != site_id]
        if len(filtered) == len(sites_):
            flash(f"Site '{site_id}' not found.", "error")
        else:
            scraper.save_sites(filtered)
            scraper.forget_session(site_id)
            scraper.delete_credentials(site_id)
            flash(f"Deleted site '{site_id}' with its saved session and credentials.", "ok")
        return redirect(url_for("sites"))

    @app.post("/sites/<site_id>/checks/add")
    def add_check(site_id):
        site = site_or_404(site_id)
        try:
            check = scraper.check_from_form(request.form, site)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("edit_site", site_id=site_id))
        site["checks"].append(check)
        scraper.save_sites([site if s["id"] == site_id else s for s in scraper.load_sites()])
        flash(f"Added check '{check['name']}'.", "ok")
        return redirect(url_for("edit_site", site_id=site_id))

    @app.post("/sites/<site_id>/checks/<check_id>/edit")
    def edit_check(site_id, check_id):
        site = site_or_404(site_id)
        current = next((c for c in site["checks"] if c["id"] == check_id), None)
        if not current:
            abort(404)
        try:
            check = scraper.check_from_form(request.form, site, existing=current)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("edit_site", site_id=site_id))
        site["checks"] = [check if c["id"] == check_id else c for c in site["checks"]]
        scraper.save_sites([site if s["id"] == site_id else s for s in scraper.load_sites()])
        flash(f"Updated check '{check['name']}'.", "ok")
        return redirect(url_for("edit_site", site_id=site_id))

    @app.post("/sites/<site_id>/checks/<check_id>/delete")
    def delete_check(site_id, check_id):
        site = site_or_404(site_id)
        site["checks"] = [c for c in site["checks"] if c["id"] != check_id]
        scraper.save_sites([site if s["id"] == site_id else s for s in scraper.load_sites()])
        flash("Check removed (its history is kept on disk).", "ok")
        return redirect(url_for("edit_site", site_id=site_id))

    @app.post("/sites/<site_id>/credentials")
    @app.post("/sites/save-credentials/<site_id>")
    def save_site_credentials(site_id):
        site = site_or_404(site_id)
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Username and password are required.", "error")
        else:
            scraper.save_credentials(site_id, username, password,
                                     (request.form.get("profile") or "").strip() or None)
            flash(f"Credentials saved locally for '{site['name']}'.", "ok")
        return back(url_for("sites"))

    @app.post("/sites/<site_id>/credentials/delete")
    def delete_site_credentials(site_id):
        site_or_404(site_id)
        removed = scraper.delete_credentials(site_id)
        flash("Saved credentials removed." if removed else "No saved credentials for this site.", "ok")
        return back(url_for("sites"))

    @app.post("/sites/<site_id>/sign-in")
    def sign_in(site_id):
        site = site_or_404(site_id)
        if scraper.validate_site(site):
            flash("Fix this site's configuration before signing in.", "error")
            return back(url_for("sites"))

        def task(progress=None):
            return scraper.interactive_sign_in(site_id, stop=jobs.stop_event(), progress=progress)

        if jobs.start("sign_in", f"Sign in to {site['name']}", task, site_id=site_id):
            flash("A browser window is opening. Sign in there (including any MFA, passkey or "
                  "CAPTCHA), then click “I've signed in”.", "info")
        else:
            flash("Another job is still running; wait for it to finish.", "warn")
        return back(url_for("sites"))

    @app.post("/sites/<site_id>/session/forget")
    def forget_session(site_id):
        site_or_404(site_id)
        scraper.forget_session(site_id)
        flash("Saved session deleted. The next check will need you to sign in again.", "ok")
        return back(url_for("sites"))

    @app.post("/sites/import-chrome-passwords")
    def import_chrome_passwords():
        csv_file = request.files.get("csv_file")
        if not csv_file or csv_file.filename == "":
            flash("No CSV file selected.", "error")
            return redirect(url_for("sites"))
        save_secrets = request.form.get("save_credentials") == "on"
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            csv_file.save(tmp_path)
            imported = scraper.import_chrome_passwords_csv(tmp_path, with_secrets=save_secrets)
        except Exception as exc:
            flash(f"Import failed: {exc.__class__.__name__}", "error")
            return redirect(url_for("sites"))
        finally:
            os.unlink(tmp_path)
        if not imported:
            flash("No http(s) logins found in that CSV.", "warn")
            return redirect(url_for("sites"))

        sites_ = scraper.load_sites()
        existing_ids = {s["id"] for s in sites_}
        new_sites = [s for s in imported if s["id"] not in existing_ids]
        if save_secrets:
            for s in new_sites:
                scraper.save_credentials(s["id"], s.pop("username"), s.pop("password"))
        sites_.extend(new_sites)
        scraper.save_sites(sites_)
        env_lines = []
        if not save_secrets:
            for s in new_sites:
                env_lines += [f"# {s['name']} ({s['login_url']})",
                              f"{s['user_env']}=your_username_here",
                              f"{s['pass_env']}=your_password_here", ""]
        return render_template("import_result.html", imported=new_sites,
                               skipped_count=len(imported) - len(new_sites),
                               saved_secrets=save_secrets, env_lines="\n".join(env_lines))

    @app.get("/chrome-profiles")
    def chrome_profiles():
        user_data_dir = os.getenv("CHROME_USER_DATA_DIR", "") or None
        found_dir = user_data_dir or scraper._default_chrome_user_data_dir() or ""
        return jsonify({
            "profiles": scraper.detect_chrome_profiles(user_data_dir),
            "user_data_dir": found_dir,
            "current_profile": os.getenv("CHROME_PROFILE", ""),
            "chrome_running": scraper.is_chrome_running(user_data_dir),
        })

    @app.post("/chrome-profiles/select")
    def chrome_profiles_select():
        user_data_dir = (request.form.get("user_data_dir") or "").strip()
        profile = (request.form.get("profile") or "").strip()
        clear = request.form.get("clear") == "1"
        if clear:
            user_data_dir = profile = ""
        else:
            problem = None
            if not user_data_dir or not profile:
                problem = "Choose a Chrome User Data directory and a profile."
            elif not (Path(user_data_dir) / "Local State").exists():
                problem = "That is not a Chrome User Data directory (no 'Local State' file)."
            elif "/" in profile or "\\" in profile or profile in {".", ".."} \
                    or not (Path(user_data_dir) / profile).is_dir():
                problem = f"Profile '{profile}' not found."
            if problem:
                flash(problem, "error")
                return redirect(url_for("sites"))
        env_path = BASE_DIR / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

        def set_or_append(key, value):
            for i, line in enumerate(lines):
                if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
                    lines[i] = f"{key}={value}"
                    return
            lines.append(f"{key}={value}")

        set_or_append("CHROME_USER_DATA_DIR", user_data_dir)
        set_or_append("CHROME_PROFILE", profile)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["CHROME_USER_DATA_DIR"] = user_data_dir
        os.environ["CHROME_PROFILE"] = profile
        flash("Chrome profile cleared." if clear else f"Chrome profile '{profile}' selected.", "ok")
        return redirect(url_for("sites"))

    # ---------------------------------------------------------------- colleges
    @app.get("/colleges")
    def colleges():
        data = planner.load()
        counts = {c["id"]: {"visits": sum(1 for v in data["visits"] if v["college_id"] == c["id"]),
                            "tasks": sum(1 for t in data["tasks"] if t["college_id"] == c["id"] and t["status"] != "done")}
                  for c in data["colleges"]}
        return render_template("colleges.html", colleges=sorted(data["colleges"], key=lambda c: c["name"].lower()),
                               counts=counts, sites=scraper.load_sites())

    @app.post("/colleges/add")
    def add_college():
        data = planner.load()
        try:
            c = planner.college_from_form(request.form, data=data)
        except ConfigError as exc:
            flash(str(exc), "error")
            return back(url_for("colleges"))
        data["colleges"].append(c)
        planner.save(data)
        flash(f"Added {c['name']}.", "ok")
        return back(url_for("college", college_id=c["id"]))

    @app.get("/colleges/<college_id>")
    def college(college_id):
        data = planner.load()
        c = planner.get_college(data, college_id)
        if not c:
            abort(404)
        visits = sorted([v for v in data["visits"] if v["college_id"] == college_id], key=planner.visit_start)
        tasks = sorted([t for t in data["tasks"] if t["college_id"] == college_id], key=planner.task_sort_key)
        linked = [s for s in scraper.load_sites() if s.get("college_id") == college_id or s["id"] == c.get("portal_site_id")]
        results = [r for r in scraper.load_results() if any(s["id"] == r["site_id"] for s in linked)]
        return render_template("college.html", college=c, visits=visits, tasks=tasks,
                               sites=scraper.load_sites(), linked_sites=linked, results=results)

    @app.post("/colleges/<college_id>/edit")
    def edit_college(college_id):
        data = planner.load()
        c = planner.get_college(data, college_id)
        if not c:
            abort(404)
        try:
            updated = planner.college_from_form(request.form, existing=c)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("college", college_id=college_id))
        data["colleges"] = [updated if x["id"] == college_id else x for x in data["colleges"]]
        planner.save(data)
        flash("College updated.", "ok")
        return redirect(url_for("college", college_id=college_id))

    @app.post("/colleges/<college_id>/delete")
    def delete_college(college_id):
        data = planner.load()
        try:
            planner.delete_college(data, college_id)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("college", college_id=college_id))
        planner.save(data)
        flash("College deleted.", "ok")
        return redirect(url_for("colleges"))

    # ------------------------------------------------------------------ visits
    @app.get("/visits")
    def visits():
        data = planner.load()
        show = request.args.get("show", "upcoming")
        now = datetime.now(timezone.utc)
        items = data["visits"]
        if show == "upcoming":
            items = [v for v in items if v["status"] not in {"completed", "cancelled"}]
        elif show == "past":
            items = [v for v in items if v["status"] in {"completed", "cancelled"} or planner.visit_is_past(v, now)]
        items = sorted(items, key=planner.visit_start, reverse=(show == "past"))
        return render_template("visits.html", visits=items, show=show,
                               colleges={c["id"]: c for c in data["colleges"]}, now=now)

    def visit_form(visit=None, values=None, status=200):
        data = planner.load()
        return render_template("visit_form.html", visit=visit, v=values or visit or {},
                               colleges=sorted(data["colleges"], key=lambda c: c["name"].lower()),
                               timezones=planner.timezone_choices()), status

    @app.get("/visits/new")
    def new_visit():
        return visit_form(values={"college_id": request.args.get("college", ""),
                                  "timezone": planner.local_tz_name(), "remind_days": 3,
                                  "kind": request.args.get("kind", "campus_tour")})

    @app.post("/visits/new")
    def create_visit():
        data = planner.load()
        try:
            v = planner.visit_from_form(request.form, data)
        except ConfigError as exc:
            flash(str(exc), "error")
            return visit_form(values=_form_values(request.form), status=400)
        data["visits"].append(v)
        planner.save(data)
        flash("Visit saved.", "ok")
        return redirect(url_for("visit", visit_id=v["id"]))

    @app.get("/visits/<visit_id>")
    def visit(visit_id):
        data = planner.load()
        v = planner.get_visit(data, visit_id)
        if not v:
            abort(404)
        tasks = [t for t in data["tasks"] if t.get("visit_id") == visit_id]
        return render_template("visit.html", v=v, college=planner.get_college(data, v["college_id"]),
                               tasks=tasks, is_past=planner.visit_is_past(v))

    @app.get("/visits/<visit_id>/edit")
    def edit_visit(visit_id):
        v = planner.get_visit(planner.load(), visit_id)
        if not v:
            abort(404)
        return visit_form(visit=v)

    @app.post("/visits/<visit_id>/edit")
    def update_visit(visit_id):
        data = planner.load()
        v = planner.get_visit(data, visit_id)
        if not v:
            abort(404)
        try:
            updated = planner.visit_from_form(request.form, data, existing=v)
        except ConfigError as exc:
            flash(str(exc), "error")
            return visit_form(visit=v, values={**v, **_form_values(request.form)}, status=400)
        data["visits"] = [updated if x["id"] == visit_id else x for x in data["visits"]]
        planner.save(data)
        flash("Visit updated.", "ok")
        return redirect(url_for("visit", visit_id=visit_id))

    @app.post("/visits/<visit_id>/status")
    def visit_status(visit_id):
        data = planner.load()
        v = planner.get_visit(data, visit_id)
        if not v:
            abort(404)
        try:
            planner.set_visit_status(v, request.form.get("status", ""))
        except ConfigError as exc:
            flash(str(exc), "error")
            return back(url_for("visit", visit_id=visit_id))
        planner.save(data)
        flash(f"Marked {planner.RSVP_STATES[v['status']].lower()}.", "ok")
        return back(url_for("visit", visit_id=visit_id))

    @app.post("/visits/<visit_id>/toggle")
    def visit_toggle(visit_id):
        data = planner.load()
        v = planner.get_visit(data, visit_id)
        if not v:
            abort(404)
        field = request.form.get("field")
        if field not in {"prep", "followups"}:
            abort(400)
        try:
            item = v[field][int(request.form.get("index", "-1"))]
        except (ValueError, IndexError):
            abort(400)
        item["done"] = not item.get("done")
        planner.save(data)
        return back(url_for("visit", visit_id=visit_id))

    @app.post("/visits/<visit_id>/followup-task")
    def followup_task(visit_id):
        data = planner.load()
        v = planner.get_visit(data, visit_id)
        if not v:
            abort(404)
        try:
            item = v["followups"][int(request.form.get("index", "-1"))]
        except (ValueError, IndexError):
            abort(400)
        return redirect(url_for("new_task", college=v["college_id"], visit=visit_id,
                                title=item["text"], category="visit", source_url=v["source_url"]))

    @app.post("/visits/<visit_id>/delete")
    def delete_visit(visit_id):
        data = planner.load()
        data["visits"] = [v for v in data["visits"] if v["id"] != visit_id]
        for t in data["tasks"]:
            if t.get("visit_id") == visit_id:
                t["visit_id"] = ""
        planner.save(data)
        flash("Visit deleted.", "ok")
        return redirect(url_for("visits"))

    # ------------------------------------------------------------------- tasks
    @app.get("/tasks")
    def tasks():
        data = planner.load()
        college_id = request.args.get("college", "")
        show = request.args.get("show", "open")
        items = data["tasks"]
        if college_id:
            items = [t for t in items if t["college_id"] == college_id]
        if show == "open":
            items = [t for t in items if t["status"] != "done"]
        elif show == "done":
            items = [t for t in items if t["status"] == "done"]
        return render_template("tasks.html", tasks=sorted(items, key=planner.task_sort_key), show=show,
                               college_id=college_id,
                               colleges=sorted(data["colleges"], key=lambda c: c["name"].lower()),
                               college_map={c["id"]: c for c in data["colleges"]})

    def task_form(task=None, values=None, status=200):
        data = planner.load()
        return render_template("task_form.html", task=task, t=values or task or {},
                               colleges=sorted(data["colleges"], key=lambda c: c["name"].lower()),
                               visits=sorted(data["visits"], key=planner.visit_start)), status

    @app.get("/tasks/new")
    def new_task():
        a = request.args
        return task_form(values={"college_id": a.get("college", ""), "title": a.get("title", ""),
                                 "source_url": a.get("source_url", ""), "check_ref": a.get("check_ref", ""),
                                 "visit_id": a.get("visit", ""), "category": a.get("category", "application"),
                                 "priority": "normal", "remind_days": 3})

    @app.post("/tasks/new")
    def create_task():
        data = planner.load()
        try:
            t = planner.task_from_form(request.form, data)
        except ConfigError as exc:
            flash(str(exc), "error")
            return task_form(values=_form_values(request.form), status=400)
        data["tasks"].append(t)
        planner.save(data)
        flash("Task saved.", "ok")
        return back(url_for("tasks"))

    @app.get("/tasks/<task_id>/edit")
    def edit_task(task_id):
        t = planner.get_task(planner.load(), task_id)
        if not t:
            abort(404)
        return task_form(task=t)

    @app.post("/tasks/<task_id>/edit")
    def update_task(task_id):
        data = planner.load()
        t = planner.get_task(data, task_id)
        if not t:
            abort(404)
        try:
            updated = planner.task_from_form(request.form, data, existing=t)
        except ConfigError as exc:
            flash(str(exc), "error")
            return task_form(task=t, values={**t, **_form_values(request.form)}, status=400)
        data["tasks"] = [updated if x["id"] == task_id else x for x in data["tasks"]]
        planner.save(data)
        flash("Task updated.", "ok")
        return back(url_for("tasks"))

    @app.post("/tasks/<task_id>/status")
    def task_status(task_id):
        data = planner.load()
        t = planner.get_task(data, task_id)
        if not t:
            abort(404)
        try:
            planner.set_task_status(t, request.form.get("status", ""))
        except ConfigError as exc:
            flash(str(exc), "error")
            return back(url_for("tasks"))
        planner.save(data)
        return back(url_for("tasks"))

    @app.post("/tasks/<task_id>/delete")
    def delete_task(task_id):
        data = planner.load()
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
        planner.save(data)
        flash("Task deleted.", "ok")
        return back(url_for("tasks"))

    @app.get("/calendar.ics")
    def calendar():
        return Response(planner.to_ics(planner.load()), mimetype="text/calendar",
                        headers={"Content-Disposition": "attachment; filename=college-tracker.ics"})

    # ------------------------------------------------------------------- email
    EMAIL_SHOW = {"open": "Open", "important": "Important", "done": "Done", "all": "All"}

    @app.get("/email")
    def email_page():
        colleges = sorted(planner.load()["colleges"], key=lambda c: c["name"].lower())
        a = request.args
        college_id, category, show = a.get("college", ""), a.get("category", ""), a.get("show", "open")
        items = inbox.load_emails()
        counts = inbox.summary(items)
        if college_id:
            items = [m for m in items if m["college_id"] == college_id]
        if category:
            items = [m for m in items if m["category"] == category]
        if show == "open":
            items = [m for m in items if not m.get("done")]
        elif show == "important":
            items = [m for m in items if m.get("important") and not m.get("done")]
        elif show == "done":
            items = [m for m in items if m.get("done")]
        return render_template("email.html", emails=items[:300], counts=counts, colleges=colleges,
                               college_map={c["id"]: c for c in colleges}, college_id=college_id,
                               category=category, show=show, shows=EMAIL_SHOW, cfg=inbox.account_config(),
                               missing_domains=[c for c in colleges if not c.get("email_domains")],
                               web_link=inbox.web_link)

    @app.get("/email/setup")
    def email_setup():
        return render_template("email_setup.html", cfg=inbox.account_config(), rules=inbox.load_rules(),
                               filters=inbox.filter_rules(), colleges=planner.load()["colleges"],
                               stored=len(inbox.load_emails()))

    @app.post("/email/account")
    def email_account():
        try:
            new = inbox.validate_account(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("email_setup"))
        if not new["password"]:
            old = scraper.load_imap_settings()
            if not old.get("password"):
                flash("Enter an app password for the mail account.", "error")
                return redirect(url_for("email_setup"))
            new["password"] = old["password"]
        scraper.save_imap_settings(new)
        flash("Email account saved locally to credentials.toml.", "ok")
        return redirect(url_for("email_setup"))

    @app.post("/email/account/test")
    def email_account_test():
        try:
            flash(inbox.test_connection(inbox.account_config()), "ok")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("email_setup"))

    @app.post("/email/account/delete")
    def email_account_delete():
        scraper.save_imap_settings(None)
        flash("Email account removed.", "ok")
        return redirect(url_for("email_setup"))

    @app.post("/email/rules")
    def email_rules():
        try:
            if request.form.get("reset"):
                inbox.reset_rules()
            else:
                inbox.save_rules_from_form(request.form)
            n = inbox.reclassify_all()
            flash(f"Category rules saved; {n} stored email(s) re-sorted.", "ok")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("email_setup"))

    @app.get("/email/gmail-filters.xml")
    def gmail_filters():
        a = request.args
        prefix = (a.get("prefix") or "College").strip()[:40]
        xml = inbox.gmail_filters_xml(prefix=prefix, by_college=a.get("colleges", "1") == "1",
                                      by_category=a.get("categories", "1") == "1")
        return Response(xml, mimetype="application/xml",
                        headers={"Content-Disposition": "attachment; filename=college-tracker-gmail-filters.xml"})

    @app.post("/email/sync")
    def email_sync():
        if not inbox.account_config()["configured"]:
            flash("Set up your email account first.", "warn")
            return redirect(url_for("email_setup"))

        def task(progress=None):
            try:
                return inbox.sync(progress=progress)
            finally:
                scheduler.mark_run("email")

        if jobs.start("email", "Syncing college email", task):
            flash("Email sync started (read-only).", "info")
        else:
            flash("Another job is still running; wait for it to finish.", "warn")
        return back(url_for("email_page"))

    @app.post("/email/<msg_id>/update")
    def email_update(msg_id):
        f = request.form
        fields = {}
        if "done" in f:
            fields["done"] = f.get("done") == "1"
        if f.get("category"):
            if f["category"] not in planner.TASK_CATEGORIES:
                abort(400)
            fields.update(category=f["category"], category_locked=True)
        if not fields or not inbox.set_email_fields(msg_id, **fields):
            abort(404)
        if fields.get("done"):
            history.dismiss_key(f"email:{msg_id}")
        return back(url_for("email_page"))

    @app.post("/email/clear")
    def email_clear():
        inbox.save_emails([])
        flash("Stored email cleared. Your mailbox was not touched.", "ok")
        return redirect(url_for("email_setup"))

    # ---------------------------------------------------------------- settings
    @app.post("/settings/schedule")
    def save_schedule():
        try:
            scheduler.save_from_form(request.form)
            flash("Automation saved. It runs while this website is open.", "ok")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings"))

    @app.get("/settings")
    def settings():
        sched = scheduler.load()
        return render_template("settings.html", notify=history.notify_config(), sched=sched,
                               next_check=scheduler.next_run("checks", sched["check_hours"], sched),
                               next_email=scheduler.next_run("email", sched["email_hours"], sched),
                               max_runs=os.getenv("HISTORY_MAX_RUNS", "50"),
                               max_days=os.getenv("HISTORY_MAX_DAYS", "90"),
                               session_days=os.getenv("SESSION_MAX_AGE_DAYS", "14"),
                               allow_local=env_flag("ALLOW_LOCAL_TARGETS"),
                               keyring_ok=scraper.keyring_available(),
                               headless=headless())

    @app.post("/settings/test-notification")
    def test_notification():
        cfg = history.notify_config()
        if not cfg["enabled"]:
            flash(cfg["error"] or "Notifications are off. Set NOTIFY_WEBHOOK_URL in .env to turn them on.", "warn")
        else:
            ok = history.notify({"kind": next(iter(cfg["kinds"]), "change"), "title": "College Tracker test",
                                 "body": "Notifications are working.",
                                 "created_at": datetime.now(timezone.utc).isoformat()})
            flash("Test notification sent." if ok else "The webhook did not accept the test notification.",
                  "ok" if ok else "error")
        return redirect(url_for("settings"))

    @app.post("/settings/send-reminders")
    def send_reminders():
        n = planner.send_due_reminders()
        flash(f"{n} reminder(s) recorded." if n else "No reminders are due.", "ok")
        return back(url_for("index"))

    return app


def _form_values(form) -> dict:
    values = {k: form.get(k) for k in form.keys()}
    values["remind"] = form.get("remind") == "on"
    for key in ("prep", "followups"):
        if isinstance(values.get(key), str):
            values[key] = [{"text": l.strip(), "done": False} for l in values[key].splitlines() if l.strip()]
    if isinstance(values.get("questions"), str):
        values["questions"] = [l.strip() for l in values["questions"].splitlines() if l.strip()]
    return values


app = create_app()


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    if not bind_host_ok(host):
        raise SystemExit(f"Refusing to listen on {host}: this app is local-only. "
                         "Use HOST=127.0.0.1 (or set ALLOW_REMOTE=1 if you understand the risk).")
    scheduler.start_background()
    app.run(host=host, port=int(os.getenv("PORT", "5000")), use_reloader=False,
            debug=os.getenv("FLASK_DEBUG", "0") == "1", threaded=True)


if __name__ == "__main__":
    main()
