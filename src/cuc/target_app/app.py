"""Flask application factory for the legacy member-servicing stand-in.

Deliberately hostile surface: frameset, nested tables, no ids, no <label>
elements, form fields identified only by adjacent text. Business errors
render as HTTP 200 pages with human-readable text, the way legacy apps do.
"""
from __future__ import annotations

import os
import random
import time
from functools import wraps

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

from . import data
from .faults import FaultKind, FaultRegistry

# Paths that never receive injected faults (harness/navigation chrome).
_FAULT_EXEMPT_PREFIXES = ("/admin", "/nav", "/static")


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config.update(
        SECRET_KEY=os.environ.get("TARGET_APP_SECRET_KEY", "dev-only-not-a-secret"),
        SLOW_SECONDS=float(os.environ.get("TARGET_APP_SLOW_SECONDS", "4")),
    )
    if config:
        app.config.update(config)
    faults = FaultRegistry()
    app.extensions["faults"] = faults

    # ---------------------------------------------------------------- helpers
    def current_user() -> dict | None:
        uid = session.get("uid")
        if not uid or uid not in data.USERS:
            return None
        return {"uid": uid, "role": data.USERS[uid]["role"]}

    def page(template: str, status: int = 200, **ctx) -> Response:
        ctx.setdefault("user", current_user())
        ctx.setdefault("fault", g.get("fault"))
        return Response(render_template(template, **ctx), status=status)

    def login_required(view):
        @wraps(view)
        def wrapper(*a, **kw):
            if current_user() is None:
                return page("expired.html")
            return view(*a, **kw)
        return wrapper

    def teller_required(view):
        @wraps(view)
        @login_required
        def wrapper(*a, **kw):
            if current_user()["role"] != "teller":
                return page("denied.html", reason="role")
            return view(*a, **kw)
        return wrapper

    # ---------------------------------------------------------- fault hooks
    @app.before_request
    def apply_fault():
        g.fault = None
        if request.path.startswith(_FAULT_EXEMPT_PREFIXES) or request.path == "/":
            return None
        kind = request.args.get("fault") or faults.consume(request.path)
        if not kind:
            return None
        try:
            fault = FaultKind(kind)
        except ValueError:
            return None
        g.fault = fault
        if fault is FaultKind.SLOW:
            time.sleep(app.config["SLOW_SECONDS"])
        elif fault is FaultKind.SERVER_ERROR:
            return page("error500.html", status=500, reference=f"ERR-{random.randint(100000, 999999)}")
        elif fault is FaultKind.SESSION_TIMEOUT:
            session.clear()
        elif fault is FaultKind.PERMISSION_DENIED:
            return page("denied.html", reason="injected")
        return None

    # ------------------------------------------------------------- routes
    @app.get("/")
    def frameset():
        return page("frameset.html")

    @app.get("/nav")
    def nav():
        return page("nav.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            uid = request.form.get("uid", "").strip()
            pwd = request.form.get("pwd", "")
            user = data.USERS.get(uid)
            if user and user["password"] == pwd:
                session.clear()
                session["uid"] = uid
                return redirect(url_for("member_search"))
            error = "Invalid User ID or Password. (SEC-401)"
        return page("login.html", error=error)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/members/search", methods=["GET", "POST"])
    @login_required
    def member_search():
        if request.method == "POST":
            number = request.form.get("member_number", "").strip()
            member = None if g.fault is FaultKind.NOT_FOUND else data.find_member(number)
            if member is None:
                return page("search.html", not_found=number or "(blank)")
            return redirect(url_for("member_detail", number=member.number))
        return page("search.html")

    @app.get("/members/<number>")
    @login_required
    def member_detail(number: str):
        member = data.find_member(number)
        if member is None:
            return page("search.html", not_found=number)
        return page("detail.html", member=member)

    @app.route("/members/<number>/subaccount", methods=["GET", "POST"])
    @teller_required
    def subaccount(number: str):
        member = data.find_member(number)
        if member is None:
            return page("search.html", not_found=number)
        errors: list[str] = []
        form = {"product": "SAV", "nickname": "", "deposit": ""}
        if request.method == "POST":
            form = {k: request.form.get(k, "").strip() for k in form}
            if form["product"] not in dict(data.PRODUCT_TYPES):
                errors.append("Product Type is not valid.")
            if not form["nickname"]:
                errors.append("Nickname is required.")
            try:
                deposit_cents = round(float(form["deposit"].replace(",", "").replace("$", "")) * 100)
                if deposit_cents < 0:
                    raise ValueError
            except ValueError:
                deposit_cents = -1
                errors.append("Initial Deposit must be a non-negative amount.")
            if g.fault is FaultKind.VALIDATION:
                errors.append("Nickname contains characters not permitted by policy. (VAL-118)")
            if not errors:
                acct, confirmation = data.open_sub_account(member, form["product"], form["nickname"], deposit_cents)
                session["last_confirmation"] = {"member": member.number, "suffix": acct.suffix, "confirmation": confirmation}
                return redirect(url_for("subaccount_confirm", number=member.number))
        return page("subaccount.html", member=member, form=form, errors=errors, products=data.PRODUCT_TYPES)

    @app.get("/members/<number>/subaccount/confirm")
    @teller_required
    def subaccount_confirm(number: str):
        conf = session.get("last_confirmation")
        member = data.find_member(number)
        if member is None or not conf or conf["member"] != number:
            return redirect(url_for("member_detail", number=number))
        acct = next((a for a in member.accounts if a.suffix == conf["suffix"]), None)
        return page("confirm.html", member=member, account=acct, confirmation=conf["confirmation"])

    # --------------------------------------------------- admin (harness)
    @app.route("/admin/fault/<kind>", methods=["GET", "POST"])
    def admin_arm_fault(kind: str):
        if kind == "clear":
            faults.clear()
            return jsonify({"armed": []})
        try:
            fk = FaultKind(kind)
        except ValueError:
            return jsonify({"error": f"unknown fault kind {kind!r}", "kinds": [k.value for k in FaultKind]}), 400
        route = request.args.get("route", "/members")
        count = int(request.args.get("count", "1"))
        faults.arm(fk, route, count)
        return jsonify({"armed": faults.status()})

    @app.get("/admin/faults")
    def admin_faults():
        return jsonify({"armed": faults.status()})

    @app.post("/admin/reset")
    def admin_reset():
        data.reset()
        faults.clear()
        return jsonify({"reset": True})

    return app


def main() -> None:  # pragma: no cover - manual entry point
    port = int(os.environ.get("TARGET_APP_PORT", "5055"))
    create_app().run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":  # pragma: no cover
    main()
