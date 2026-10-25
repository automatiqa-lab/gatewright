"""Chaser agent.

For each open, flagged or overdue commitment: next reminder from the cadence class,
level from the ladder, one per commitment per day, at most N per party per day
(overflow carried in priority order), planned inside the party's send window in its
local time. L1-L2 apply when autonomy is act; L3 always drafts for approval; L4
creates a war-room item and sends nothing. The chaser never parses replies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2

from gw.agents.base import AgentOutput, Snapshot
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store
from gw.signals.calendars import business_days_between, is_business_day

S = m.CommitmentStatus
ACTIVE = {S.open, S.flagged, S.overdue}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TEMPLATES = jinja2.Environment(loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates" / "chase"),
                               autoescape=False, keep_trailing_newline=True)


def _hm(s: str) -> time:
    h, mi = s.split(":")
    return time(int(h), int(mi))


def send_slot(pol: Policies, tz: str, country: str, after: datetime) -> datetime:
    """The next moment inside the party's send window, on a send day, in UTC."""
    win = str(pol.get("chasing.send_window", "08:00-10:00")).split("-")
    start, end = _hm(win[0]), _hm(win[1])
    days = set(pol.get("chasing.send_days") or DAYS[:5])
    zone = ZoneInfo(tz)
    local = after.astimezone(zone)
    for i in range(0, 15):
        d = (local + timedelta(days=i)).date()
        if DAYS[d.weekday()] not in days or not is_business_day(d, country):
            continue
        open_at = datetime.combine(d, start, zone)
        close_at = datetime.combine(d, end, zone)
        if i == 0 and local >= close_at:
            continue
        slot = max(open_at, local) if i == 0 else open_at
        return slot.astimezone(after.tzinfo or ZoneInfo("UTC"))
    return after


def in_window(pol: Policies, tz: str, at: datetime) -> bool:
    win = str(pol.get("chasing.send_window", "08:00-10:00")).split("-")
    local = at.astimezone(ZoneInfo(tz))
    days = set(pol.get("chasing.send_days") or DAYS[:5])
    return DAYS[local.weekday()] in days and _hm(win[0]) <= local.time() < _hm(win[1])


@dataclass
class Plan:
    commitment: m.Commitment
    party: m.Party
    level: int
    step: int
    planned_for: datetime
    priority: tuple[Any, ...]
    reason: str


def cadence_class(party: m.Party, pol: Policies) -> str:
    if party.kind in ("authority", "sponsor"):
        return "authority"
    if party.kind == "internal_team":
        return "internal"
    if pol.get("chasing.silent_vendor_uses_authority_cadence", True) and party.response_profile.silence_rate > float(pol.get("chasing.slow_responder_rate", 0.4)):
        return "authority"
    return "vendor"


class Chaser:
    name = "chaser"
    output_type = "ChaseMessage"

    def __init__(self, project_code: str | None = None, only: str | None = None, force_level: int | None = None) -> None:
        self.project_code = project_code
        self.only = only  # one commitment (gw.commitment.chase)
        self.force_level = force_level
        self.summary = ""
        self.tokens_used = 0

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        from gw.engines.drafting import Drafter

        self.drafter = Drafter(store, policies, "chaser")
        self.draft_levels = set(policies.get("models.drafting.chaser_levels", [3]) or [])
        now = clock.now()
        cs = [c for c in store.all(m.Commitment) if c.status in ACTIVE]
        if self.project_code:
            pid = store.find_one(m.Project, code=self.project_code)
            cs = [c for c in cs if pid and c.project_id == pid.id]
        if self.only:
            cs = [c for c in cs if c.code == self.only or c.id == self.only]
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        chases = store.all(m.Chase)
        steps_done: dict[str, set[int]] = {}
        today_by_party: dict[str, int] = {}
        today_commit: set[str] = set()
        open_drafts: set[str] = set()
        for ch in chases:
            steps_done.setdefault(ch.commitment_id, set()).add(ch.step)
            when = ch.sent_at or ch.planned_for
            if when >= today_start and ch.status in ("sent", "queued"):
                today_by_party[ch.party_id] = today_by_party.get(ch.party_id, 0) + 1
                today_commit.add(ch.commitment_id)
            if ch.status in ("drafted", "war_room"):
                open_drafts.add(f"{ch.commitment_id}:{ch.level}")
        projects = {p.id: p for p in store.all(m.Project)}
        gates = {g.id: g for g in store.all(m.GateInstance)}
        return Snapshot(seq=store.last_seq(), policies=policies, data={
            "now": now, "commitments": cs, "parties": {p.id: p for p in store.all(m.Party)},
            "users": {u.id: u for u in store.all(m.User)}, "projects": projects, "gates": gates,
            "steps_done": steps_done, "today_by_party": today_by_party, "today_commit": today_commit,
            "open_drafts": open_drafts})

    def plan(self, snap: Snapshot) -> tuple[list[Plan], list[Plan]]:
        """Today's chases (within caps) and the overflow carried to the next day."""
        pol, d = snap.policies, snap.data
        now: datetime = d["now"]
        silence_l3 = int(pol.get("escalation.silence_days_to_l3", 5))
        l4_after = l4_overdue_days(pol)
        candidates: list[Plan] = []
        for c in d["commitments"]:
            party: m.Party = d["parties"][c.party_id]
            cadence = sorted((int(x) for x in pol.get(f"chasing.cadence.{cadence_class(party, pol)}")
                              or pol.get("chasing.cadence.default") or []), reverse=True)
            due = c.due_at.astimezone(ZoneInfo(party.tz)).date()
            today = now.astimezone(ZoneInfo(party.tz)).date()
            done = d["steps_done"].get(c.id, set())
            silence = business_days_between(c.first_unanswered_chase_at, now, party.country) if c.first_unanswered_chase_at else 0
            overdue_days = (today - due).days
            if self.force_level:
                lvl, step, why = self.force_level, max(done, default=0) + 1, "manual chase"
            elif l4_after is not None and overdue_days >= l4_after and c.status in (S.overdue, S.open):
                lvl, step, why = 4, 90, f"overdue {overdue_days} d"
            else:
                reached = [i for i, k in enumerate(cadence, start=1) if due - timedelta(days=k) <= today]
                pending = [i for i in reached if i not in done]
                if not pending:
                    if c.chase_count and silence >= silence_l3 and c.escalation_level < 3:
                        lvl, step, why = 3, max(done, default=0) + 1, f"silent {silence} bd"
                    else:
                        continue
                else:
                    step = pending[-1]
                    lvl = min(step, 3)
                    why = f"T-{cadence[step - 1]} (step {step})"
                    if silence >= silence_l3:
                        lvl, why = 3, f"silent {silence} bd"
            if lvl >= 3 and f"{c.id}:{lvl}" in d["open_drafts"]:
                continue  # a draft already waits for a person
            if c.id in d["today_commit"] and pol.get("chasing.one_per_commitment_per_day", True):
                continue
            slot = send_slot(pol, party.tz, party.country, now)
            prio = (not c.mandatory, c.kind != "gate_deliverable", c.due_at, c.code)
            candidates.append(Plan(c, party, lvl, step, slot, prio, why))
        candidates.sort(key=lambda p: p.priority)
        cap = int(pol.get("chasing.max_per_party_per_day", 3))
        used = dict(d["today_by_party"])
        today_plans: list[Plan] = []
        overflow: list[Plan] = []
        for p in candidates:
            if p.level >= 4:
                today_plans.append(p)  # war-room items send nothing; no cap
                continue
            if used.get(p.party.id, 0) >= cap:
                p.planned_for = send_slot(pol, p.party.tz, p.party.country, p.planned_for + timedelta(days=1)) \
                    if in_window(pol, p.party.tz, p.planned_for) else send_slot(pol, p.party.tz, p.party.country,
                                                                                p.planned_for + timedelta(hours=14))
                overflow.append(p)
                continue
            used[p.party.id] = used.get(p.party.id, 0) + 1
            today_plans.append(p)
        return today_plans, overflow

    def render(self, snap: Snapshot, p: Plan) -> dict[str, Any]:
        d = snap.data
        c = p.commitment
        owner = d["users"].get(c.owner_user_id)
        project = d["projects"][c.project_id]
        gi = d["gates"].get(c.gate_instance_id or "")
        dependency = f"{project.code} {gi.name} review {gi.planned_review_at:%d %b}" if gi else ""
        contact = p.party.contacts[0] if p.party.contacts else m.Contact(name=p.party.name, email="")
        ref = f"{c.code}/{p.step}"
        pm = d["users"].get(project.owner_user_id or "")
        cc_names = ", ".join(x for x in [pm.name if pm and p.level >= 2 else None,
                                          "sponsor" if p.level >= 3 else None] if x) or "-"
        tmpl = TEMPLATES.get_template(f"l{min(p.level, 3)}.md.j2")
        text = tmpl.render(contact=contact.name, ref=ref, project=f"{project.code} {project.name}", title=c.title,
                           title_lower=c.title[0].lower() + c.title[1:], due=f"{c.due_at:%d %b %Y}",
                           dependency=dependency, dependency_short=dependency or "the next review",
                           owner=owner.name if owner else "the project owner", cc_names=cc_names,
                           silence=business_days_between(c.first_unanswered_chase_at, d["now"], p.party.country)
                           if c.first_unanswered_chase_at else 0,
                           free_text=snap.policies.get("chasing.reply_form", "structured_plus_free_text") != "structured_only")
        to = [contact.email] if p.level != 2 else [contact.email] + ([owner.email] if owner else [])
        cc = [pm.email] if pm and p.level >= 2 else []
        return {"to": to, "cc": cc, "subject": f"[{ref}] {c.title} - due {c.due_at:%d %b}", "text": text, "gw_ref": ref,
                "channel": contact.channel_pref if contact.channel_pref in ("mail", "teams") else
                snap.policies.get("chasing.channel_default", "mail")}

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        plans, overflow = self.plan(snap)
        outs: list[AgentOutput] = []
        for p in plans:
            c = p.commitment
            if p.level >= 4:
                outs.append(AgentOutput(
                    output_type="WarRoomItem", title=f"{c.code} overdue - decide on the escalation",
                    payload={"commitment": c.code, "commitment_id": c.id, "level": 4, "reason": p.reason,
                             "party": p.party.name, "planned_for": snap.data["now"].isoformat()},
                    citations=[c.id, p.party.id], approval_kind=None, apply=None, hard_limited=True,
                    on_draft=lambda st, run, ao, ap: record_draft(st, run, ao, "war_room", ap)))
                continue
            msg = self.render(snap, p)
            generated: list[str] = []
            drafter = getattr(self, "drafter", None)
            if drafter is not None and p.level in self.draft_levels:
                draft = drafter.rewrite(msg["text"], purpose=f"L{p.level} chase to a {p.party.kind} party",
                                        facts={"commitment": c.code, "title": c.title, "due": f"{c.due_at:%d %b %Y}",
                                               "level": p.level, "reason": p.reason},
                                        keep=[msg["text"].splitlines()[0], f"gw-ref {msg['gw_ref']}", "CONFIRM",
                                              "FLAG - <new date YYYY-MM-DD> <reason>"])
                msg = {**msg, "text": draft.text, "drafting": draft.audit()}
                generated = ["text"] if draft.generated else []
            payload = {"commitment": c.code, "commitment_id": c.id, "party_id": p.party.id, "party": p.party.name,
                       "party_kind": str(p.party.kind), "level": p.level, "step": p.step, "reason": p.reason,
                       "planned_for": p.planned_for.isoformat(), **msg}
            if p.level >= 3:
                outs.append(AgentOutput(output_type="EscalationDraft", title=f"L3 to {p.party.name} on {c.code}",
                                        payload=payload, citations=[c.id, p.party.id], hard_limited=True,
                                        approval_kind="escalation_l3", apply=None, generated=generated,
                                        on_draft=lambda st, run, ao, ap: record_draft(st, run, ao, "drafted", ap)))
            else:
                outs.append(AgentOutput(output_type="ChaseMessage", title=f"L{p.level} {c.code} to {p.party.name}",
                                        payload=payload, citations=[c.id, p.party.id], generated=generated,
                                        approval_kind="send_external", apply=queue_chase,
                                        on_draft=lambda st, run, ao, ap: record_draft(st, run, ao, "drafted", ap)))
        self.summary = (f"{sum(o.output_type == 'ChaseMessage' for o in outs)} queued | "
                        f"{sum(o.output_type == 'EscalationDraft' for o in outs)} L3 drafted | "
                        f"{sum(o.output_type == 'WarRoomItem' for o in outs)} L4 items | {len(overflow)} carried")
        self.overflow = overflow
        self.tokens_used = getattr(getattr(self, "drafter", None), "tokens", 0)
        return outs


def l4_overdue_days(pol: Policies) -> int | None:
    """Days overdue that raise L4, from the ladder's `at: overdue_<n>_days` (None: no L4 step)."""
    for step in pol.get("escalation.ladder", []) or []:
        found = re.fullmatch(r"overdue_(\d+)_days?", str(step.get("at", "")))
        if int(step.get("level", 0)) == 4 and found:
            return int(found.group(1))
    return None


def queue_chase(store: Store, run: m.AgentRun, out: m.AgentOutput, approval_id: str | None = None) -> m.Chase:
    """Turn a chase output into a queued Chase and advance the commitment's ladder state."""
    pl = out.payload
    c = store.get(m.Commitment, pl["commitment_id"])
    ch = m.Chase(id=new_id("Chase"), commitment_id=c.id, project_id=c.project_id, party_id=pl["party_id"],
                 level=int(pl["level"]), step=int(pl["step"]), channel=pl.get("channel", "mail"), status="queued",
                 planned_for=datetime.fromisoformat(pl["planned_for"]), gw_ref=pl["gw_ref"], to=pl["to"], cc=pl["cc"],
                 subject=pl["subject"], text=pl["text"], agent_run_id=run.id, approval_id=approval_id)
    store.insert(ch, actor=f"agent:{run.agent}", agent_run_id=run.id)
    c.chase_count += 1
    c.last_chase_at = ch.planned_for
    if c.first_unanswered_chase_at is None:
        c.first_unanswered_chase_at = ch.planned_for
    if c.status in ACTIVE:
        c.escalation_level = max(c.escalation_level, ch.level)
    store.update(c, actor=f"agent:{run.agent}", kind="chased", agent_run_id=run.id,
                 payload={"level": ch.level, "gw_ref": ch.gw_ref})
    return ch


def record_draft(store: Store, run: m.AgentRun, out: m.AgentOutput, status: str,
                 approval_id: str | None = None) -> m.Chase:
    """L3 drafts and L4 items are visible on GW-300 as chases that were not sent."""
    pl = out.payload
    c = store.get(m.Commitment, pl["commitment_id"])
    ch = m.Chase(id=new_id("Chase"), commitment_id=c.id, project_id=c.project_id, party_id=c.party_id,
                 level=int(pl["level"]), step=int(pl.get("step", 90)), status=status,
                 planned_for=datetime.fromisoformat(pl["planned_for"]) if pl.get("planned_for") else clock.now(),
                 gw_ref=pl.get("gw_ref", c.code), to=pl.get("to", []), cc=pl.get("cc", []),
                 subject=pl.get("subject", ""), text=pl.get("text", ""), agent_run_id=run.id, approval_id=approval_id)
    store.insert(ch, actor=f"agent:{run.agent}", agent_run_id=run.id)
    return ch
