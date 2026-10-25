"""GW-020 /ask (the assistant) and /live (what changed while a sheet was open).

/ask keeps one conversation per `c` id; every answer is an AgentRun with its tool calls and the
proposals a person can act on. /live is polled by every sheet every 20 s and says how many record
changes and real-time agent runs happened since the sheet was rendered, with a reload link.
"""

from __future__ import annotations

import secrets
import threading
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from gw.errors import GwError
from gw.record import models as m
from gw.surfaces.principal import principal, service
from gw.surfaces.web.routes import _problem, back, chrome, render

router = APIRouter(include_in_schema=False)
QUIET = ("system:retrieval", "system:realtime", "system:signals", "system:auth")


_JOBS: dict[str, dict[str, Any]] = {}  # user id -> the question being answered in the background
_JOBS_LOCK = threading.Lock()


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, c: str | None = None) -> HTMLResponse:
    svc, user = service(request), principal(request)
    cfg = dict(svc.policies().get("models.assistant", {}) or {})
    conn_id = cfg.get("connection")
    conn = svc.store.find_one(m.Connection, id=conn_id) if conn_id else None
    conv = c if c and c.isalnum() and len(c) <= 32 else None
    turns = svc.answers(user, conv) if conv else []
    recent = list(reversed(svc.answers(user, limit=8)))
    with _JOBS_LOCK:
        job = dict(_JOBS.get(user.id) or {})
    ctx = chrome(request, "GW-020", "Ask the record", "ask", "Assistant agent",
                 ("Advisory", "blue"), f"<b>Ask</b> | answers from the record with citations | {user.name}")
    ctx.update(turns=turns, conv=conv or secrets.token_hex(8), enabled=bool(conn), conn=conn, conn_id=conn_id,
               recent=recent, cfg=cfg, job=job if job.get("conv") == conv else None)
    return render("ask.html", ctx)


@router.post("/ask")
def ask_submit(request: Request, question: str = Form(...), c: str = Form("")) -> Response:
    """Start the answer in the background; the page polls /ask/pending until it lands."""
    svc, user = service(request), principal(request)
    conv = c if c.isalnum() and len(c) <= 32 else secrets.token_hex(8)
    with _JOBS_LOCK:
        if (_JOBS.get(user.id) or {}).get("state") == "working":
            return back(request, f"/ask?c={conv}", err="one question at a time; the last one is still being answered")
        _JOBS[user.id] = {"state": "working", "conv": conv, "question": question[:200]}

    def work() -> None:
        try:
            svc.ask(user, question, conv)
            state: dict[str, Any] = {"state": "done", "conv": conv}
        except GwError as e:
            state = {"state": "error", "conv": conv, "error": _problem(e)}
        except Exception as e:  # never leave a job spinning
            state = {"state": "error", "conv": conv, "error": f"{type(e).__name__}"}
        with _JOBS_LOCK:
            _JOBS[user.id] = state

    threading.Thread(target=work, name=f"gw-ask-{user.id}", daemon=True).start()
    return RedirectResponse(f"/ask?c={conv}", status_code=303)


@router.get("/ask/pending", response_class=HTMLResponse)
def ask_pending(request: Request) -> Response:
    user = principal(request)
    with _JOBS_LOCK:
        job = dict(_JOBS.get(user.id) or {})
        if job.get("state") in ("done", "error"):
            _JOBS.pop(user.id, None)
    if job.get("state") == "working":
        return HTMLResponse('<div class="card" id="pending" hx-get="/ask/pending" hx-trigger="every 2s" '
                            'hx-swap="outerHTML"><h4>Working on it</h4><p class="small muted">Looking things up in '
                            'the record. This page updates when the answer is ready.</p></div>')
    if job.get("state") == "error":
        return HTMLResponse(f'<div class="err" role="alert" id="pending"><b>Refused</b> | {escape(job["error"])}</div>')
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.get("/live", response_class=HTMLResponse)
def live(request: Request, since: int = 0) -> HTMLResponse:
    """The polled badge: record changes and real-time agent runs since the sheet was rendered."""
    svc = service(request)
    principal(request)
    now = svc.store.last_seq()
    if since <= 0 or now <= since:
        return HTMLResponse(f'<span id="live" hx-get="/live?since={now}" hx-trigger="every 20s" hx-swap="outerHTML">'
                            '</span>')
    changes, runs = svc.store.count_events_after(since, QUIET)
    if not changes:
        return HTMLResponse(f'<span id="live" hx-get="/live?since={since}" hx-trigger="every 20s" hx-swap="outerHTML">'
                            '</span>')
    label = f"{changes} change{'s' if changes != 1 else ''}" + (f" | {runs} agent run{'s' if runs != 1 else ''}"
                                                                 if runs else "")
    return HTMLResponse(f'<a id="live" class="tag amber" href="" hx-get="/live?since={since}" hx-trigger="every 20s" '
                        f'hx-swap="outerHTML" title="Reload to see them">{escape(label)} | reload</a>')
