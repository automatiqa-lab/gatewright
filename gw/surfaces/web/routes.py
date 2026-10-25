"""Web sheets (server-rendered Jinja2 + HTMX).

Every route is a thin translation of service calls into a sheet. No business logic:
data shaping only. POST routes redirect back to the sheet (303) and carry a
problem detail in `?err=` when a GwError (Forbidden, Conflict, 422) is raised.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from gw.errors import GwError
from gw.record import clock
from gw.record import models as m
from gw.surfaces import auth
from gw.surfaces.principal import principal, service

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter(include_in_schema=False)

NAV = [
    ("GW-010", "Brief", "/brief", "brief"),
    ("GW-020", "Ask", "/ask", "ask"),
    ("GW-100", "War room", "/", "war"),
    ("GW-110", "Heat map", "/heatmap", "heat"),
    ("GW-200", "Gates", "/gates", "gate"),
    ("GW-300", "Chasers", "/chasers", "chase"),
    ("GW-310", "Inbox", "/inbox", "inbox"),
    ("GW-320", "Tenders", "/tender", "tender"),
    ("GW-400", "Dependencies", "/deps", "deps"),
    ("GW-500", "Forecast", "/forecast", "forecast"),
]
NAV_SYSTEM = [
    ("GW-600", "Harness", "/harness", "harness"),
    ("GW-700", "Policies", "/policies", "policies"),
    ("GW-800", "Gate models", "/standards/gatemodels", "gm"),
    ("GW-810", "Lessons", "/standards/lessons", "lessons"),
    ("GW-910", "Reports", "/reports", "reports"),
    ("GW-900", "Agents", "/agents", "agents"),
    ("—", "Projects", "/projects", "projects"),
    ("GW-950", "Settings", "/settings", "settings"),
]
MCP_TOOLS = [
    "gw.portfolio.list", "gw.project.get", "gw.gate.score", "gw.gate.gaps", "gw.gate.pack", "gw.gate.decide",
    "gw.commitment.list", "gw.commitment.chase", "gw.commitment.set_status", "gw.evidence.link",
    "gw.evidence.accept", "gw.chase.status", "gw.signal.query", "gw.heatmap.get", "gw.forecast.slip",
    "gw.forecast.gate", "gw.forecast.whatif", "gw.forecast.explain", "gw.doc.classify", "gw.reply.parse",
    "gw.search", "gw.deps.propose", "gw.deps.decide", "gw.tender.check", "gw.policy.get", "gw.policy.set",
    "gw.approval.list", "gw.approval.decide", "gw.audit.query", "gw.harness.list", "gw.harness.test",
    "gw.harness.sync", "gw.brief.today",
]


# --- filters ------------------------------------------------------------------------------


def _dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def ddmmm(v: Any) -> str:
    d = _dt(v)
    return d.strftime("%d %b") if d else "—"


def hhmm(v: Any) -> str:
    d = _dt(v)
    return d.strftime("%d %b %H:%M") if d else "—"


def pct(v: Any, digits: int = 0) -> str:
    return "—" if v is None else f"{float(v) * 100:.{digits}f}%"


def heat(v: Any) -> str:
    """Six-step single-hue ramp."""
    if v is None:
        return "none"
    x = abs(float(v))
    for i, top in enumerate((16, 33, 50, 66, 83)):
        if x <= top:
            return f"h{i}"
    return "h5"


def num(v: Any, digits: int = 2) -> str:
    return "—" if v is None else f"{float(v):.{digits}f}"


def ai_chip(obj: Any) -> Markup:
    """The visible "drafted by AI" chip for model-written content (Art. 50); nothing otherwise.
    Its data-ai-scope attribute is what the disclosure middleware turns into meta tags."""
    from gw.governance import disclosure

    env = obj.get("ai_generated") if isinstance(obj, dict) else None
    if env is None or not disclosure.is_envelope(env):
        return Markup("")
    scope = ",".join(s for s in env.get("scope", []) if re.fullmatch(r"[a-z0-9_:-]+", s))
    return Markup('<span class="tag ai" data-ai-generated="true" data-ai-scope="{}">{}</span>').format(
        scope, disclosure.item_chip())


def ai_session_notice() -> str:
    """The Art. 50(1) line on a sheet where a person works with an agent."""
    from gw.governance import disclosure

    return disclosure.session_notice()


templates.env.filters.update(ddmmm=ddmmm, hhmm=hhmm, pct=pct, heat=heat, num=num)
templates.env.globals.update(ai_chip=ai_chip, ai_session_notice=ai_session_notice)


# --- chrome --------------------------------------------------------------------------------


def _safe(fn: Callable[[], Any], default: Any = None) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except GwError as e:
        return default, e.detail


def _last_human(svc: Any) -> str:
    users = {u.id: u.name for u in svc.store.all(m.User)}
    for e in svc.store.events(since=clock.now() - timedelta(days=7), limit=500):
        actor = str(e.get("actor", ""))
        if actor.startswith("user:"):
            return str(users.get(actor[5:], actor[5:]))
    return "—"


def chrome(request: Request, sheet: str, title: str, nav: str, drawn: str, status: tuple[str, str],
           crumb: str = "") -> dict[str, Any]:
    svc = service(request)
    user = principal(request)
    svc._viewer(user)  # every sheet is a read; a user with no role sees none
    conns = svc.registry.list()
    syncs = [c.last_sync_at for c in conns if c.last_sync_at]
    readiness = svc.readiness()
    issue = clock.now()
    return {
        "request": request, "sheet": sheet, "title": title, "nav": NAV, "nav_system": NAV_SYSTEM, "cur": nav,
        "user": user, "users": sorted(svc.store.all(m.User), key=lambda u: u.name), "drawn": drawn,
        "checked": _last_human(svc), "status_word": status[0], "status_colour": status[1],
        "issue": issue, "readiness": readiness.get("overall"), "last_sync": max(syncs) if syncs else None,
        "n_conns": len(conns), "unhealthy": sum(1 for c in conns if c.health != "ok"), "crumb": _crumb(crumb),
        "err": request.query_params.get("err"), "msg": request.query_params.get("msg"),
        "seq": svc.store.last_seq(),
    }


def _crumb(text: str) -> Markup:
    """Escape a heading built from record values; only <b> and </b> stay markup (XSS)."""
    return Markup(str(escape(text)).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>"))


def back(request: Request, fallback: str, err: str | None = None, msg: str | None = None) -> RedirectResponse:
    ref = request.headers.get("referer") or fallback
    parsed = urlparse(ref)
    path = parsed.path or fallback
    if (parsed.netloc and parsed.netloc != request.url.netloc) or not path.startswith("/") \
            or path.startswith(("//", "/\\")) or "\\" in path:
        path, parsed = fallback, urlparse(fallback)
    params = {k: v for k, v in (p.split("=", 1) for p in parsed.query.split("&") if "=" in p) if k not in ("err", "msg")}
    if err:
        params["err"] = err
    if msg:
        params["msg"] = msg
    q = "&".join(f"{k}={quote(str(v), safe='')}" if k in ("err", "msg") else f"{k}={v}" for k, v in params.items())
    # the fallback comes from the caller and may carry a path segment, so it is checked as well
    return RedirectResponse(local_path(local_path(path + (f"?{q}" if q else ""), fallback)), status_code=303)


def local_path(url: str, fallback: str = "/") -> str:
    """A redirect target on this site, or the fallback.

    Browsers read a backslash as a slash and drop tabs and line breaks, so "/" + backslash + "evil.example"
    and "///evil.example" both leave the site; such targets fall back.
    """
    url = url.replace("\\", "").replace("\t", "").replace("\n", "").replace("\r", "")
    if url.startswith("/") and not url.startswith("//") and not urlparse(url).netloc and not urlparse(url).scheme:
        return url
    return fallback


def render(name: str, ctx: dict[str, Any]) -> HTMLResponse:
    """Render a sheet; every page carries the CSRF token (double-submit cookie)."""
    request: Request = ctx["request"]
    token = request.cookies.get(auth.CSRF_COOKIE) or auth.new_csrf()
    ctx.setdefault("csrf", token)
    ctx.setdefault("login_mode", auth.login_required(service(request).store))
    resp = templates.TemplateResponse(request, name, ctx)
    if not request.cookies.get(auth.CSRF_COOKIE):
        resp.set_cookie(auth.CSRF_COOKIE, token, samesite="strict", httponly=False)
    return resp


def _problem(e: GwError) -> str:
    p = e.problem()
    extra = ""
    if p.get("pointer"):
        extra = f" at {p['pointer']}"
    if p.get("limit"):
        extra += f" (hard limit: {p['limit']})"
    if p.get("allowed"):
        extra += f"; allowed: {', '.join(p['allowed'])}"
    return f"{p['title']}: {p['detail']}{extra}"


# --- principal switch ------------------------------------------------------------------------


@router.post("/as")
def act_as(request: Request, user: str = Form(...)) -> RedirectResponse:
    """Trust mode only: switch the acting user. Refused once any user has a password."""
    if auth.login_required(service(request).store):
        return back(request, "/", err="switching user is disabled in login mode; sign out and in instead")
    try:
        chosen = service(request).user(user)
    except GwError as e:
        return back(request, "/", err=_problem(e))
    resp = back(request, "/")
    resp.set_cookie("gw_user", chosen.id, samesite="lax", httponly=True)
    return resp


# --- GW-100 war room ------------------------------------------------------------------------------


def _changed_projects(svc: Any) -> set[str]:
    since = clock.now() - timedelta(hours=24)
    ids = {e["entity_id"] for e in svc.store.events(since=since, entity_type="Commitment", limit=5000)
           if e["kind"] in ("status", "replied", "accepted", "rejected") and not str(e["actor"]).startswith("system:")}
    if not ids:
        return set()
    return {c.project_id for c in svc.store.find(m.Commitment, id=list(ids))}


def _with_links(svc: Any, b: dict[str, Any] | None) -> dict[str, Any] | None:
    """Add a `url` to each focus item and change of a brief payload (: cited items open their sheet)."""
    if not b:
        return b
    from gw.surfaces.web.links import Linker

    link = Linker(svc.store)
    items = []
    for it in b.get("items", []):
        cites = it.get("citations") or []
        url = link.url(it["approval_id"]) if it.get("approval_id") else link.url(cites[0]) if cites else None
        items.append({**it, "url": url})
    changed = []
    for c in b.get("changed", []):
        url = link.url(c.get("entity_id"), c.get("entity_type"))
        if url is None:  # briefs issued before changes carried their entity: read the code from the text
            code = str(c.get("what", "")).split(" ", 1)[0]
            hit = svc.store.find_one(m.Commitment, code=code) if code.startswith("C-") else None
            url = f"/commitments/{code}" if hit else None
        changed.append({**c, "url": url})
    return {**b, "items": items, "changed": changed}


@router.get("/", response_class=HTMLResponse)
def war_room(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    rows, err = _safe(lambda: svc.portfolio_list(user), [])
    port = {s.signal_id: s for s in svc.store.latest_signals(scope="portfolio")}
    proj_sig = svc.store.latest_signals(scope="project")
    losing = sum(1 for s in proj_sig if s.signal_id == "float_erosion" and s.value < 0)
    stale = sum(1 for s in proj_sig if s.signal_id == "schedule_staleness" and s.value > 10)
    rr = port.get("chase_response_rate")
    from gw.engines.forecast.engine import latest

    ps = [r.p for r in latest(svc.store, "project_slip").values()]
    slips = latest(svc.store, "slip")
    above = sum(1 for r in slips.values() if r.p > 0.5)
    changed = _changed_projects(svc)
    codes = {p.id: p.code for p in svc.store.all(m.Project)}
    brief, berr = _safe(lambda: _with_links(svc, svc.brief(user)), None)
    silent = sum(1 for s in svc.store.latest_signals("silence_days", "party") if s.value >= 3)
    kpis = {
        "due": port["due_in_n"].value if "due_in_n" in port else None,
        "unconfirmed": port["unconfirmed_due_in_n"].value if "unconfirmed_due_in_n" in port else None,
        "at_risk": sum(1 for r in rows if r["at_risk"]), "projects": len(rows), "losing": losing, "stale": stale,
        "rate7": (rr.inputs or {}).get("rate_7d") if rr else None, "silent": silent,
        "pslip": sum(ps) / len(ps) if ps else None, "above": above,
        "window": int(svc.policies().get("signals.due_window_days", 5)),
    }
    ctx = chrome(request, "GW-100", "Portfolio war room", "war", "Brief + Reporter agents",
                 ("For decision", "amber"),
                 f"<b>Portfolio</b> | {len(rows)} projects | {len({r['zone'] for r in rows})} zones | "
                 f"{clock.now():%a %d %b %Y} | issue {clock.now():%H:%M}")
    ctx.update(rows=rows, kpis=kpis, changed={codes[p] for p in changed if p in codes}, brief=brief,
               err=ctx["err"] or err or berr)
    return render("war.html", ctx)


@router.get("/projects", response_class=HTMLResponse)
def projects(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    rows, err = _safe(lambda: svc.portfolio_list(user), [])
    ctx = chrome(request, "GW-101", "Projects", "projects", "Record", ("Derived", "blue"),
                 f"<b>Projects</b> | {len(rows)}")
    ctx.update(rows=rows, err=ctx["err"] or err)
    return render("projects.html", ctx)


@router.get("/gates", response_class=HTMLResponse)
def gates_list(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    rows, err = _safe(lambda: svc.portfolio_list(user), [])
    ctx = chrome(request, "GW-200", "Gate readiness", "gate", "Gatekeeper agent", ("Derived", "blue"),
                 f"<b>Gates</b> | {len(rows)} current gate instances")
    ctx.update(rows=rows, err=ctx["err"] or err, gates_index=True)
    return render("projects.html", ctx)


# --- GW-110 heat map -------------------------------------------------------------------------------


@router.get("/heatmap", response_class=HTMLResponse)
def heatmap(request: Request, mode: str = "exposure") -> HTMLResponse:
    svc, user = service(request), principal(request)
    if mode not in ("exposure", "trend", "forecast"):
        mode = "exposure"
    hm, err = _safe(lambda: svc.heatmap(user, mode), {"dimensions": [], "rows": []})
    load, err2 = _safe(lambda: svc.heatmap(user, "load"), {"weeks": [], "rows": []})
    ctx = chrome(request, "GW-110", "Exposure and load heat maps", "heat", "Signals + Prediction engine",
                 ("Derived", "blue"),
                 f"<b>Heat map</b> | {'exposure' if mode == 'exposure' else mode} by project and dimension | "
                 f"derived {hhmm(hm.get('computed_at'))}")
    ctx.update(hm=hm, load=load, mode=mode, err=ctx["err"] or err or err2)
    return render("heatmap.html", ctx)


# --- GW-200 gate readiness -----------------------------------------------------------------------------


@router.get("/projects/{code}", response_class=HTMLResponse)
def project(request: Request, code: str) -> Response:
    svc = service(request)
    try:
        gi = svc.gate_instance(code)
    except GwError as e:
        return back(request, "/projects", err=_problem(e))
    return RedirectResponse(local_path(f"/projects/{quote(code, safe='')}/gates/{quote(gi.gate_id, safe='')}"),
                            status_code=303)


@router.get("/projects/{code}/gates/{gate}", response_class=HTMLResponse)
def gate(request: Request, code: str, gate: str) -> HTMLResponse:
    svc, user = service(request), principal(request)
    p = svc.project(code)
    gi = svc.gate_instance(code, gate)
    score = svc.gate_score(user, code, gate)
    gaps = {g["deliverable_id"]: g for g in svc.gate_gaps(user, code, gate)["gaps"]}
    model = next((x for x in svc.gatemodels() if x["id"] == gi.gate_model_id), None)
    instances = {g.gate_id: g for g in svc.store.find(m.GateInstance, project_id=p.id)}
    track = []
    for g in sorted((model["model"]["gates"] if model else []), key=lambda x: x["order"]):
        inst = instances.get(g["id"])
        if inst and inst.decision in ("pass", "pass_with_actions"):
            state, sub = "done", f"{inst.decision.replace('_', ' ')} {ddmmm(inst.decided_at)}"
        elif inst and inst.decision == "pending":
            state, sub = "cur", f"{pct(score['readiness'])} of {pct(inst.threshold)} | review {ddmmm(inst.planned_review_at)}"
        elif inst:
            state, sub = "cur", f"{inst.decision} {ddmmm(inst.decided_at)}"
        else:
            state, sub = "next", f"threshold {pct(g['threshold'])}"
        track.append({"name": g["name"], "state": state, "sub": sub, "id": g["id"]})
    cs = {c.deliverable_id: c for c in svc.store.find(m.Commitment, gate_instance_id=gi.id) if c.kind == "gate_deliverable"}
    users = {u.id: u.name for u in svc.store.all(m.User)}
    parties = {x.id: x.name for x in svc.store.all(m.Party)}
    ev_ids = [c.evidence_id for c in cs.values() if c.evidence_id]
    evidence = {e.id: e for e in svc.store.find(m.Evidence, id=ev_ids)} if ev_ids else {}
    rows = []
    for d in gi.deliverables:
        c = cs.get(d.id)
        ev = evidence.get(c.evidence_id) if c and c.evidence_id else None
        gap = gaps.get(d.id)
        status = str(c.status) if c else "missing"
        if gap and gap["reason"] == "check failed":
            status_label, colour = "check failed", "amber"
        elif status == "accepted":
            status_label, colour = "accepted", "green"
        elif status == "submitted":
            status_label, colour = "in review", "amber"
        elif status == "waived":
            status_label, colour = "waived", ""
        elif status == "flagged":
            status_label, colour = "flagged", "red" if d.mandatory else "amber"
        elif status == "overdue":
            status_label, colour = "overdue", "red"
        else:
            status_label, colour = ("missing" if d.mandatory else "open"), "red" if d.mandatory else "amber"
        hatch = ""
        if gap:
            hatch = "hatch" if d.mandatory and gap["credit"] < 1 and status != "submitted" else "hatch amber"
        rows.append({"d": d, "c": c, "ev": ev, "gap": gap, "status_label": status_label, "colour": colour,
                     "hatch": hatch, "owner": users.get(c.owner_user_id, "—") if c else "—",
                     "party": parties.get(c.party_id, "—") if c else "—"})
    assessment = svc.gate_assessment(code, gate)
    pack, _ = _safe(lambda: svc.gate_pack(user, code, gate), None)
    fg, _ = _safe(lambda: svc.forecast_gate(user, code, gate), None)
    rec = score["recommendation"]
    stamp = ("Hold", "red") if rec == "hold" else ("Pass", "green") if rec == "pass" else ("Pass with actions", "amber")
    ctx = chrome(request, "GW-200", f"{code} | {gi.name} readiness", "gate", "Gatekeeper agent", stamp,
                 f"<b>{code}</b> | {p.name} | gate model {gi.gate_model_id} rev {gi.gate_model_rev} | {gi.name} instance")
    ctx.update(p=p, gi=gi, score=score, track=track, rows=rows, assessment=assessment, pack=pack, fg=fg,
               stamp=stamp, days_to_review=(gi.planned_review_at.date() - clock.today()).days)
    return render("gate.html", ctx)


@router.post("/projects/{code}/gates/{gate}/decide")
def gate_decide(request: Request, code: str, gate: str, decision: str = Form(...), note: str = Form("")) -> Response:
    svc, user = service(request), principal(request)
    try:
        svc.gate_decide(user, code, gate, decision, note)
    except GwError as e:
        return back(request, f"/projects/{code}/gates/{gate}", err=_problem(e))
    return RedirectResponse(local_path(f"/projects/{quote(code, safe='')}?msg={quote(f'{code}/{gate} stamped {decision}')}"),
                            status_code=303)


@router.post("/projects/{code}/gates/{gate}/gatekeeper")
def run_gatekeeper(request: Request, code: str, gate: str) -> Response:
    svc = service(request)
    try:
        from gw.governance import core as gov

        gov.require(principal(request), {"project_manager", "portfolio_lead"}, "gatekeeper.run")
        res = svc.run_gatekeeper(code)
    except GwError as e:
        return back(request, f"/projects/{code}/gates/{gate}", err=_problem(e))
    return back(request, f"/projects/{code}/gates/{gate}", msg=f"Gatekeeper: {res.run.summary}")


@router.post("/evidence/{evidence}/decide")
def evidence_decide(request: Request, evidence: str, accept: str = Form(...), reason: str = Form("")) -> Response:
    svc, user = service(request), principal(request)
    try:
        svc.evidence_accept(user, evidence, accept == "1", reason or None)
    except GwError as e:
        return back(request, "/gates", err=_problem(e))
    return back(request, "/gates", msg="evidence accepted" if accept == "1" else "evidence rejected")


@router.get("/projects/{code}/gates/{gate}/pack", response_class=HTMLResponse)
def gate_pack(request: Request, code: str, gate: str) -> HTMLResponse:
    svc, user = service(request), principal(request)
    pack, err = _safe(lambda: svc.gate_pack(user, code, gate), None)
    ctx = chrome(request, "GW-210", f"{code} | review pack", "gate", "Gatekeeper agent",
                 ("Issued", "green") if pack and pack["status"] == "issued" else ("Draft", "amber"),
                 f"<b>{code}</b> | {gate} review pack")
    ctx.update(pack=pack, code=code, gate=gate, err=ctx["err"] or err)
    return render("pack.html", ctx)


# --- GW-800 gate models -------------------------------------------------------------------------------


@router.get("/standards/gatemodels", response_class=HTMLResponse)
def gatemodels(request: Request, model: str | None = None) -> HTMLResponse:
    svc = service(request)
    models = svc.gatemodels()
    cur = next((x for x in models if x["id"] == model), None) or next((x for x in models if x["id"] == service(request).policies().get("gates.default_model")), None) \
        or (models[0] if models else None)
    custom = sum(1 for x in models if x["published_by"] != "system")
    ctx = chrome(request, "GW-800", "Gate model editor", "gm", "Standards library", ("Issued", "green"),
                 f"<b>Standards</b> | gate models | {len(models)} installed | {custom} published by people")
    ctx.update(models=models, model=cur)
    return render("gatemodels.html", ctx)


@router.post("/standards/gatemodels/validate", response_class=HTMLResponse)
def gatemodels_validate(request: Request, yaml_text: str = Form(...)) -> HTMLResponse:
    svc = service(request)
    try:
        rep = svc.standards_validate(yaml_text)
    except Exception as e:  # malformed YAML is a validation result, not a crash
        rep = {"ok": False, "errors": [{"pointer": "", "message": f"YAML error: {e}"}], "warnings": []}
    return render("_validation.html", {"request": request, "rep": rep})


@router.post("/standards/gatemodels/publish")
def gatemodels_publish(request: Request, yaml_text: str = Form(...)) -> Response:
    svc, user = service(request), principal(request)
    try:
        res = svc.standards_publish(user, yaml_text)
    except GwError as e:
        return back(request, "/standards/gatemodels", err=_problem(e))
    except Exception as e:
        return back(request, "/standards/gatemodels", err=f"YAML error: {e}")
    return RedirectResponse(f"/standards/gatemodels?model={res['id']}&msg={quote(f'published {res['id']} rev {res['rev']}')}",
                            status_code=303)


# --- GW-300 chasers ----------------------------------------------------------------------------------


@router.get("/chasers", response_class=HTMLResponse)
def chasers(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    st, err = _safe(lambda: svc.chase_status(user), {"summary": {}, "commitments": []})
    approvals = [a for a in svc.approval_list(user) if a["kind"] in ("escalation_l3", "send_external")]
    since = clock.now() - timedelta(days=7)
    sent7 = [c for c in svc.store.find(m.Chase, status="sent") if c.sent_at and c.sent_at >= since]
    replied7 = sum(1 for c in sent7 if c.replied_at)
    latest_out = svc.store.find(m.Chase, status=["queued", "sent"], order_by="-planned_for", limit=40)
    preview = next((c for c in latest_out if c.text), None)
    war = svc.store.find(m.Chase, status="war_room", order_by="-planned_for", limit=20)
    codes = {c.id: c.code for c in svc.store.find(m.Commitment, id=[w.commitment_id for w in war])} if war else {}
    pol = svc.policies()
    ctx = chrome(request, "GW-300", "Chaser loop", "chase", "Chaser agent",
                 ("For decision", "amber") if approvals else ("Checked", "blue"),
                 f"<b>Chasers</b> | last 7 days | {len(sent7)} sent")
    ctx.update(st=st, approvals=approvals, sent7=len(sent7), replied7=replied7, preview=preview,
               ladder=pol.get("escalation.ladder") or [], cadence=pol.get("chasing.cadence") or {},
               silence_l3=pol.get("escalation.silence_days_to_l3"), war=war, war_codes=codes,
               err=ctx["err"] or err)
    return render("chasers.html", ctx)


@router.post("/chasers/run")
def chasers_run(request: Request) -> Response:
    svc = service(request)
    try:
        from gw.governance import core as gov

        gov.require(principal(request), {"project_manager", "portfolio_lead"}, "chaser.run")
        res = svc.run_chaser(trigger="manual")
    except GwError as e:
        return back(request, "/chasers", err=_problem(e))
    return back(request, "/chasers", msg=f"Chaser: {res['summary']} | {res['dispatched']} dispatched")


@router.post("/approvals/{approval}")
def approval_decide(request: Request, approval: str, decision: str = Form(...),
                    edited_output: str = Form("")) -> Response:
    svc, user = service(request), principal(request)
    try:
        svc.approval_decide(user, approval, decision, edited_output or None)
    except GwError as e:
        return back(request, "/chasers", err=_problem(e))
    word = {"approved": "approved", "approved_edited": "approved with edits", "rejected": "rejected", "hold": "held"}
    return back(request, "/chasers", msg=f"{approval} {word.get(decision, decision)}")


# --- GW-310 inbox --------------------------------------------------------------------------------------


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    items = svc.store.find(m.InboundItem, kind=["message", "document"], order_by="-received_at", limit=40)
    cids = [i.commitment_id for i in items if i.commitment_id]
    codes = {c.id: c.code for c in svc.store.find(m.Commitment, id=cids)} if cids else {}
    queue, err = _safe(lambda: svc.review_queue(user), [])
    queue = [q for q in queue if q["kind"] in ("message", "document")]
    metrics = svc.semantic_metrics()
    tax = svc.taxonomy_get()
    taxonomy = yaml.safe_load(tax["yaml"]) or {}
    taxonomy["rev"] = tax["rev"]
    since = clock.now() - timedelta(hours=24)
    recent = [i for i in items if i.received_at >= since]
    applied = sum(1 for i in recent if i.processed_at and i.review_state is None)
    in_review = sum(1 for i in recent if i.review_state == "pending")
    cur = metrics.get("current") or {}
    ctx = chrome(request, "GW-310", "Inbox intelligence", "inbox",
                 f"Semantic engine rev {cur.get('rev', '—')}",
                 ("For decision", "amber") if queue else ("Checked", "blue"),
                 f"<b>Inbox intelligence</b> | last 24 h | {len(recent)} items")
    ctx.update(items=items, codes=codes, queue=queue, metrics=metrics, cur=cur, taxonomy=taxonomy,
               taxonomy_yaml=tax["yaml"], taxonomy_history=tax["history"],
               applied=applied, in_review=in_review, err=ctx["err"] or err)
    return render("inbox.html", ctx)


@router.post("/inbox/taxonomy")
def inbox_taxonomy(request: Request, yaml_text: str = Form(...), note: str = Form("")) -> Response:
    """GW-310 taxonomy editor: every save is a validated revision."""
    svc, user = service(request), principal(request)
    try:
        res = svc.taxonomy_set(user, yaml_text, note)
    except GwError as e:
        return back(request, "/inbox", err=_problem(e))
    return back(request, "/inbox", msg=f"taxonomy rev {res['rev']} saved")


@router.post("/inbox/{item}/review")
def inbox_review(request: Request, item: str, label: str = Form(...), commitment: str = Form("")) -> Response:
    svc, user = service(request), principal(request)
    try:
        svc.review_decide(user, item, label, commitment or None, accept=bool(commitment))
    except GwError as e:
        return back(request, "/inbox", err=_problem(e))
    return back(request, "/inbox", msg=f"{item} labelled {label}")


# --- GW-320 tender ------------------------------------------------------------------------------------


def _latest_tender(svc: Any, code: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, Any]:
    check = bids = None
    when = None
    for o in svc.store.find(m.AgentOutput, output_type=["PackageCheck", "BidComparison"], order_by="created_at"):
        if o.payload.get("package") != code:
            continue
        if o.output_type == "PackageCheck":
            check, when = o.payload, o.created_at
        else:
            bids = o.payload
    return check, bids, when


@router.get("/packages/{code}/tender")
def package_tender(code: str) -> RedirectResponse:
    """GW-320 at the address gives it; the sheet itself lives at /tender?package=."""
    return RedirectResponse(local_path(f"/tender?package={quote(code, safe='')}"), status_code=307)


@router.get("/tender", response_class=HTMLResponse)
def tender(request: Request, package: str | None = None) -> HTMLResponse:
    from gw.agents.tender import signoffs

    svc = service(request)
    pkgs = [w for w in svc.store.find(m.WorkPackage, order_by="code") if w.tender_state != "none"]
    cur = next((w for w in pkgs if w.code == package), pkgs[0] if pkgs else None)
    check = bids = when = None
    sign: list[dict[str, Any]] = []
    if cur is not None:
        check, bids, when = _latest_tender(svc, cur.code)
        sign = signoffs(svc.store, cur)
    unlocked = len({s["role"] for s in sign}) >= 2
    missing = len(check["missing"]) if check else 0
    ctx = chrome(request, "GW-320", "Tender check", "tender", "Tender agent",
                 ("For decision", "amber") if missing or not unlocked else ("Checked", "green"),
                 f"<b>Tenders</b> | {len(pkgs)} packages in a tender state" +
                 (f" | {cur.code} {cur.name}" if cur else ""))
    ctx.update(pkgs=pkgs, cur=cur, check=check, bids=bids, when=when, sign=sign, unlocked=unlocked)
    return render("tender.html", ctx)


@router.post("/tender/{code}/run")
def tender_run(request: Request, code: str) -> Response:
    svc, user = service(request), principal(request)
    try:
        res = svc.tender_check(user, code)
    except GwError as e:
        return back(request, f"/tender?package={code}", err=_problem(e))
    return back(request, f"/tender?package={code}", msg=f"{code}: {len(res['check']['missing'])} sections missing, "
                                                         f"{len(res['bids']['bids'])} bids compared")


@router.post("/tender/{code}/signoff")
def tender_signoff(request: Request, code: str) -> Response:
    svc, user = service(request), principal(request)
    try:
        svc.tender_signoff(user, code)
        svc.tender_check(user, code)
    except GwError as e:
        return back(request, f"/tender?package={code}", err=_problem(e))
    return back(request, f"/tender?package={code}", msg=f"{code}: technical sign-off recorded by {user.name}")


# --- GW-400 dependencies -----------------------------------------------------------------------------------


def _dep_drawing(deps: list[dict[str, Any]], packages: dict[str, m.WorkPackage], codes: dict[str, str]) -> dict[str, Any]:
    """Boxes per work package grouped in project columns; edges solid (accepted) or dashed (proposed)."""
    shown = [d for d in deps if d["status"] in ("proposed", "accepted")][:24]
    wp_ids: list[str] = []
    for d in shown:
        for k in ("from_work_package_id", "to_work_package_id"):
            if d[k] not in wp_ids and d[k] in packages:
                wp_ids.append(d[k])
    by_project: dict[str, list[str]] = {}
    for w in wp_ids:
        by_project.setdefault(codes.get(packages[w].project_id, "?"), []).append(w)
    cols = sorted(by_project)
    pos: dict[str, tuple[int, int]] = {}
    bw, bh, gx, gy = 150, 46, 60, 26
    for ci, code in enumerate(cols):
        for ri, w in enumerate(by_project[code]):
            pos[w] = (20 + ci * (bw + gx), 40 + ri * (bh + gy))
    boxes = [{"x": x, "y": y, "w": bw, "h": bh, "code": packages[w].code, "name": packages[w].name[:24]}
             for w, (x, y) in pos.items()]
    edges = []
    for d in shown:
        a, b = pos.get(d["from_work_package_id"]), pos.get(d["to_work_package_id"])
        if not a or not b:
            continue
        edges.append({"x1": a[0] + bw / 2, "y1": a[1] + bh / 2, "x2": b[0] + bw / 2, "y2": b[1] + bh / 2,
                      "proposed": d["status"] == "proposed", "kind": d["kind"]})
    rows = max((len(v) for v in by_project.values()), default=1)
    return {"boxes": boxes, "edges": edges, "cols": [(20 + i * (bw + gx), c) for i, c in enumerate(cols)],
            "width": max(640, 20 + len(cols) * (bw + gx)), "height": 70 + rows * (bh + gy),
            "n_projects": len(cols), "n_packages": len(boxes)}


@router.get("/deps", response_class=HTMLResponse)
def deps(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    all_deps, err = _safe(lambda: svc.deps(user), [])
    packages = {w.id: w for w in svc.store.all(m.WorkPackage)}
    codes = {p.id: p.code for p in svc.store.all(m.Project)}
    proposed = [d for d in all_deps if d["status"] == "proposed"]
    accepted = [d for d in all_deps if d["status"] == "accepted"]
    ctx = chrome(request, "GW-400", "Interdependency map", "deps", "Scout agent",
                 ("Proposed", "blue") if proposed else ("Checked", "blue"),
                 f"<b>Interdependencies</b> | portfolio | {len(proposed)} proposed | {len(accepted)} accepted")
    ctx.update(proposed=proposed, accepted=accepted, rejected=[d for d in all_deps if d["status"] == "rejected"][:10],
               drawing=_dep_drawing(all_deps, packages, codes), err=ctx["err"] or err)
    return render("deps.html", ctx)


@router.post("/deps/{dep}/decide")
def deps_decide(request: Request, dep: str, accept: str = Form(...), reason: str = Form("")) -> Response:
    svc, user = service(request), principal(request)
    try:
        svc.deps_decide(user, dep, accept == "1", reason)
    except GwError as e:
        return back(request, "/deps", err=_problem(e))
    return back(request, "/deps", msg=f"{dep} {'accepted' if accept == '1' else 'rejected'}")


# --- GW-500 forecast ----------------------------------------------------------------------------------


@router.get("/forecast", response_class=HTMLResponse)
def forecast(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    rows, err = _safe(lambda: svc.forecast_slip(user), [])
    cal = svc.calibration()
    from gw.engines.forecast.engine import latest

    gates = latest(svc.store, "gate_pass_on_date")
    gis = {g.id: g for g in svc.store.find(m.GateInstance, decision="pending")}
    codes = {p.id: p.code for p in svc.store.all(m.Project)}
    gate_rows: list[dict[str, Any]] = sorted([{"code": codes.get(gis[k].project_id, "?"), "gate": gis[k].name, "p": r.p,
                         "review": gis[k].planned_review_at} for k, r in gates.items() if k in gis], key=lambda x: float(x["p"]))
    now = clock.now()
    due30 = [r for r in rows if (_dt(r["due"]) or now) <= now + timedelta(days=30)]
    model = rows[0]["model"] if rows else None
    degraded = next((s for s in svc.store.latest_signals("forecast_degraded", "portfolio")), None)
    ctx = chrome(request, "GW-500", "Forecast", "forecast", "Prediction engine", ("Advisory", "blue"),
                 f"<b>Forecast</b> | {len(rows)} open commitments | {len(gate_rows)} gate instances")
    ctx.update(rows=rows[:30], n_rows=len(rows), above=sum(1 for r in rows if r["p_slip"] > 0.5), due30=len(due30),
               cal=cal, gate_rows=gate_rows, model=model, degraded=degraded,
               flag=float(svc.policies().get("forecast.flag_above", 0.35)), err=ctx["err"] or err)
    return render("forecast.html", ctx)


@router.post("/forecast/whatif/{commitment}", response_class=HTMLResponse)
def forecast_whatif(request: Request, commitment: str, lever: str = Form("escalate_today")) -> HTMLResponse:
    svc, user = service(request), principal(request)
    try:
        r = svc.forecast_whatif(user, commitment, [lever])
        text = f"{r['before']:.2f} → {r['after']:.2f} ({r['delta']:+.2f}) if {lever.replace('_', ' ')}"
    except GwError as e:
        text = _problem(e)
    return HTMLResponse(f'<span class="whatif">{escape(text)}</span>')


# --- GW-600 harness ------------------------------------------------------------------------------------


@router.get("/harness", response_class=HTMLResponse)
def harness(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    conns, err = _safe(lambda: svc.harness_list(user), [])
    healthy = sum(1 for c in conns if c["health"] == "ok")
    bad = len(conns) - healthy
    ctx = chrome(request, "GW-600", "Harness | connections", "harness", "Harness registry",
                 (f"{bad} degraded", "amber") if bad else ("Issued", "green"),
                 f"<b>Harness</b> | {len(conns)} connections | {healthy} healthy | {bad} degraded or down")
    ctx.update(conns=conns, tools=MCP_TOOLS, err=ctx["err"] or err)
    return render("harness.html", ctx)


@router.post("/harness/{conn}/test")
def harness_test(request: Request, conn: str) -> Response:
    svc, user = service(request), principal(request)
    try:
        h = svc.harness_test(user, conn)
    except GwError as e:
        return back(request, "/harness", err=_problem(e))
    return back(request, "/harness", msg=f"{conn}: {h['status']} | {h.get('detail', '')}")


@router.post("/harness/{conn}/sync")
def harness_sync(request: Request, conn: str) -> Response:
    svc, user = service(request), principal(request)
    try:
        res = svc.harness_sync(user, None if conn == "all" else conn)
    except GwError as e:
        return back(request, "/harness", err=_problem(e))
    pulled = sum(int(v.get("pulled", 0)) for v in res.values())
    return back(request, "/harness", msg=f"synced {len(res)} connection(s) | {pulled} items pulled")


@router.post("/harness/add-live")
def harness_add_live(request: Request, kind: str = Form(...), conn_id: str = Form(...), tenant: str = Form(...),
                     client_id: str = Form(...), mailbox: str = Form(""), root_path: str = Form("Gatewright demo")) -> Response:
    """GW-600 add connection: identifiers only, no secret field."""
    svc, user = service(request), principal(request)
    try:
        svc.harness_add_live(user, kind, conn_id, tenant, client_id, mailbox, root_path)
    except GwError as e:
        return back(request, "/harness", err=_problem(e))
    return back(request, "/harness", msg=f"{conn_id} added - sign in to activate it")


def _logins(request: Request) -> dict[str, dict[str, Any]]:
    if not hasattr(request.app.state, "device_logins"):
        request.app.state.device_logins = {}
    logins: dict[str, dict[str, Any]] = request.app.state.device_logins
    return logins


@router.post("/harness/{conn}/login", response_class=HTMLResponse)
def harness_login_start(request: Request, conn: str) -> HTMLResponse:
    """Start a device-code sign-in; a background thread waits for the person to finish it."""
    import threading

    svc, user = service(request), principal(request)
    try:
        if not ({"admin", "portfolio_lead"} & set(user.roles)):
            raise GwError(f"{conn}: signing in needs admin or portfolio_lead")
        g = svc.harness_graph(conn)
        flow = g.start_device_flow()
    except (GwError, RuntimeError) as e:
        return HTMLResponse(f'<div class="msg err">{escape(_problem(e) if isinstance(e, GwError) else str(e))}</div>')
    state: dict[str, Any] = {"status": "waiting", "code": flow["user_code"], "url": flow["verification_uri"],
                             "user": user.id}
    _logins(request)[conn] = state

    def wait() -> None:
        try:
            g.finish_device_flow(flow)
            svc.store.log(f"user:{user.id}", "Connection", conn, "signed_in", {})
            state["status"] = "done"
        except Exception as e:  # expired, declined or wrong account
            state.update(status="error", error=str(e)[:200])

    threading.Thread(target=wait, daemon=True).start()
    return _login_status_html(conn, state)


@router.get("/harness/{conn}/login", response_class=HTMLResponse)
def harness_login_status(request: Request, conn: str) -> HTMLResponse:
    state = _logins(request).get(conn, {"status": "none"})
    if state.get("user") and state["user"] != principal(request).id:
        return HTMLResponse("")  # the device code belongs to whoever started the sign-in
    return _login_status_html(conn, state)


def _login_status_html(conn: str, state: dict[str, Any]) -> HTMLResponse:
    conn = str(escape(conn))
    url = str(state.get("url", ""))
    state = {**state, "url": escape(url if url.startswith("https://") else ""), "code": escape(state.get("code", "")),
             "error": escape(state.get("error", "sign-in failed"))}
    if state["status"] == "waiting":
        return HTMLResponse(
            f'<div class="msg" hx-get="/harness/{conn}/login" hx-trigger="every 3s" hx-swap="outerHTML">'
            f'<div class="h">{conn} | device sign-in | waiting</div>'
            f'Open <a href="{state["url"]}" target="_blank" rel="noopener">{state["url"]}</a> and enter '
            f'<b class="mono">{state["code"]}</b>. Sign in as the masked demo account only. '
            f'The token is kept encrypted outside the repository.</div>')
    if state["status"] == "done":
        return HTMLResponse(f'<div class="msg"><span class="stamp green flat">Signed in</span> {conn} is active. '
                            f'<a href="/harness">Reload</a> and test it.</div>')
    if state["status"] == "error":
        return HTMLResponse(f'<div class="msg err">{conn}: {state.get("error", "sign-in failed")}</div>')
    return HTMLResponse("")


@router.post("/harness/test-all")
def harness_test_all(request: Request) -> Response:
    svc, user = service(request), principal(request)
    out = []
    try:
        for c in svc.harness_list(user):
            out.append(f"{c['id']} {svc.harness_test(user, c['id'])['status']}")
    except GwError as e:
        return back(request, "/harness", err=_problem(e))
    return back(request, "/harness", msg=" | ".join(out))


# --- GW-700 policies --------------------------------------------------------------------------------------

SECTIONS = ["chasing", "escalation", "agents", "gates", "signals", "exposure", "forecast", "semantic", "calendars",
            "channels", "retention", "readiness_score"]


@router.get("/policies", response_class=HTMLResponse)
def policies(request: Request, section: str = "chasing") -> HTMLResponse:
    from gw.policies.store import current_revision

    svc, user = service(request), principal(request)
    if section not in SECTIONS:
        section = "chasing"
    pol, err = _safe(lambda: svc.policy_get(user), {"rev": 0, "policies": {}})
    rev = current_revision(svc.store)
    users = {u.id: u.name for u in svc.store.all(m.User)}
    author = rev.author.split(":", 1)[-1]
    fields = _flatten((pol["policies"] or {}).get(section) or {})
    draft = request.query_params.get("draft")
    ctx = chrome(request, "GW-700", "Policies | configuration", "policies", users.get(author, author),
                 ("Issued", "green"),
                 f"<b>Policies</b> | rev {rev.rev} | changed {ddmmm(rev.created_at)} by {users.get(author, author)}")
    ctx.update(sections=SECTIONS, section=section, fields=fields, rev=rev, yaml_text=draft or rev.yaml_text,
               readiness_def=(pol["policies"] or {}).get("readiness_score") or {}, err=ctx["err"] or err)
    return render("policies.html", ctx)


def _flatten(d: Any, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v and all(not isinstance(x, (dict, list)) for x in v.values()) and len(v) <= 8:
                out.append((key, " | ".join(f"{a}: {b}" for a, b in v.items())))
            elif isinstance(v, dict) and v:
                out.extend(_flatten(v, key))
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                for i, item in enumerate(v):
                    out.append((f"{key}[{i}]", " | ".join(f"{a}: {b}" for a, b in item.items())))
            else:
                out.append((key, ", ".join(map(str, v)) if isinstance(v, list) else ("{}" if v == {} else str(v))))
    return out


@router.post("/policies")
def policies_save(request: Request, yaml_text: str = Form(...), note: str = Form(""), action: str = Form("save")) -> Response:
    svc, user = service(request), principal(request)
    if action == "diff":
        from difflib import unified_diff

        from gw.policies.store import current_revision

        cur = current_revision(svc.store)
        diff = "".join(unified_diff(cur.yaml_text.splitlines(True), yaml_text.splitlines(True), f"rev {cur.rev}", "draft"))
        return HTMLResponse(f'<pre>{_esc(diff) or "no changes"}</pre>')
    try:
        res = svc.policy_set(user, yaml_text, note)
    except GwError as e:
        resp = back(request, "/policies", err=_problem(e))
        return resp
    return back(request, "/policies", msg=f"saved rev {res['rev']}")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- GW-010 brief ---------------------------------------------------------------------------------------


@router.get("/brief", response_class=HTMLResponse)
def brief(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    b, err = _safe(lambda: _with_links(svc, svc.brief(user)), None)
    stamp_colour = {"FOR DECISION": "amber", "APPROVAL": "amber", "DEADLINE": "red", "PROPOSED": "blue"}
    ctx = chrome(request, "GW-010", "Morning brief", "brief", "Brief agent",
                 ("For decision", "amber") if b and b.get("items") else ("Checked", "blue"),
                 f"<b>Morning brief</b> | {clock.now():%a %d %b %Y} | {clock.now():%H:%M} | for {user.name}")
    ctx.update(b=b, stamp_colour=stamp_colour, err=ctx["err"] or err)
    return render("brief.html", ctx)


# --- agents ----------------------------------------------------------------------------------------------


@router.get("/agents", response_class=HTMLResponse)
def agents(request: Request) -> HTMLResponse:
    svc, user = service(request), principal(request)
    runs = svc.store.find(m.AgentRun, order_by="-started_at", limit=100)
    approvals = svc.approval_list(user)
    ctx = chrome(request, "GW-900", "Agent runs and approvals", "agents", "Runner | audit log",
                 ("For decision", "amber") if approvals else ("Checked", "blue"),
                 f"<b>Agents</b> | {len(runs)} recent runs | {len(approvals)} approvals pending")
    ctx.update(runs=runs, approvals=approvals)
    return render("agents.html", ctx)


@router.get("/healthz-web", include_in_schema=False)
def web_health(request: Request) -> dict[str, Any]:
    return {"ok": True, "sheets": [n[2] for n in NAV + NAV_SYSTEM]}



# --- sign in (login mode) ---------------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in | Gatewright</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Architects+Daughter&family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/static/gw.css"></head><body><div class="app" style="max-width:520px"><div class="zsheet">
<div class="card"><div class="vt"><div class="bub" aria-hidden="true"><span>000</span><span>GW</span></div><div><h1>Sign in</h1>
<div class="vt-meta"><span>Gatewright | <a href="https://www.automatiqa.io/gatewright/" rel="noopener">Automatiqa Lab</a></span></div></div></div>{err}
<form method="post" action="/login" class="form">
<input type="hidden" name="next" value="{next}">
<div class="field"><label for="email">Email</label><div class="ctl"><input class="inp" id="email" name="email" autocomplete="username" required></div></div>
<div class="field"><label for="password">Password</label><div class="ctl"><input class="inp" id="password" name="password" type="password" autocomplete="current-password" required></div></div>
<div class="field"><label></label><div class="ctl"><button class="btn" type="submit">Sign in</button></div></div>
</form></div></div></div></body></html>"""


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", err: str = "") -> HTMLResponse:
    from html import escape

    msg = f'<p class="msg err">{escape(err)}</p>' if err else ""
    target = local_path(next)
    idps = auth.oidc_connections(service(request).store)
    sso = "".join(f'<p><a class="btn" href="/login/oidc/{escape(c.id)}?next={escape(quote(target))}">Sign in with '
                  f'{escape(c.name)}</a></p>' for c in idps)
    return HTMLResponse(LOGIN_PAGE.format(err=msg + sso, next=escape(target)))


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")) -> Response:
    svc = service(request)
    user = svc.store.find_one(m.User, email=email.strip().lower()) or svc.store.find_one(m.User, short=email.strip())
    target = local_path(next)
    client = request.client.host if request.client else "?"
    if not auth.login_allowed(client, email):
        return RedirectResponse(local_path(f"/login?err={quote('too many attempts; wait 15 minutes')}"
                                           f"&next={quote(target, safe='')}"), status_code=303)
    ok = auth.verify_password(user, password) if user is not None else auth.verify_dummy(password)
    auth.login_attempt(client, email, ok)
    if user is None or not ok:
        svc.store.log("system:auth", "User", user.id if user else "unknown", "login_failed", {})
        return RedirectResponse(local_path(f"/login?err={quote('wrong email or password')}&next={quote(target, safe='')}"),
                                status_code=303)
    svc.store.log(f"user:{user.id}", "User", user.id, "login", {})
    resp = RedirectResponse(local_path(target), status_code=303)
    secure = auth.secure_cookies(request)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user.id, store=svc.store), samesite="lax", httponly=True,
                    max_age=auth.SESSION_HOURS * 3600, secure=secure)
    resp.set_cookie(auth.CSRF_COOKIE, auth.new_csrf(), samesite="strict", httponly=False, secure=secure)
    return resp


@router.post("/logout")
def logout(request: Request) -> Response:
    svc = service(request)
    uid = auth.read_session(request.cookies.get(auth.SESSION_COOKIE), svc.store)
    if uid:
        auth.revoke_sessions(svc.store, uid)  # every session of this user ends, not only this cookie
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    resp.delete_cookie(auth.CSRF_COOKIE)
    return resp
