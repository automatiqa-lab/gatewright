"""Turn pulled items into record facts.step 1 and
the mappings

The harness does not interpret content: messages and documents land in
`inbound_items` for the semantic engine; schedules, tasks and cost rows are
normalised facts.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from gw.harness.contracts import Activity, SyncResult, Task
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store


def ingest(registry: Any, conn: m.Connection) -> SyncResult:
    """Dispatch on the connection's contract."""
    adapter = registry.get(conn.id)
    fn = {
        "ScheduleSource": ingest_schedule,
        "PlanSource": ingest_plan,
        "DocumentSource": ingest_documents,
        "MessageChannel": ingest_messages,
        "CostSource": ingest_cost,
        "CalendarSource": ingest_calendar,
    }.get(str(conn.contract))
    if fn is None:
        return SyncResult()
    result: SyncResult = fn(registry.store, conn, adapter)
    return result


def _project_for(store: Store, conn: m.Connection, ext_id: str, ext_name: str = "") -> m.Project | None:
    pmap = (conn.mapping or {}).get("projects") or conn.config.get("projects") or {}
    code = pmap.get(ext_id)
    if code is None:
        for p in store.all(m.Project):
            if ext_id.startswith(p.code) or ext_name.startswith(p.code):
                code = p.code
                break
    return store.find_one(m.Project, code=code) if code else None


def _pmo_party(store: Store, project: m.Project) -> m.Party:
    parties = store.find(m.Party, kind=m.PartyKind.internal_team)
    for p in parties:
        if p.name.lower().startswith("pmo"):
            return p
    if parties:
        return parties[0]
    party = m.Party(id=new_id("Party"), name="PMO", kind=m.PartyKind.internal_team, country=project.country)
    store.insert(party)
    return party


def next_commitment_code(store: Store) -> str:
    n = store.count(m.Commitment)
    while store.find_one(m.Commitment, code=f"C-{n + 1:04d}"):
        n += 1
    return f"C-{n + 1:04d}"


# --- ScheduleSource ----------------------------------------------------------------


def _critical_min_float(acts: list[Activity]) -> float:
    crit = [a.total_float for a in acts if a.is_critical and a.pct < 100]
    if crit:
        return min(crit)
    open_acts = [a.total_float for a in acts if a.pct < 100]
    return min(open_acts) if open_acts else 0.0


def ingest_schedule(store: Store, conn: m.Connection, src: Any) -> SyncResult:
    """Snapshot float and DCMA per project; milestones become milestone commitments."""
    res = src.sync(conn.cursor)
    changed = 0
    errors: list[str] = []
    for ext in src.list_projects():
        project = _project_for(store, conn, ext.id, ext.name)
        if project is None:
            errors.append(f"{conn.id}: no project matches schedule {ext.id}")
            continue
        acts = src.get_activities(ext.id)
        dcma = src.dcma14(ext.id)
        sig = hashlib.sha256("|".join(f"{a.id}:{a.finish}:{a.total_float}" for a in acts).encode()).hexdigest()
        last = store.find(m.ScheduleSnapshot, project_id=project.id, conn_id=conn.id, order_by="-synced_at", limit=1)
        is_changed = not last or store.get_meta(f"schedsig:{conn.id}:{project.id}") != sig
        store.insert(m.ScheduleSnapshot(
            id=new_id("SS"), project_id=project.id, conn_id=conn.id,
            data_date=ext.data_date or clock.now(), synced_at=clock.now(),
            min_float_critical=_critical_min_float(acts), dcma_passed=dcma.passed if dcma else None,
            activity_count=len(acts), changed=is_changed))
        store.set_meta(f"schedsig:{conn.id}:{project.id}", sig)
        if project.schedule_source_conn_id != conn.id or acts:
            project.schedule_source_conn_id = conn.id
            fins = [a.finish for a in acts if a.finish]
            if fins:
                project.forecast_finish = max(fins).date()
            base = src.get_baseline(ext.id)
            if base and base.finish and project.baseline_finish is None:
                project.baseline_finish = base.finish.date()  # the record keeps its own baseline once set
            store.update(project, kind="schedule_synced", payload={"conn": conn.id})
        changed += _map_milestones(store, conn, project, src.get_milestones(ext.id))
        _map_activities_to_packages(store, conn, project, acts)
    return SyncResult(pulled=res.pulled, changed=changed, errors=errors + res.errors, cursor=res.cursor)


def _map_milestones(store: Store, conn: m.Connection, project: m.Project, milestones: list[Activity]) -> int:
    existing = {c.source_ref.external_id: c for c in store.find(m.Commitment, project_id=project.id, kind="milestone")
                if c.source_ref and c.source_ref.conn_id == conn.id}
    new: list[m.Commitment] = []
    changed = 0
    party = _pmo_party(store, project)
    for a in milestones:
        if a.finish is None:
            continue
        c = existing.get(a.id)
        if c is None:
            new.append(m.Commitment(
                id=new_id("Commitment"), code="", project_id=project.id, deliverable_id=f"ms:{a.id}",
                title=a.name, kind=m.CommitmentKind.milestone, owner_user_id=project.owner_user_id or "",
                party_id=party.id, due_at=a.finish, original_due_at=a.finish,
                status=m.CommitmentStatus.accepted if a.pct >= 100 else m.CommitmentStatus.open,
                closed_at=a.finish if a.pct >= 100 else None,
                source_ref=m.ExtRef(conn_id=conn.id, external_id=a.id), created_at=clock.now()))
        elif c.due_at != a.finish and c.status not in ("accepted", "waived", "cancelled"):
            c.due_at = a.finish
            store.update(c, kind="schedule_date", payload={"due_at": a.finish.isoformat()})
            changed += 1
    if new:
        n = store.count(m.Commitment)
        for i, c in enumerate(new):
            c.code = f"C-{n + i + 1:04d}"
        store.insert_many(new, actor=f"system:{conn.id}", kind="imported")
    return changed + len(new)


def _map_activities_to_packages(store: Store, conn: m.Connection, project: m.Project, acts: list[Activity]) -> None:
    for wp in store.find(m.WorkPackage, project_id=project.id):
        refs = [m.ExtRef(conn_id=conn.id, external_id=a.id) for a in acts if a.wbs == wp.code]
        if refs and [r.external_id for r in wp.schedule_activity_refs] != [r.external_id for r in refs]:
            wp.schedule_activity_refs = refs
            store.update(wp, kind="schedule_mapped")


# --- PlanSource ------------------------------------------------------------------------


def ingest_plan(store: Store, conn: m.Connection, src: Any) -> SyncResult:
    """Map tasks to commitments by label, bucket or rule; record the rest as unmapped."""
    res = src.sync(conn.cursor)
    mapping = conn.mapping or {}
    mapped_total = 0
    for plan in src.list_plans():
        project = _project_for(store, conn, plan.id, plan.name)
        if project is None:
            continue
        if conn.id not in project.plan_source_conn_ids:
            project.plan_source_conn_ids = [*project.plan_source_conn_ids, conn.id]
            store.update(project, kind="plan_linked", payload={"conn": conn.id})
        mapped_total += _map_tasks(store, conn, project, src.get_tasks(plan.id), mapping)
    return SyncResult(pulled=res.pulled, changed=mapped_total, errors=res.errors, cursor=res.cursor)


def _map_tasks(store: Store, conn: m.Connection, project: m.Project, tasks: list[Task], mapping: dict[str, Any]) -> int:
    gi = store.get(m.GateInstance, project.current_gate_instance_id) if project.current_gate_instance_id else None
    deliverables = {d.id: d for d in gi.deliverables} if gi else {}
    packages = {wp.code: wp for wp in store.find(m.WorkPackage, project_id=project.id)}
    commitments = store.find(m.Commitment, project_id=project.id)
    by_ref = {c.source_ref.external_id: c for c in commitments if c.source_ref and c.source_ref.conn_id == conn.id}
    gate_deliv = {c.deliverable_id: c for c in commitments
                  if c.kind == m.CommitmentKind.gate_deliverable and gi and c.gate_instance_id == gi.id}
    rules = mapping.get("rules") or []
    users = {u.email: u for u in store.all(m.User)}
    party = _pmo_party(store, project)
    mapped = 0
    new: list[m.Commitment] = []
    unmapped: list[m.InboundItem] = []
    for t in tasks:
        deliv = next((lab for lab in t.labels if lab in deliverables), None) if mapping.get("label_to_deliverable", True) else None
        wp = packages.get(t.bucket) if mapping.get("bucket_to_package", True) else None
        if deliv is None and wp is None:
            for rule in rules:
                if rule.get("contains", "").lower() in t.title.lower():
                    deliv = rule.get("deliverable")
                    wp = packages.get(rule.get("package", ""))
        if deliv is None and wp is None:
            if not store.find_one(m.InboundItem, conn_id=conn.id, external_id=t.id):
                unmapped.append(m.InboundItem(id=new_id("InboundItem"), conn_id=conn.id, external_id=t.id,
                                              kind="plan_task", received_at=clock.now(), processed_at=clock.now(),
                                              payload={"title": t.title, "bucket": t.bucket, "project": project.code,
                                                       "mapped": False, "due": t.due.isoformat() if t.due else None}))
            continue
        mapped += 1
        status = m.CommitmentStatus.submitted if t.done else m.CommitmentStatus.open
        existing = by_ref.get(t.id)
        if existing is not None:
            if t.done and existing.status in ("open", "confirmed", "flagged", "overdue"):
                from gw.record.repo import set_commitment_status

                set_commitment_status(store, existing, "submitted", f"system:{conn.id}", reason="plan task done")
            continue
        owner = users.get(t.owner_email or "")
        due = t.due or (gi.planned_review_at if gi else clock.now())
        spec = deliverables.get(deliv) if deliv else None
        kind = m.CommitmentKind.gate_deliverable if spec and deliv not in gate_deliv else m.CommitmentKind.plan_task
        c = m.Commitment(
            id=new_id("Commitment"), code="", project_id=project.id,
            gate_instance_id=gi.id if (gi and spec) else None, work_package_id=wp.id if wp else None,
            deliverable_id=deliv or (wp.code if wp else t.id), title=t.title, kind=kind,
            owner_user_id=owner.id if owner else (project.owner_user_id or ""),
            party_id=wp.party_id if wp and wp.party_id else party.id, due_at=due, original_due_at=due,
            status=status, source_ref=m.ExtRef(conn_id=conn.id, external_id=t.id),
            weight=spec.weight if spec and kind == m.CommitmentKind.gate_deliverable else 0.0,
            mandatory=bool(spec.mandatory) if spec and kind == m.CommitmentKind.gate_deliverable else False,
            tags=list(spec.tags) if spec else [], created_at=clock.now())
        if kind == m.CommitmentKind.gate_deliverable and deliv:
            gate_deliv[deliv] = c
        new.append(c)
    if new:
        n = store.count(m.Commitment)
        for i, c in enumerate(new):
            c.code = f"C-{n + i + 1:04d}"
        store.insert_many(new, actor=f"system:{conn.id}", kind="imported")
    if unmapped:
        store.insert_many(unmapped, actor=f"system:{conn.id}", kind="pulled")
    return mapped


# --- DocumentSource / MessageChannel ---------------------------------------------------------


def _new_items(store: Store, conn_id: str, items: list[m.InboundItem]) -> list[m.InboundItem]:
    if not items:
        return []
    have = {i.external_id for i in store.find(m.InboundItem, conn_id=conn_id, external_id=[x.external_id for x in items])}
    fresh = [i for i in items if i.external_id not in have]
    store.insert_many(fresh, actor=f"system:{conn_id}", kind="pulled")
    return fresh


def ingest_documents(store: Store, conn: m.Connection, src: Any) -> SyncResult:
    """Document changes become inbound items with metadata only; bodies are never stored.

    A tender room (`config.role: tender`) is read by the tender agent on demand, not by the inbox.
    """
    changes, cursor = src.list_changes(conn.cursor)
    if conn.config.get("role") == "tender":
        return SyncResult(pulled=len(changes), cursor=cursor)
    items = [m.InboundItem(id=new_id("InboundItem"), conn_id=conn.id, external_id=c.ref.external_id + "@" + c.modified.isoformat(),
                           kind="document", received_at=c.modified,
                           payload={"name": c.name, "ref": c.ref.model_dump(), "deleted": c.deleted})
             for c in changes]
    fresh = _new_items(store, conn.id, items)
    return SyncResult(pulled=len(changes), changed=len(fresh), cursor=cursor)


def ingest_messages(store: Store, conn: m.Connection, src: Any) -> SyncResult:
    msgs = src.pull(conn.cursor)
    items = [m.InboundItem(id=new_id("InboundItem"), conn_id=conn.id, external_id=x.id, kind="message",
                           received_at=x.received_at,
                           payload={"from": (x.headers.get("x-gw-party") if conn.config.get("demo_party_header") else None)
                                    or x.sender, "subject": x.subject, "text": x.text, "thread_id": x.thread_id,
                                    "gw_ref": x.headers.get("x-gw-ref"),
                                    "attachments": [a.model_dump() for a in x.attachments]})
             for x in msgs]
    fresh = _new_items(store, conn.id, items)
    cursor = max((x.received_at for x in msgs), default=None)
    return SyncResult(pulled=len(msgs), changed=len(fresh), cursor=cursor.isoformat() if cursor else conn.cursor)


# --- CostSource ---------------------------------------------------------------------------


def ingest_cost(store: Store, conn: m.Connection, src: Any) -> SyncResult:
    """Cost rows land in `cost_rows`; contingency drawn is recomputed per project."""
    rows = src.get_rows(conn.cursor)
    have = {(r.project_code, r.wbs, r.kind, r.posted_at, r.amount) for r in store.all(m.CostRow)}
    fresh = [m.CostRow(id=new_id("CR"), **r.model_dump()) for r in rows
             if (r.project_code, r.wbs, r.kind, r.posted_at, r.amount) not in have]
    store.insert_many(fresh, actor=f"system:{conn.id}", kind="pulled")
    touched = {r.project_code for r in fresh}
    for code in touched:
        p = store.find_one(m.Project, code=code)
        if p is None:
            continue
        drawn = sum(r.amount for r in store.find(m.CostRow, project_code=code, kind="contingency_draw"))
        if abs(drawn - p.contingency_drawn) > 1e-6:
            p.contingency_drawn = drawn
            store.update(p, kind="contingency", payload={"drawn": drawn})
    res = src.sync(conn.cursor)
    return SyncResult(pulled=len(rows), changed=len(fresh), cursor=res.cursor or datetime.now(UTC).date().isoformat())


# --- CalendarSource ---------------------------------------------------------------------------


def _gate_label(name: str) -> str:
    """'FEL 3 | Definition' -> 'fel 3'; 'Stage 2 | Concept design' -> 'stage 2' (older names used a middle dot)."""
    return re.split(r"[|\u00b7]", name)[0].strip().lower()


def ingest_calendar(store: Store, conn: m.Connection, src: Any) -> SyncResult:
    """Gate review dates come from the calendar when linked.

    An event whose title holds a project code and the current gate's id or name (e.g.
    "DC-01 FEL 3 review") sets that gate instance's planned_review_at; a change is an event.
    """
    import re
    from datetime import timedelta

    now = clock.now()
    events = src.list_events((now - timedelta(days=30), now + timedelta(days=400)))
    projects = {p.code: p for p in store.all(m.Project)}
    changed = 0
    for ev in events:
        mt = re.search(r"\b([A-Z]{2,4}-\d{2})\b", ev.title or "")
        p = projects.get(mt.group(1)) if mt else None
        if p is None or not p.current_gate_instance_id:
            continue
        gi = store.get(m.GateInstance, p.current_gate_instance_id)
        title = (ev.title or "").lower()
        if gi.gate_id.lower() not in title.split() and _gate_label(gi.name) not in title:
            continue
        start = ev.start if ev.start.tzinfo else ev.start.replace(tzinfo=now.tzinfo)
        if abs((gi.planned_review_at - start).total_seconds()) < 60:
            continue
        old = gi.planned_review_at
        gi.planned_review_at = start
        store.update(gi, actor=f"system:{conn.id}", kind="review_rescheduled",
                     payload={"from": old.isoformat(), "to": start.isoformat(), "event": ev.id})
        changed += 1
    return SyncResult(pulled=len(events), changed=changed, cursor=now.isoformat())
