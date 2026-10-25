"""GW-305 /commitments/{code}: one commitment, its chases, evidence, forecast and audit trail.

Read-only: chasing,
approvals and what-ifs stay on their own sheets, linked from here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from gw.engines.forecast.engine import latest
from gw.record import models as m
from gw.surfaces.principal import service
from gw.surfaces.web.links import Linker
from gw.surfaces.web.routes import back, chrome, render

router = APIRouter()


@router.get("/commitments/{code}", response_class=HTMLResponse)
def commitment(request: Request, code: str) -> Response:
    svc = service(request)
    c = svc.store.find_one(m.Commitment, code=code)
    if c is None:
        return back(request, "/chasers", err=f"{code} not found")
    link = Linker(svc.store)
    project = svc.store.find_one(m.Project, id=c.project_id)
    party = svc.store.find_one(m.Party, id=c.party_id)
    owner = svc.store.find_one(m.User, id=c.owner_user_id)
    gate = svc.store.find_one(m.GateInstance, id=c.gate_instance_id) if c.gate_instance_id else None
    chases = svc.store.find(m.Chase, commitment_id=c.id, order_by="-planned_for")
    evidence = svc.store.find(m.Evidence, commitment_id=c.id)
    inbound = svc.store.find(m.InboundItem, commitment_id=c.id, order_by="-received_at", limit=20)
    slip = latest(svc.store, "slip").get(c.id)
    events = svc.store.events(entity_id=c.id, limit=100)
    status_colour = {"confirmed": "green", "flagged": "amber", "closed": "blue"}.get(str(c.status), "")
    ctx = chrome(request, "GW-305", f"{c.code} | {c.title}", "chasers", "Record | event log",
                 ("Checked", "blue"), f"<b>Commitment</b> | {c.code} | {project.code if project else ''}")
    ctx.update(c=c, project=project, party=party, owner=owner, gate=gate, chases=chases, evidence=evidence,
               inbound=inbound, slip=slip, events=events, status_colour=status_colour,
               gate_url=link.url(gate.id, "GateInstance") if gate else None)
    return render("commitment.html", ctx)
