"""A tiny fake college portal for end-to-end tests.

Routes:
    /login        username/password form (user "student@example.edu", password "s3cret!")
    /checklist    requires the session cookie, otherwise redirects to /login
    /aid          a second protected page
    /mfa/login    accepts the password, then demands a one-time code
    /mfa/home     protected page behind the MFA flow
    /public       no sign-in needed
"""

from __future__ import annotations

import threading
from pathlib import Path

from flask import Flask, make_response, redirect, request
from werkzeug.serving import make_server

HERE = Path(__file__).resolve().parent
STATE = {"items": ["Complete FAFSA application", "Meet with academic advisor", "Register for fall classes"],
         "hold": False}
USER, PASSWORD = "student@example.edu", "s3cret!"


def create_portal() -> Flask:
    app = Flask(__name__)

    def signed_in():
        return request.cookies.get("portal_session") == "ok"

    @app.get("/login")
    def login_form():
        return (HERE / "mock_login.html").read_text(encoding="utf-8")

    @app.post("/login")
    def login():
        if request.form.get("email") == USER and request.form.get("password") == PASSWORD:
            resp = make_response(redirect("/checklist"))
            resp.set_cookie("portal_session", "ok")
            return resp
        return (HERE / "mock_login.html").read_text(encoding="utf-8").replace(
            "<h1>", "<p class='err'>Invalid login</p><h1>"), 401

    @app.get("/checklist")
    def checklist():
        if not signed_in():
            return redirect("/login")
        items = "".join(f"<li>[ ] {i}</li>" for i in STATE["items"])
        hold = "Registration hold: Missing transcript" if STATE["hold"] else "No registration restrictions"
        return (f"<!doctype html><title>Example College - Registration Checklist</title>"
                f"<main><h1>Registration Checklist</h1><ul>{items}</ul><p>{hold}</p></main>")

    @app.get("/aid")
    def aid():
        if not signed_in():
            return redirect("/login")
        return "<!doctype html><title>Financial Aid</title><main><h1>Aid status</h1><p>Award letter available</p></main>"

    @app.get("/public")
    def public():
        return "<!doctype html><title>Open House</title><main><h1>Open house</h1><p>Saturday tours</p></main>"

    @app.get("/mfa/login")
    def mfa_login_form():
        return ("<!doctype html><title>Sign in</title><form method=post>"
                "<input type=email name=email><input type=password name=password>"
                "<button type=submit>Sign in</button></form>")

    @app.post("/mfa/login")
    def mfa_login():
        return ("<!doctype html><title>Verify</title><p>Enter the verification code from your authenticator app</p>"
                "<form method=post action=/mfa/verify><input name=otp autocomplete=one-time-code>"
                "<button type=submit>Verify</button></form>")

    @app.get("/mfa/home")
    def mfa_home():
        if not signed_in():
            return redirect("/mfa/login")
        return "<!doctype html><title>Home</title><main>Welcome</main>"

    return app


class PortalServer:
    def __init__(self):
        self.server = make_server("127.0.0.1", 0, create_portal())
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
