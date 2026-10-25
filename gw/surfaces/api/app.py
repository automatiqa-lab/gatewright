"""FastAPI app: REST /api/v1, web sheets, /healthz, /metrics, scheduler.

Every route is a thin wrapper over the service layer. Errors are problem+json:
Forbidden names the hard limit, Conflict lists allowed transitions, 422 carries a
JSON pointer.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from gw.errors import GwError
from gw.record import clock
from gw.record import models as m
from gw.surfaces.principal import principal, service
from gw.surfaces.service import Service

log = logging.getLogger("gw.api")
api = APIRouter(prefix="/api/v1")
Svc = Depends(service)
User = Depends(principal)


@api.get("/portfolio")
def portfolio(zone: str | None = None, gate: str | None = None, at_risk_only: bool = False, svc: Service = Svc,
              user: m.User = User) -> Any:
    return svc.portfolio_list(user, zone, gate, at_risk_only)


@api.get("/projects/{code}")
def project(code: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.project_get(user, code)


@api.get("/projects/{code}/gates/{gate}/score")
def gate_score(code: str, gate: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.gate_score(user, code, gate)


@api.get("/projects/{code}/gates/{gate}/gaps")
def gate_gaps(code: str, gate: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.gate_gaps(user, code, gate)


@api.get("/projects/{code}/gates/{gate}/pack")
def gate_pack(code: str, gate: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.gate_pack(user, code, gate)


@api.post("/projects/{code}/gates/{gate}/decision")
def gate_decide(code: str, gate: str, body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.gate_decide(user, code, gate, body["decision"], body.get("note", ""))


@api.get("/commitments")
def commitments(project: str | None = None, party: str | None = None, status: str | None = None,
                due_within_days: int | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.commitment_list(user, project, party, status, due_within_days)


@api.post("/commitments/{ref}/chase")
def chase(ref: str, level: int | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.commitment_chase(user, ref, level)


@api.post("/commitments/{ref}/status")
def set_status(ref: str, body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.commitment_set_status(user, ref, body["status"], body.get("new_due"), body.get("reason"))


@api.post("/evidence")
def evidence_link(body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.evidence_link(user, body["commitment"], body["uri"], body.get("title"), conn_id=body.get("connection"))


@api.post("/evidence/{evidence_id}/accept")
def evidence_accept(evidence_id: str, body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.evidence_accept(user, evidence_id, bool(body.get("accept", True)), body.get("reason"))


@api.get("/chase/status")
def chase_status(project: str | None = None, party: str | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.chase_status(user, project, party)


@api.get("/signals")
def signals(signal: str, scope: str, scope_id: str | None = None, history_days: int | None = None,
            svc: Service = Svc, user: m.User = User) -> Any:
    return svc.signal_query(user, signal, scope, scope_id, history_days)


@api.get("/heatmap")
def heatmap(mode: str = "exposure", svc: Service = Svc, user: m.User = User) -> Any:
    return svc.heatmap(user, mode)


@api.get("/forecast/slip")
def forecast_slip(project: str | None = None, commitment: str | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.forecast_slip(user, project, commitment)


@api.get("/forecast/gate")
def forecast_gate(project: str, gate: str | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.forecast_gate(user, project, gate)


@api.post("/forecast/whatif")
def forecast_whatif(body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.forecast_whatif(user, body["target"], list(body.get("levers", [])))


@api.get("/forecast/explain/{target}")
def forecast_explain(target: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.forecast_explain(user, target)


@api.get("/packages/{code}/tender")
def tender_check(code: str, svc: Service = Svc, user: m.User = User) -> Any:
    """gw.tender.check over REST."""
    return svc.tender_check(user, code)


@api.post("/replies/parse")
def reply_parse(body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    """gw.reply.parse over REST: intent, entities, candidate commitment; no side effect."""
    return svc.reply_parse(user, str(body.get("text", "")), body.get("thread_ref"))


@api.post("/semantic/classify")
def classify(body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    if body.get("kind", "message") == "document" or body.get("name"):
        return svc.doc_classify(user, body.get("text", ""), body.get("name", ""))
    return svc.reply_parse(user, body.get("text", ""), body.get("thread_ref"))


@api.get("/search")
def search(q: str, k: int = 10, sources: list[str] | None = Query(None), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.search(user, q, k, sources)


@api.get("/deps")
def deps(project: str | None = None, status: str | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.deps(user, project, status)


@api.post("/deps/{dep_id}")
def deps_decide(dep_id: str, body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.deps_decide(user, dep_id, bool(body["accept"]), body.get("reason", ""))


@api.get("/policies")
def policies(section: str | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.policy_get(user, section)


@api.put("/policies")
async def policies_put(request: Request, note: str = "", svc: Service = Svc, user: m.User = User) -> Any:
    text = (await request.body()).decode("utf-8")
    return svc.policy_set(user, text, note)


@api.get("/approvals")
def approvals(mine: bool = False, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.approval_list(user, mine)


@api.post("/ask")
def ask(body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    """gw.ask: the assistant answers with the record's tools; the answer carries the Art. 50 envelope."""
    return svc.ask(user, str(body.get("question", "")), body.get("conversation"))


@api.post("/approvals/{approval_id}")
def approval_decide(approval_id: str, body: dict[str, Any] = Body(...), svc: Service = Svc, user: m.User = User) -> Any:
    return svc.approval_decide(user, approval_id, body["decision"], body.get("edited_output"))


@api.post("/cards/{approval_id}")
def card_callback(approval_id: str, body: dict[str, Any] = Body(...), svc: Service = Svc) -> Any:
    """Teams / Slack card button: signed, single-use token bound to the approval and the user."""
    from gw.errors import Forbidden
    from gw.surfaces.webhooks import verify_card_token

    uid = verify_card_token(approval_id, str(body.get("token", "")))
    if uid is None:
        raise Forbidden("card token invalid", limit="card_token")
    action = str(body.get("action", "hold")).lower()
    decision = {"approve": "approved", "edit": "approved_edited", "hold": "hold", "reject": "rejected"}.get(action, "hold")
    user = svc.store.get(m.User, uid)
    return svc.approval_decide(user, approval_id, decision, body.get("edited_output"))


@api.post("/cards/chase/{chase_id}")
def chase_card_callback(chase_id: str, body: dict[str, Any] = Body(...), svc: Service = Svc) -> Any:
    """CONFIRM / FLAG button on a chase card; token bound to the chase."""
    return svc.chase_card_reply(chase_id, str(body.get("token", "")), str(body.get("action", "")),
                                body.get("new_date"), body.get("reason"))


@api.get("/audit")
def audit(entity: str | None = None, agent: str | None = None, user_key: str | None = Query(None, alias="user"),
          since_days: int = 7, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.audit_query(user, entity, agent, user_key, since_days)


@api.get("/harness")
def harness(svc: Service = Svc, user: m.User = User) -> Any:
    return svc.harness_list(user)


@api.post("/harness/{conn_id}/test")
def harness_test(conn_id: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.harness_test(user, conn_id)


@api.post("/harness/sync")
def harness_sync(connection: str | None = None, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.harness_sync(user, connection)


@api.get("/reports/{period}")
def report(period: str, svc: Service = Svc, user: m.User = User) -> Any:
    return svc.report(period, user)


@api.get("/brief")
def brief(svc: Service = Svc, user: m.User = User) -> Any:
    return svc.brief(user)


def healthz(request: Request) -> Any:
    svc = service(request)
    conns = svc.registry.list()
    status = "ok" if all(c.health != "down" for c in conns) else "degraded"
    try:  # anonymous probes learn up or down only; connection ids are for signed-in callers
        from gw.surfaces.principal import principal

        principal(request)
    except Exception:
        return {"status": status}
    return {"status": status, "today": clock.now().isoformat(), "connections": [
                {"id": c.id, "contract": c.contract, "health": c.health,
                 "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None} for c in conns]}


def metrics(request: Request) -> PlainTextResponse:
    svc = service(request)
    st = svc.store
    cal = svc.calibration()
    lines = [
        f'gw_syncs_total {sum(1 for e in st.events(entity_type="Connection", limit=100000) if e["kind"] == "synced")}',
        f"gw_items_classified_total {sum(1 for i in st.all(m.InboundItem) if i.processed_at and (i.classification or {}).get('source') not in ('history', 'case'))}",
        f"gw_chases_sent_total {st.count(m.Chase, status='sent')}",
        f"gw_approvals_pending {st.count(m.Approval, decision='pending')}",
        f"gw_review_queue_depth {st.count(m.InboundItem, review_state='pending')}",
        f"gw_forecast_ece {cal['ece'] if cal else 'NaN'}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


def _scheduler(svc: Service, lock: threading.Lock | None = None) -> Any:
    """APScheduler inside the process (solo mode). Times from policies.agents.schedules."""
    from apscheduler.schedulers.background import BackgroundScheduler

    pol = svc.policies()
    sched = BackgroundScheduler(timezone="UTC")
    lock = lock or threading.Lock()

    def job(fn: Any) -> Any:
        def run() -> None:
            with lock:
                try:
                    fn()
                except Exception:
                    log.exception("scheduled job failed")
        return run

    def hm(key: str, default: str) -> dict[str, int]:
        val = str(pol.get(f"agents.schedules.{key}", default)).split()[-1]
        h, mi = val.split(":")
        return {"hour": int(h), "minute": int(mi)}

    every = {k: int(v) for k, v in (pol.get("agents.intervals") or {}).items()}
    sched.add_job(job(svc.registry.sync_all), "interval", minutes=every.get("sync", 15), id="harness.sync")
    sched.add_job(job(lambda: svc.process_inbound("schedule")), "interval", minutes=every.get("semantic", 15),
                  id="semantic")
    sched.add_job(job(svc.recompute_signals), "cron", **hm("signals", "06:00"), id="signals.full")
    sched.add_job(job(lambda: svc.run_gatekeeper(trigger="schedule")), "cron", **hm("gatekeeper", "06:05"), id="gatekeeper")
    sched.add_job(job(lambda: svc.run_scout("schedule")), "cron", **hm("scout", "06:08"), id="scout")
    sched.add_job(job(lambda: svc.run_forecast("schedule")), "cron", **hm("forecast", "06:10"), id="forecast")
    sched.add_job(job(lambda: svc.run_brief(None, "schedule")), "cron", **hm("brief", "06:30"), id="brief")
    sched.add_job(job(lambda: svc.run_chaser("schedule")), "interval", minutes=every.get("chaser", 60), id="chaser")
    sched.add_job(job(lambda: svc.run_reporter("weekly", "schedule")), "cron", day_of_week="mon", hour=7, id="reporter")
    sched.add_job(job(svc.retention_purge), "cron", **hm("retention", "03:00"), id="retention")
    for spec in scheduled_jobs(pol):
        sched.add_job(job(_JOB_CALLS[spec["id"]](svc)), "cron", id=spec["id"], **spec["cron"])
    return sched


# Maintenance jobs driven by policies.
_JOB_CALLS: dict[str, Any] = {
    "semantic.retrain": lambda svc: svc.semantic_retrain,
    "forecast.calibrate": lambda svc: svc.forecast_calibrate,
    "reporter.monthly": lambda svc: (lambda: svc.run_reporter_monthly_if_due("schedule")),
    "governance.nightly": lambda svc: svc.nightly_governance,
}


def scheduled_jobs(pol: Any) -> list[dict[str, Any]]:
    """The policy-dependent cron jobs; pure, so tests can check them without starting a scheduler."""
    jobs: list[dict[str, Any]] = []
    retrain = str(pol.get("semantic.retrain", "weekly"))
    if retrain == "weekly":
        jobs.append({"id": "semantic.retrain", "cron": {"day_of_week": "sun", "hour": 2, "minute": 0}})
    elif retrain == "daily":
        jobs.append({"id": "semantic.retrain", "cron": {"hour": 2, "minute": 0}})
    if pol.get("forecast.calibrate_weekly", True):
        jobs.append({"id": "forecast.calibrate", "cron": {"day_of_week": "sun", "hour": 2, "minute": 30}})
    # first business day: run daily at 07:00 on days 1-7 and let the service decide (holiday-aware)
    jobs.append({"id": "reporter.monthly", "cron": {"day": "1-7", "hour": 7, "minute": 0}})
    jobs.append({"id": "governance.nightly", "cron": {"hour": 2, "minute": 45}})
    return jobs


def create_app(db: str = "gw.duckdb", scheduler: bool = False, mcp_http: bool = False,
               service_obj: Service | None = None) -> FastAPI:
    """Build the app over one database. The scheduler is off in tests."""
    svc = service_obj or Service.open(db)

    mcp_server = None
    if mcp_http:
        from gw.surfaces.mcp.server import build_server

        mcp_server = build_server(service=svc, http=True)
        mcp_asgi = mcp_server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        lock = threading.Lock()  # one agent run at a time across schedule and real-time reactions
        sched = _scheduler(svc, lock) if scheduler else None
        reactor = None
        if sched:
            sched.start()
            from gw.surfaces.realtime import attach

            reactor = attach(svc, lock)
            reactor.start()
        try:
            if mcp_server is not None:
                async with mcp_server.session_manager.run():
                    yield
            else:
                yield
        finally:
            if sched:
                sched.shutdown(wait=False)
            if reactor is not None:
                reactor.stop()

    app = FastAPI(title="Gatewright", version="0.1.0", lifespan=lifespan,
                  description="AI PMO harness for capital project delivery. Every route mirrors a gw.* MCP tool.")
    app.state.svc = svc

    @app.exception_handler(GwError)
    async def problem(request: Request, exc: GwError) -> JSONResponse:
        return JSONResponse(exc.problem(), status_code=exc.status, media_type="application/problem+json")

    from gw.surfaces.api.security import SecurityMiddleware
    from gw.surfaces.principal import Unauthenticated

    app.add_middleware(SecurityMiddleware, store_getter=lambda: svc.store, registry_getter=lambda: svc.registry)
    from gw.surfaces.api.disclosure_mw import DisclosureMiddleware

    app.add_middleware(DisclosureMiddleware)

    @app.exception_handler(Unauthenticated)
    async def unauth(request: Request, exc: Unauthenticated) -> Any:
        from fastapi.responses import RedirectResponse

        if request.url.path.startswith("/api/"):
            return JSONResponse(exc.problem(), status_code=401, media_type="application/problem+json")
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    app.include_router(api)
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/metrics", metrics, methods=["GET"])
    try:
        from gw.surfaces.web.oidc_routes import router as oidc_router
        from gw.surfaces.web.routes import router as web_router

        app.include_router(oidc_router)
        from gw.surfaces.web.admin_routes import router as admin_router

        app.include_router(admin_router)
        from gw.surfaces.web.detail_routes import router as detail_router

        app.include_router(detail_router)
        from gw.surfaces.web.assistant_routes import router as assistant_router

        app.include_router(assistant_router)
        app.include_router(web_router)
    except ImportError:
        log.warning("web sheets not available")
    static = Path(__file__).resolve().parents[1] / "web" / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")
    if mcp_server is not None:
        app.mount("/mcp", mcp_asgi)
    return app
