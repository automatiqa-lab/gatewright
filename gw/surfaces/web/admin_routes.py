"""Secondary sheets: lessons library, reports, settings.

GW-810 /standards/lessons   lessons with citations from gate assessments; add a lesson
GW-910 /reports             weekly and monthly SteerCo reports (Reporter agent), JSON for Power BI
GW-950 /settings            users and roles, sign-in mode, API tokens, retention and privacy
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from gw.errors import GwError
from gw.governance import core as gov
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.surfaces import auth
from gw.surfaces.principal import principal, service
from gw.surfaces.web.routes import _problem, back, chrome, render

router = APIRouter()
ADMIN = {"admin", "portfolio_lead"}
KNOWN_ROLES = ["portfolio_lead", "project_manager", "project_director", "site_lead", "sponsor",
               "investment_committee", "viewer", "admin", "sme:engineering", "sme:hse", "sme:systems",
               "sme:operations", "sme:procurement", "sme:finance", "sme:legal", "sme:it", "sme:hr"]


# --- GW-810 lessons -------------------------------------------------------------------------------------


@router.get("/standards/lessons", response_class=HTMLResponse)
def lessons(request: Request) -> HTMLResponse:
    svc = service(request)
    projects = {p.id: p.code for p in svc.store.all(m.Project)}
    cited: Counter[str] = Counter()
    where: dict[str, set[str]] = {}
    for o in svc.store.find(m.AgentOutput, output_type="GateAssessment"):
        for lesson in o.payload.get("lessons", []):
            cited[lesson["id"]] += 1
            where.setdefault(lesson["id"], set()).add(str(o.payload.get("project")))
    rows = [{"l": lesson, "source": projects.get(lesson.source_project_id or "", lesson.source_project_id or "—"),
             "cited": cited[lesson.id], "where": sorted(where.get(lesson.id, set()))}
            for lesson in sorted(svc.store.all(m.Lesson), key=lambda x: x.code)]
    ctx = chrome(request, "GW-810", "Lessons library", "lessons", "Standards | gatekeeper citations",
                 ("Issued", "green"), f"<b>Standards</b> | {len(rows)} lessons | {sum(cited.values())} citations")
    ctx.update(rows=rows, project_codes=sorted(projects.values()))
    return render("lessons.html", ctx)


@router.post("/standards/lessons")
def lesson_add(request: Request, title: str = Form(...), text: str = Form(...), tags: str = Form(""),
               source: str = Form("")) -> Response:
    svc, user = service(request), principal(request)
    try:
        gov.require(user, {"portfolio_lead", "project_manager"}, "lessons.add")
        if len(title.strip()) < 8 or len(text.strip()) < 20:
            raise GwError("a lesson needs a title (8+ characters) and what happened (20+ characters)")
        codes = [int(x.code.split("-")[1]) for x in svc.store.all(m.Lesson) if x.code.split("-")[1].isdigit()]
        project = svc.store.find_one(m.Project, code=source) if source else None
        lesson = m.Lesson(id=new_id("Lesson"), code=f"L-{(max(codes) + 1) if codes else 1:03d}", title=title.strip(),
                          text=text.strip(), tags=[t.strip().lower() for t in tags.split(",") if t.strip()],
                          source_project_id=project.id if project else None)
        svc.store.insert(lesson, actor=f"user:{user.id}")
    except GwError as e:
        return back(request, "/standards/lessons", err=_problem(e))
    return back(request, "/standards/lessons", msg=f"{lesson.code} added; the gatekeeper can cite it from the next run")


# --- GW-910 reports --------------------------------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, period: str = "weekly") -> HTMLResponse:
    svc = service(request)
    period = period if period in ("weekly", "monthly") else "weekly"
    outs = [o for o in svc.store.find(m.AgentOutput, output_type="Report", order_by="-created_at")
            if o.payload.get("period") == period]
    report = outs[0].payload if outs else None
    history = [{"at": o.created_at, "from": o.payload.get("from"), "to": o.payload.get("to"), "id": o.id}
               for o in outs[:12]]
    ctx = chrome(request, "GW-910", f"{period.title()} SteerCo report", "reports", "Reporter agent",
                 ("Issued", "green") if report else ("Hold", "amber"),
                 f"<b>Reports</b> | {period} | lagging indicators for SteerCo and Power BI")
    ctx.update(report=report, period=period, history=history, when=outs[0].created_at if outs else None)
    return render("reports.html", ctx)


@router.post("/reports/{period}/run")
def reports_run(request: Request, period: str) -> Response:
    svc = service(request)
    try:
        gov.require(principal(request), ADMIN | {"project_manager"}, "reports.run")
        svc.run_reporter(period if period in ("weekly", "monthly") else "weekly", trigger="manual")
    except GwError as e:
        return back(request, f"/reports?period={period}", err=_problem(e))
    return back(request, f"/reports?period={period}", msg=f"{period} report rendered")


# --- GW-950 settings --------------------------------------------------------------------------------------


def _settings_ctx(request: Request, token: str | None = None, export: Any = None) -> dict[str, Any]:
    svc = service(request)
    tokens = auth.api_tokens(svc.store)
    users = sorted(svc.store.all(m.User), key=lambda u: u.name)
    pol = svc.policies()
    retention = pol.section("retention")
    idps = auth.oidc_connections(svc.store)
    ctx = chrome(request, "GW-950", "Settings | users, sign-in, retention", "settings", "Admin",
                 ("Checked", "blue"), f"<b>Settings</b> | {len(users)} users | "
                 f"{'login mode' if auth.login_required(svc.store) else 'trust mode (localhost)'}")
    ctx.update(users_rows=[{"u": u, "password": bool(u.password_hash),
                            "tokens": sum(1 for t in tokens.values() if t.get("user") == u.id)} for u in users],
               known_roles=KNOWN_ROLES, retention=retention, idps=idps, new_token=token, export=export,
               login_mode=auth.login_required(svc.store), policy_rev=pol.rev,
               purgeable=sum(1 for i in svc.store.all(m.InboundItem)
                             if i.payload and not i.payload.get("purged")
                             and (clock.now() - i.received_at).days > int(retention.get("inbound_item_payload_days", 90))))
    return ctx


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> Response:
    try:
        gov.require(principal(request), ADMIN, "settings.view")
    except GwError as e:
        return back(request, "/", err=_problem(e))
    return render("settings.html", _settings_ctx(request))


@router.post("/settings/users")
def settings_user_add(request: Request, short: str = Form(...), name: str = Form(...), email: str = Form(...),
                      roles: list[str] = Form([])) -> Response:
    svc, user = service(request), principal(request)
    try:
        gov.require(user, ADMIN, "settings.users")
        if svc.store.find_one(m.User, email=email.strip().lower()) or svc.store.find_one(m.User, short=short.strip()):
            raise GwError(f"{short} or {email} already exists")
        svc.store.insert(m.User(id=new_id("User"), email=email.strip().lower(), name=name.strip(), short=short.strip(),
                                roles=[r for r in roles if r in KNOWN_ROLES] or ["viewer"]), actor=f"user:{user.id}")
    except GwError as e:
        return back(request, "/settings", err=_problem(e))
    return back(request, "/settings", msg=f"{name} added")


@router.post("/settings/users/{uid}/roles")
def settings_roles(request: Request, uid: str, roles: list[str] = Form([])) -> Response:
    svc, user = service(request), principal(request)
    try:
        gov.require(user, ADMIN, "settings.roles")
        target = svc.store.get(m.User, uid)
        new = [r for r in roles if r in KNOWN_ROLES]
        if target.id == user.id and not set(new) & ADMIN:
            raise GwError("you cannot remove your own admin and portfolio lead roles")
        leads = [u for u in svc.store.all(m.User) if "portfolio_lead" in u.roles and u.id != target.id]
        if "portfolio_lead" in target.roles and "portfolio_lead" not in new and not leads:
            raise GwError("the last portfolio lead cannot lose the role")
        target.roles = new or ["viewer"]
        svc.store.update(target, actor=f"user:{user.id}", kind="roles_set", payload={"roles": target.roles})
    except GwError as e:
        return back(request, "/settings", err=_problem(e))
    return back(request, "/settings", msg=f"roles updated for {target.name}")


@router.post("/settings/users/{uid}/token", response_class=HTMLResponse)
def settings_token(request: Request, uid: str) -> Response:
    svc, user = service(request), principal(request)
    try:
        gov.require(user, ADMIN, "settings.tokens")
        token = auth.create_api_token(svc.store, svc.store.get(m.User, uid))
    except GwError as e:
        return back(request, "/settings", err=_problem(e))
    return render("settings.html", _settings_ctx(request, token=token))  # shown once, never stored in clear


@router.post("/settings/users/{uid}/tokens/revoke")
def settings_revoke(request: Request, uid: str) -> Response:
    svc, user = service(request), principal(request)
    try:
        gov.require(user, ADMIN, "settings.tokens")
        n = auth.revoke_api_tokens(svc.store, uid)
    except GwError as e:
        return back(request, "/settings", err=_problem(e))
    return back(request, "/settings", msg=f"{n} API tokens revoked")


@router.post("/settings/retention/purge")
def settings_purge(request: Request) -> Response:
    svc = service(request)
    try:
        gov.require(principal(request), ADMIN, "settings.retention")
        n = svc.retention_purge()
    except GwError as e:
        return back(request, "/settings", err=_problem(e))
    return back(request, "/settings", msg=f"{n} inbound payloads purged; derived facts and events kept")


@router.post("/settings/privacy", response_class=HTMLResponse)
def settings_privacy(request: Request, email: str = Form(...), action: str = Form("export")) -> Response:
    svc = service(request)
    try:
        gov.require(principal(request), ADMIN, "settings.privacy")
        if action == "erase":
            res = svc.privacy_erase(email.strip().lower())
            return back(request, "/settings", msg=f"{res['rewritten']} records rewritten for {email}")
        export = svc.privacy_export(email.strip().lower())
    except GwError as e:
        return back(request, "/settings", err=_problem(e))
    return render("settings.html", _settings_ctx(request, export=export))
