"""Synthetic portfolio generator.

`build()` creates the fixed portfolio, walks each project's gate model through 24
months of history, plays the scripted cases, exports schedules (MSPDI), a Planner
fixture and cost CSVs, syncs them through the real harness, and writes the
labelled corpus and the forecast priors. Same seed and same `today` give the same
record.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from gw.demo import portfolio as dc_portfolio
from gw.demo import texts
from gw.demo.portfolio import (
    BEHAVIOUR,
    COUNTRY_TZ,
    DEMO_DOMAIN,
    DemoConfig,
    ProjectSpec,
)
from gw.engines.forecast import features as feat
from gw.policies import store as policies
from gw.record import clock, ids
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store
from gw.signals.calendars import add_business_days, business_days_between
from gw.standards import gatemodels as gm


def portfolio_module(name: str) -> ModuleType:
    """The fixed data of a synthetic portfolio: `dc` (capital projects) or `integration` (M&A)."""
    if name == "integration":
        from gw.demo import portfolio_integration

        return portfolio_integration
    return dc_portfolio


S = m.CommitmentStatus
DAY = timedelta(days=1)


def at(d: date, hour: int = 9) -> datetime:
    return datetime.combine(d, time(hour, 0), tzinfo=UTC)


def slip_label(due: date, accepted: date | None, confirmed: date | None) -> float:
    """: slip = accepted/confirmed after due_at, or neither by due + 1 d."""
    first = min(d for d in (accepted, confirmed) if d is not None) if (accepted or confirmed) else None
    return 1.0 if first is None or first > due + DAY else 0.0


@dataclass
class Sim:
    """Outcome of simulating one commitment's chase loop up to a horizon."""

    chases: list[m.Chase] = field(default_factory=list)
    inbound: list[m.InboundItem] = field(default_factory=list)
    submit: date | None = None
    accept: date | None = None
    rejected_once: bool = False
    flagged_to: date | None = None
    last_reply: datetime | None = None
    first_unanswered: datetime | None = None
    level: int = 0
    confirmed: bool = False
    confirmed_at: date | None = None


class Gen:
    """Generator state. Cases (gw/demo/cases) read and modify it before it is flushed."""

    def __init__(self, store: Store, today: date, cfg: DemoConfig, data_dir: Path,
                 pf: ModuleType | None = None) -> None:
        self.pf = pf or dc_portfolio
        self.store = store
        self.today = today
        self.now = datetime.combine(today, time(6, 30), tzinfo=UTC)
        self.cfg = cfg
        self.data_dir = data_dir
        self.rng = random.Random(f"{cfg.seed}:{today.isoformat()}")
        self.pol = policies.from_default()
        self.users: dict[str, m.User] = {}
        self.parties: dict[str, m.Party] = {}
        self.pspec: dict[str, Any] = {}
        self.projects: dict[str, m.Project] = {}
        self.specs: dict[str, ProjectSpec] = {}
        self.models: dict[str, gm.GateModel] = {}
        self.packages: dict[str, list[m.WorkPackage]] = {}
        self.gates: list[m.GateInstance] = []
        self.current_gate: dict[str, m.GateInstance] = {}
        self.commitments: list[m.Commitment] = []
        self.chases: list[m.Chase] = []
        self.inbound: list[m.InboundItem] = []
        self.evidence: list[m.Evidence] = []
        self.features: list[m.FeatureRow] = []
        self.risks: list[m.Risk] = []
        self.decisions: list[m.Decision] = []
        self.lessons: list[m.Lesson] = []
        self.overnight_mail: list[dict[str, Any]] = []
        self.overnight_docs: dict[str, dict[str, str]] = {}
        self.cost_rows: list[dict[str, Any]] = []
        self.calendar: list[dict[str, Any]] = []
        self.scripted: dict[str, dict[str, dict[str, Any]]] = {}
        self.float_change: dict[str, float] = {}
        self.dcma_fail: dict[str, list[str]] = {}
        self.estimate_drift: dict[str, float] = {}
        self.contingency_share: dict[str, float] = {}
        self.silent_overrides: set[str] = set()  # commitment ids whose party never replies (cases)
        self._code = 0

    # --- helpers ---------------------------------------------------------------------

    def code(self) -> str:
        self._code += 1
        return f"C-{self._code:04d}"

    def user(self, key: str) -> m.User:
        return self.users[key]

    def party(self, key: str) -> m.Party:
        """A party by key; portfolios without that team fall back to their PMO."""
        return self.parties.get(key) or self.parties["pmo"]

    def party_by_name(self, name: str) -> m.Party:
        return next(p for p in self.parties.values() if p.name == name)

    def commitment(self, project: str, deliverable: str) -> m.Commitment:
        pid = self.projects[project].id
        gi = self.current_gate[project]
        return next(c for c in self.commitments if c.project_id == pid and c.deliverable_id == deliverable
                    and c.gate_instance_id == gi.id)

    def behaviour(self, party: m.Party) -> tuple[float, float, float, float]:
        return BEHAVIOUR[party.behaviour]

    def lang(self, party: m.Party) -> str:
        spec = self.pspec[party.id]
        return spec.lang if spec.lang != "en" and self.rng.random() < 0.5 else "en"

    def cadence_class(self, party: m.Party) -> str:
        if party.kind in ("authority", "sponsor"):
            return "authority"
        if party.kind == "internal_team":
            return "internal"
        if party.behaviour == "silent":
            return "authority"
        return "vendor"

    # --- fixed data ------------------------------------------------------------------

    def build_fixed(self) -> None:
        for f in gm.shipped_files():
            model = gm.publish(self.store, f.read_text(encoding="utf-8"), "system")
            self.models[model.id] = model
        policies.install_default(self.store)
        for u in self.pf.USERS:
            self.users[u.key] = m.User(id=new_id("User"), email=f"{u.key}@{DEMO_DOMAIN}", name=u.name, short=u.key,
                                       roles=list(u.roles), tz=u.tz)
        for p in self.pf.PARTIES:
            prof = BEHAVIOUR[p.behaviour]
            party = m.Party(id=new_id("Party"), name=p.name, kind=m.PartyKind(p.kind), country=p.country,
                            tz=COUNTRY_TZ.get(p.country, "UTC"), behaviour=p.behaviour,
                            contacts=[m.Contact(name=p.contact, email=f"{p.key.replace('-', '.')}@{DEMO_DOMAIN}",
                                                channel_pref="teams" if p.kind == "internal_team" else "mail")],
                            response_profile=m.ResponseProfile(median_reply_days=prof[0], silence_rate=prof[1],
                                                               flag_rate=prof[2], sample_n=0))
            self.parties[p.key] = party
            self.pspec[party.id] = p
        for code, title, text, src, tags in self.pf.LESSONS:
            self.lessons.append(m.Lesson(id=new_id("Lesson"), code=code, title=title, text=text, tags=tags,
                                         source_project_id=src))

    def build_projects(self) -> None:
        for s in self.pf.PROJECTS:
            model = self.models[s.gate_model]
            gates = sorted(model.gates, key=lambda g: g.order)
            cur_idx = [g.id for g in gates].index(s.current_gate)
            first_review = self.today + timedelta(days=s.past_reviews[0] if s.past_reviews else s.review_in_days)
            start = first_review - timedelta(days=150)
            total = (self.today - start).days / max(s.time_elapsed, 0.05)
            proj = m.Project(
                id=new_id("Project"), code=s.code, name=s.name, site=s.site, country=s.country, zone=s.zone,
                archetype=m.Archetype(s.archetype), size=s.size, sponsor_user_id=self.user("b.keller").id,
                owner_user_id=self.user(s.pm).id, gate_model_id=model.id, gate_model_rev=model.rev,
                estimate_class=s.estimate_class, capex_amount=s.capex, capex_currency=s.currency,
                contingency_amount=round(s.capex * s.contingency_pct, -3), contingency_drawn=0.0,
                start_date=start, baseline_finish=start + timedelta(days=int(total)), created_at=at(start))
            self.projects[s.code] = proj
            self.specs[s.code] = s
            self.build_packages(proj, s)
            # past gates
            prev = start
            for i, g in enumerate(gates[:cur_idx]):
                review = self.today + timedelta(days=s.past_reviews[i])
                self.past_gate(proj, s, model, g, review, prev)
                prev = review
            g = gates[cur_idx]
            gi = gm.instance_from_gate(proj, model, g, at(self.today + timedelta(days=s.review_in_days), 10))
            gi.decision = m.GateDecision.pending
            self.gates.append(gi)
            self.current_gate[s.code] = gi
            proj.current_gate_instance_id = gi.id

    def build_packages(self, proj: m.Project, s: ProjectSpec) -> None:
        packages = self.pf.PACKAGES
        tmpl = packages.get(s.archetype) or packages.get("greenfield") or next(iter(packages.values()))
        wps = []
        base = self.today + timedelta(days=self.rng.randint(-60, -20))
        for i, (disc, name, vtag) in enumerate(tmpl, start=1):
            vendor = self.party_by_name(s.gc) if vtag == "gc" else (
                self.party_by_name(s.vendors[vtag]) if vtag in s.vendors else self.party_by_name(s.vendors["main"]))
            ws = base + timedelta(days=25 * (i - 1) + self.rng.randint(0, 3))
            wps.append(m.WorkPackage(id=new_id("WorkPackage"), project_id=proj.id, code=f"{s.code}-WP{i:02d}",
                                     name=name, discipline=disc, owner_user_id=proj.owner_user_id, party_id=vendor.id,
                                     tender_state=m.TenderState.technical if disc in ("racking", "asrs") else m.TenderState.none,
                                     window_start=ws, window_end=ws + timedelta(days=self.rng.randint(15, 22))))
        self.packages[s.code] = wps

    # --- assignment rules --------------------------------------------------------------

    def party_for(self, s: ProjectSpec, d: m.DeliverableSpec) -> m.Party:
        role = d.party_role
        if role == "authority":
            if "environmental" in d.id and f"env-{s.country}" in self.parties:
                return self.party(f"env-{s.country}")
            return self.party(f"muni-{s.country}")
        if role == "insurer":
            return self.party("insurer")
        if role == "utility":
            return self.parties.get(f"grid-{s.country}") or self.party("engineering")
        if role == "consultant":
            return self.party("geoterra")
        if role == "contractor":
            return self.party_by_name(s.gc)
        if role == "vendor":
            for tag, key in (("systems", "systems"), ("design", "racking"), ("vendors", "main")):
                if tag in d.tags and key in s.vendors:
                    return self.party_by_name(s.vendors[key])
            return self.party_by_name(s.vendors["main"])
        tags = set(d.tags)
        if "safety" in tags:
            return self.party("hse")
        if "ops" in tags:
            return self.party("siteops")
        if "estimate" in d.id or "cost" in tags:
            return self.party("finance")
        if "vendors" in tags:
            return self.party("procurement")
        if "design" in tags or "systems" in tags:
            return self.party("engineering")
        return self.party("pmo")

    def owner_for(self, s: ProjectSpec, d: m.DeliverableSpec) -> m.User:
        roles = set(d.accept_roles)
        eu = s.zone == "EU"
        if "sme:hse" in roles and eu and s.code in ("DC-01", "DC-04", "DC-09"):
            return self.user("r.bakker")
        if ("sme:systems" in roles or "sme:operations" in roles) and eu:
            return self.user("e.visser")
        if "estimate" in d.id:
            return self.user("l.jansen")
        if "sme:procurement" in roles:
            return self.user("a.serra")
        if s.code == "DC-01" and "permits" in d.tags:
            return self.user("j.smits")
        return self.user(s.pm)

    def wp_for(self, project: str, party: m.Party, d_tags: list[str]) -> str | None:
        for wp in self.packages[project]:
            if wp.party_id == party.id or wp.discipline in d_tags:
                return wp.id
        return None

    # --- simulation ----------------------------------------------------------------------

    TYPE_EFFECT = {"permit": 0.12, "test": 0.10, "fire": 0.08, "utility": 0.10, "design": -0.04, "estimate": -0.06,
                   "ops": 0.0, "schedule": -0.06, "action": -0.05, "tender_item": 0.0, "milestone": 0.0}

    def p_slip(self, party: m.Party, mandatory: bool, due: date, dtype: str = "") -> float:
        """Slip probability from party profile and deliverable type."""
        base = {"prompt": 0.10, "slow": 0.40, "silent": 0.72, "delegating": 0.28, "flagger": 0.55}[party.behaviour]
        base += self.TYPE_EFFECT.get(dtype, 0.0)
        if party.kind == "authority":
            base += 0.08 + (0.12 if due.month in (8, 12) else 0)
        if party.kind == "insurer":
            base += 0.08
        if mandatory:
            base += 0.04
        return min(base, 0.95)

    def simulate(self, c: m.Commitment, party: m.Party, created: date, horizon: date, slip: bool | None = None,
                 force_silent: bool = False) -> Sim:
        """Chase loop for one commitment from `created` to `horizon` (inclusive)."""
        r = self.rng
        sim = Sim()
        due = c.due_at.date()
        median, silence, flag_share, deleg = self.behaviour(party)
        if force_silent:
            silence = 1.0
        if slip is None:
            slip = r.random() < max(0.02, self.p_slip(party, c.mandatory, due,
                                                      feat.deliverable_type(c.deliverable_id, str(c.kind))))
        if slip:
            delay = max(2.0, r.lognormvariate(math.log(9 if party.behaviour == "silent" else 6), 0.55))
            if party.kind == "authority" and due.month in (8, 12):
                delay *= 1.6
        else:
            delay = -r.uniform(0, 6)
        submit = due + timedelta(days=int(round(delay)))
        if submit < created + timedelta(days=2):
            submit = created + timedelta(days=2)
        if r.random() < 0.10:
            sim.rejected_once = True
            submit_final = submit + timedelta(days=r.randint(3, 7))
        else:
            submit_final = submit
        accept = submit_final + timedelta(days=r.randint(0, 2))
        cadence = self.pol.get(f"chasing.cadence.{self.cadence_class(party)}") or [5, 3, 0]
        chase_days = [due - timedelta(days=int(k)) for k in cadence]
        d = due + timedelta(days=3)
        while d < submit:
            chase_days.append(d)
            d += timedelta(days=4)
        level = 0
        lang = self.lang(party)
        for step, cd in enumerate(sorted(set(chase_days)), start=1):
            if cd < created or cd > horizon or cd >= submit:
                continue
            if cd.weekday() >= 5:
                cd = cd + timedelta(days=7 - cd.weekday())
            level = min(step, 3)
            if cd > due + timedelta(days=3):
                level = 3
            if level >= 3 and party.kind in ("authority", "sponsor"):
                continue  # L3 to an authority is a draft for a person, never history mail
            ch = m.Chase(id=new_id("Chase"), commitment_id=c.id, project_id=c.project_id, party_id=party.id,
                         level=level, step=step, channel=party.contacts[0].channel_pref, status="sent",
                         planned_for=at(cd, 8), sent_at=at(cd, 8), gw_ref=f"{c.code}/{step}",
                         to=[party.contacts[0].email], subject=f"[{c.code}/{step}] {c.title}")
            sim.chases.append(ch)
            sim.level = max(sim.level, level)
            if r.random() < silence:
                if sim.first_unanswered is None:
                    sim.first_unanswered = ch.sent_at
                continue
            rd = cd + timedelta(days=max(0, int(round(r.expovariate(1 / max(median, 0.3))))))
            if rd > horizon or rd >= submit + timedelta(days=1):
                if sim.first_unanswered is None:
                    sim.first_unanswered = ch.sent_at
                continue
            if sim.flagged_to is None and slip and r.random() < min(0.95, flag_share * 1.6):
                intent = "FLAG"
                sim.flagged_to = submit
            elif r.random() < deleg:
                intent = "DELEGATE"
            elif r.random() < 0.04:
                intent = "OOO"
            elif r.random() < 0.05 or slip:
                intent = "QUESTION"  # a party that will slip does not confirm the date
            else:
                intent = "CONFIRM"
                sim.confirmed = True
                if sim.confirmed_at is None:
                    sim.confirmed_at = rd
            ch.replied_at = at(rd, 11)
            ch.reply_intent = intent
            sim.last_reply = ch.replied_at
            sim.first_unanswered = None
            structured = r.random() < 0.4
            why = texts.reason(r, lang) if intent == "FLAG" else None
            delegate = r.choice(["Ana Ruiz", "Tom Peeters", "Lena Vogel", "Omar Haddad"]) if intent == "DELEGATE" else None
            text = texts.reply_text(r, intent, lang, c.title, due, sim.flagged_to if intent == "FLAG" else None, why,
                                    delegate, structured)
            sim.inbound.append(m.InboundItem(
                id=new_id("InboundItem"), conn_id="mail-shared", external_id=f"hist-{c.code}-{step}", kind="message",
                received_at=ch.replied_at,
                payload={"from": party.contacts[0].email, "subject": f"RE: {ch.subject}", "text": text,
                         "gw_ref": ch.gw_ref if r.random() < 0.6 else None, "thread_id": f"t-{c.code}", "lang": lang},
                processed_at=ch.replied_at, commitment_id=c.id,
                classification={"intent": intent, "confidence": 1.0, "source": "history",
                                "entities": {"new_due_date": sim.flagged_to.isoformat() if intent == "FLAG" and sim.flagged_to else None,
                                             "reason": why, "person": delegate}}))
        if submit <= horizon:
            sim.submit = submit
            dt = texts.doc_type_for(c.deliverable_id, "document")
            name = texts.doc_name(r, self.code_of(c), c.deliverable_id, dt, r.random() < 0.7)
            sim.inbound.append(m.InboundItem(
                id=new_id("InboundItem"), conn_id="docs-cp", external_id=f"hist-doc-{c.code}", kind="document",
                received_at=at(submit, 14), payload={"name": name, "ref": {"conn_id": "docs-cp", "external_id": name}},
                processed_at=at(submit, 14), commitment_id=c.id,
                classification={"doc_type": dt, "confidence": 1.0, "source": "history"}))
        if accept <= horizon:
            sim.accept = accept
        return sim

    def code_of(self, c: m.Commitment) -> str:
        return next(code for code, p in self.projects.items() if p.id == c.project_id)

    def feature_row(self, c: m.Commitment, party: m.Party, sim: Sim, gi: m.GateInstance | None, as_of: date,
                    label: float | None, delay: float | None) -> m.FeatureRow:
        s = self.specs[self.code_of(c)]
        asof_dt = at(as_of, 6)
        sent = [ch for ch in sim.chases if ch.sent_at and ch.sent_at <= asof_dt]
        replied = [ch for ch in sent if ch.replied_at and ch.replied_at <= asof_dt]
        unanswered = [ch for ch in sent if not (ch.replied_at and ch.replied_at <= asof_dt)]
        first_un = None
        last_rep = max((ch.replied_at for ch in replied if ch.replied_at), default=None)
        for ch in unanswered:
            if ch.sent_at and (last_rep is None or ch.sent_at > last_rep):
                first_un = ch.sent_at
                break
        silence = business_days_between(first_un, asof_dt, party.country) if first_un else 0
        prof = BEHAVIOUR[party.behaviour]
        vtag = next((t for t, n in s.vendors.items() if n == party.name), "")
        state = {
            "days_to_due": (c.due_at.date() - as_of).days if label is not None else (c.due_at.date() - as_of).days,
            "deliverable_type": feat.deliverable_type(c.deliverable_id, str(c.kind)),
            "deliverable_mandatory": int(c.mandatory), "weight": c.weight,
            "gate_id": gi.gate_id if gi else "none", "gate_model_id": s.gate_model,
            "project_archetype": s.archetype, "country": s.country, "party_kind": str(party.kind),
            "party_median_reply_days": prof[0] * self.rng.uniform(0.9, 1.1),
            "party_silence_rate": min(1, prof[1] * self.rng.uniform(0.9, 1.1)),
            "party_flag_rate": prof[2], "party_sample_n": self.rng.randint(5, 60),
            "chases_sent": len(sent), "replies": len(replied), "silence_days": silence,
            "escalation_level": max((ch.level for ch in unanswered), default=0),
            "owner_open_items": self.rng.randint(2, 14), "owner_overdue_items": self.rng.randint(0, 4),
            "open_dependencies_on_this": 0, "changes_since_last_gate": self.rng.randint(0, 6),
            "estimate_class": s.estimate_class, "month_of_year": c.due_at.month,
            "vendor_lead_time_class": feat.LEAD_TIME_CLASS.get(vtag, "short"),
            "evidence_submitted": int(sim.submit is not None and sim.submit <= as_of),
            "days_since_last_schedule_sync": self.rng.randint(0, 7), "float_on_path": None,
            "is_permit": int(party.kind == "authority"), "is_insurer": int(party.kind == "insurer"),
            "prior_slips_same_party_same_type": self.rng.randint(0, 3) if party.behaviour in ("slow", "silent", "flagger") else self.rng.randint(0, 1),
            "flagged": int(any(ch.reply_intent == "FLAG" for ch in replied)), "synthetic": 0,
        }
        return m.FeatureRow(id=new_id("FR"), target="slip", target_id=c.id, as_of=asof_dt, features=feat.build(state),
                            label=label, delay_days=delay)

    def close_commitment(self, c: m.Commitment, party: m.Party, created: date, gi: m.GateInstance | None,
                         accept_by: str | None = None, slip: bool | None = None) -> Sim:
        """Simulate to closure in the past; writes chases, inbound, evidence and a labelled feature row."""
        sim = self.simulate(c, party, created, self.today - DAY, slip=slip)
        if sim.accept is None:  # still open at today; caller decides the current status
            return sim
        self.apply_sim(c, party, sim)
        c.status = S.accepted
        c.closed_at = at(sim.accept, 15)
        c.escalation_level = 0
        c.first_unanswered_chase_at = None
        ev = self.make_evidence(c, sim.accept, accept_by)
        ev.accepted_at = at(sim.accept, 15)
        c.evidence_id = ev.id
        delay = (sim.accept - c.original_due_at.date()).days
        label = slip_label(c.original_due_at.date(), sim.accept, sim.confirmed_at)
        seen: set[date] = set()
        for horizon in (28, 21, 14, 7, 2):  # one labelled row per prediction horizon ( days_to_due)
            as_of = max(created + DAY, c.original_due_at.date() - timedelta(days=horizon))
            if as_of in seen:
                continue
            seen.add(as_of)
            self.features.append(self.feature_row(c, party, sim, gi, as_of, label, float(max(delay, 0))))
        return sim

    def apply_sim(self, c: m.Commitment, party: m.Party, sim: Sim) -> None:
        self.chases.extend(sim.chases)
        self.inbound.extend(sim.inbound)
        c.chase_count = len(sim.chases)
        c.last_chase_at = max((ch.sent_at for ch in sim.chases if ch.sent_at), default=None)
        c.last_reply_at = sim.last_reply
        c.first_unanswered_chase_at = sim.first_unanswered

    def make_evidence(self, c: m.Commitment, when: date, accept_by: str | None) -> m.Evidence:
        gi = next((g for g in self.gates if g.id == c.gate_instance_id), None)
        roles = next((d.accept_roles for d in gi.deliverables if d.id == c.deliverable_id), ["project_manager"]) if gi else ["project_manager"]
        acceptor = None
        if accept_by:
            acceptor = self.user(accept_by)
        else:
            owner = next(u for u in self.users.values() if u.id == c.owner_user_id)
            acceptor = owner if set(owner.roles) & set(roles) else next(
                (u for u in self.users.values() if set(u.roles) & set(roles)), owner)
        kind = next((d.evidence for d in gi.deliverables if d.id == c.deliverable_id), "document") if gi else "document"
        name = f"{self.code_of(c)}_{c.deliverable_id}_rev{self.rng.choice('ABC')}.pdf"
        ev = m.Evidence(id=new_id("Evidence"), commitment_id=c.id, kind=m.EvidenceKind(kind),
                        uri=f"fake://docs-cp/{name}", title=name, revision=name[-5],
                        sha256=hashlib.sha256(f"{c.id}:{name}".encode()).hexdigest(), size=self.rng.randint(80_000, 4_000_000),
                        received_at=at(when, 14), source_conn_id="docs-cp", accepted_by_user_id=acceptor.id)
        self.evidence.append(ev)
        return ev

    def new_commitment(self, proj: m.Project, title: str, deliverable_id: str, kind: m.CommitmentKind,
                       owner: m.User, party: m.Party, due: date, created: date, gi: m.GateInstance | None = None,
                       spec: m.DeliverableSpec | None = None, wp: str | None = None) -> m.Commitment:
        c = m.Commitment(id=new_id("Commitment"), code=self.code(), project_id=proj.id,
                         gate_instance_id=gi.id if gi else None, work_package_id=wp, deliverable_id=deliverable_id,
                         title=title, kind=kind, owner_user_id=owner.id, party_id=party.id, due_at=at(due, 17),
                         original_due_at=at(due, 17), weight=spec.weight if spec else 0.0,
                         mandatory=spec.mandatory if spec else False, tags=list(spec.tags) if spec else [],
                         created_at=at(created))
        self.commitments.append(c)
        return c

    # --- gates -----------------------------------------------------------------------------

    def past_gate(self, proj: m.Project, s: ProjectSpec, model: gm.GateModel, g: gm.Gate, review: date,
                  prev: date) -> None:
        gi = gm.instance_from_gate(proj, model, g, at(review, 10))
        self.gates.append(gi)
        span = max(30, (review - prev).days)
        accepted_at: dict[str, date | None] = {}
        for d in g.deliverables:
            party = self.party_for(s, d)
            due = prev + timedelta(days=self.rng.randint(int(span * 0.35), max(int(span * 0.35) + 1, span - g.lead_time_days)))
            c = self.new_commitment(proj, d.name, d.id, m.CommitmentKind.gate_deliverable, self.owner_for(s, d), party,
                                    due, prev, gi, d, self.wp_for(s.code, party, d.tags))
            sim = self.close_commitment(c, party, prev, gi)
            if sim.accept is None:  # forced closure before the review
                sim = self.close_commitment(c, party, prev, gi, slip=False)
            accepted_at[d.id] = sim.accept
        # decision at review, by the readiness rule plus human noise
        def readiness_at(day: date) -> tuple[float, bool]:
            num = den = 0.0
            mand = True
            for d in g.deliverables:
                ok = (accepted_at.get(d.id) or date.max) <= day
                num += d.weight * ok
                den += d.weight
                mand = mand and (ok or not d.mandatory)
            return num / den, mand
        r, mand = readiness_at(review)
        roll = self.rng.random()
        decided = review
        if (r >= g.threshold and mand and roll >= 0.03) or roll < 0.0:
            decision = "pass_with_actions" if (roll < 0.08 or r < 1.0) and "pass_with_actions" in g.decisions else "pass"
        else:
            decided = review + timedelta(days=21)  # hold, rescheduled, then passed
            decision = "pass_with_actions" if "pass_with_actions" in g.decisions else "pass"
            self.store_hold = getattr(self, "store_hold", 0) + 1
        gi.decision = m.GateDecision(decision)
        gi.actual_review_at = at(min(decided, self.today - DAY), 11)
        gi.decided_at = gi.actual_review_at
        gi.decided_by_user_id = next((u.id for u in self.users.values() if set(u.roles) & set(g.decided_by)),
                                     self.user("aleks").id)
        gi.readiness = r
        if decision == "pass_with_actions":
            for k in range(1 if s.code != "DC-01" else 1):
                party = self.party("pmo")
                due = decided + timedelta(days=g.actions_due_days)
                c = self.new_commitment(proj, f"Action from {g.name}: close open item {k + 1}", f"action:{g.id}:{k + 1}",
                                        m.CommitmentKind.action, self.user(s.pm), party, due, decided, None)
                if due < self.today - timedelta(days=10):
                    self.close_commitment(c, party, decided, None)
                else:
                    self.open_commitment(c, party, decided)

    def open_commitment(self, c: m.Commitment, party: m.Party, created: date, status: str | None = None,
                        force_silent: bool = False, sim: Sim | None = None) -> Sim:
        """Simulate an open commitment up to yesterday and derive its current status.

        `sim` reuses an earlier draw (an item that did not close by today keeps its outcome; a fresh
        draw would drop slipped items from the labelled history).
        """
        if sim is None:
            sim = self.simulate(c, party, created, self.today - DAY, slip=None, force_silent=force_silent)
        sim.submit = None
        sim.accept = None
        self.inbound.extend([i for i in sim.inbound if i.kind == "message"])
        self.chases.extend(sim.chases)
        c.chase_count = len(sim.chases)
        c.last_chase_at = max((ch.sent_at for ch in sim.chases if ch.sent_at), default=None)
        c.last_reply_at = sim.last_reply
        c.first_unanswered_chase_at = sim.first_unanswered
        if status:
            c.status = S(status)
        elif sim.flagged_to:
            c.status = S.flagged
            c.due_at = at(sim.flagged_to, 17)
        elif c.due_at < self.now:
            c.status = S.overdue
        elif sim.confirmed and sim.first_unanswered is None:
            c.status = S.confirmed
        else:
            c.status = S.open
        c.escalation_level = 0 if c.status in (S.confirmed, S.submitted, S.accepted) else min(sim.level, 3)
        confirmed_on_time = sim.confirmed_at is not None and sim.confirmed_at <= c.original_due_at.date()
        if c.original_due_at.date() < self.today - DAY and c.status != S.submitted and not confirmed_on_time:
            # not accepted by due + 1 d: the slip label is already known
            party_ = party
            seen: set[date] = set()
            for horizon in (28, 21, 14, 7, 2):
                as_of = max(created + DAY, c.original_due_at.date() - timedelta(days=horizon))
                if as_of in seen or as_of >= self.today:
                    continue
                seen.add(as_of)
                gi = next((g for g in self.gates if g.id == c.gate_instance_id), None)
                self.features.append(self.feature_row(c, party_, sim, gi, as_of, 1.0,
                                                      float((self.today - c.original_due_at.date()).days)))
        return sim

    def current_gates(self) -> None:
        for code, gi in self.current_gate.items():
            s = self.specs[code]
            proj = self.projects[code]
            script = self.scripted.get(code, {})
            review = gi.planned_review_at.date()
            dels = list(gi.deliverables)
            total = sum(d.weight for d in dels) or 1
            # choose accepted set to approach the target readiness (scripted items excluded)
            free = [d for d in dels if d.id not in script]
            self.rng.shuffle(free)
            free.sort(key=lambda d: (not d.mandatory, self.rng.random()))
            goal = s.target_readiness * total - sum(
                d.weight * {"accepted": 1, "submitted": 0.5}.get(script[d.id].get("status", ""), 0)
                for d in dels if d.id in script)
            plan: dict[str, str] = {}
            acc = 0.0
            for d in free:
                if acc + d.weight <= goal + 1e-9:
                    plan[d.id] = "accepted"
                    acc += d.weight
                elif acc + d.weight / 2 <= goal + 1e-9:
                    plan[d.id] = "submitted"
                    acc += d.weight / 2
                else:
                    plan[d.id] = ""
            for d in dels:
                sc = script.get(d.id, {})
                status = sc.get("status", plan.get(d.id, ""))
                party = self.party(sc["party"]) if "party" in sc else self.party_for(s, d)
                owner = self.user(sc["owner"]) if "owner" in sc else self.owner_for(s, d)
                if "due" in sc:
                    due = self.today + timedelta(days=sc["due"])
                elif status == "accepted":
                    due = self.today - timedelta(days=self.rng.randint(1, 40))
                else:
                    due = min(review - DAY, self.today + timedelta(days=self.rng.randint(-6, max(2, (review - self.today).days))))
                created = min(due, self.today) - timedelta(days=self.rng.randint(45, 90))
                c = self.new_commitment(proj, d.name, d.id, m.CommitmentKind.gate_deliverable, owner, party, due,
                                        created, gi, d, self.wp_for(code, party, d.tags))
                if status == "accepted":
                    sim = self.simulate(c, party, created, self.today - DAY, slip=False)
                    self.apply_sim(c, party, sim)
                    when = min(self.today - DAY, sim.submit or self.today - DAY)
                    ev = self.make_evidence(c, when, sc.get("accepted_by"))
                    ev.accepted_at = at(when, 16)
                    c.evidence_id = ev.id
                    c.status = S.accepted
                    c.closed_at = ev.accepted_at
                elif status == "submitted":
                    sim = self.open_commitment(c, party, created, status="submitted", force_silent=False)
                    when = self.today - timedelta(days=sc.get("submitted_days_ago", self.rng.randint(1, 5)))
                    ev = self.make_evidence(c, when, None)
                    ev.accepted_by_user_id = None
                    c.evidence_id = ev.id
                    c.escalation_level = 0
                elif status == "waived":
                    c.status = S.waived
                    c.closed_at = at(self.today - timedelta(days=12))
                else:
                    self.open_commitment(c, party, created, status=sc.get("force_status"),
                                         force_silent=sc.get("silent", False) or c.id in self.silent_overrides)
                    if sc.get("silent"):
                        self.silent_overrides.add(c.id)
                gi.readiness = 0.0

    # --- work package commitments (actions, submittals, tender items) -------------------------

    WP_ITEMS = ["Vendor drawings for review", "RFI response", "Method statement", "Material submittal",
                "Factory acceptance test plan", "Inspection and test plan", "Weekly progress report",
                "Shop drawings", "Interface schedule update", "Technical query answer", "Safety plan update",
                "Sample approval", "Delivery schedule confirmation", "Quality dossier"]

    def pad(self, target: int) -> int:
        """Work-package commitments until the record holds `target` commitments."""
        have = self.store.count(m.Commitment)
        need = target - have
        if need <= 0:
            return 0
        weights = []
        for code, _s in self.specs.items():
            sd = self.projects[code].start_date
            age = (self.today - sd).days if sd else 200
            weights.append((code, age * len(self.packages[code])))
        tot = sum(w for _, w in weights)
        alloc = {code: int(need * w / tot) for code, w in weights}
        rest = need - sum(alloc.values())
        for code, _ in sorted(weights, key=lambda x: -x[1])[:rest]:
            alloc[code] += 1
        self.commitments = []
        self._code = have
        for code, n in alloc.items():
            proj = self.projects[code]
            start = proj.start_date or self.today - timedelta(days=200)
            span = (self.today + timedelta(days=60) - start).days
            for i in range(n):
                wp = self.packages[code][i % len(self.packages[code])]
                party = next(p for p in self.parties.values() if p.id == wp.party_id)
                due = start + timedelta(days=int(span * (i + 0.5) / n) + self.rng.randint(-5, 5))
                created = due - timedelta(days=self.rng.randint(14, 40))
                kind = m.CommitmentKind.tender_item if wp.tender_state != "none" and i % 5 == 0 else m.CommitmentKind.action
                title = f"{self.rng.choice(self.WP_ITEMS)} - {wp.name}"
                owner = self.user(self.specs[code].pm)
                c = self.new_commitment(proj, title, wp.code, kind, owner, party, due, max(created, start), None, None, wp.id)
                if due < self.today:
                    sim = self.close_commitment(c, party, max(created, start), None)
                    if c.status != S.accepted:  # not closed by today: still open, same draw
                        self.open_commitment(c, party, max(created, start), sim=sim)
                else:
                    self.open_commitment(c, party, max(created, start))
        new = list(self.commitments)
        self.store.insert_many(new, actor="system:demo", kind="history")
        return len(new)

    # --- schedules, plans, cost -----------------------------------------------------------------

    def write_schedules(self) -> Path:
        """MSPDI per project, with float and DCMA characteristics from the cases."""
        folder = self.data_dir / "schedules"
        folder.mkdir(parents=True, exist_ok=True)
        for code, s in self.specs.items():
            proj = self.projects[code]
            model = self.models[s.gate_model]
            gates = sorted(model.gates, key=lambda g: g.order)
            acts: list[dict[str, Any]] = []
            uid = 1
            base_float = self.float_now(code)
            failing = set(self.dcma_fail.get(code, []))
            start = proj.start_date or self.today
            # milestones: gate reviews and the named next milestone
            reviews = {g.gate_id: g for g in self.gates if g.project_id == proj.id}
            prev_uid = None
            for g in gates:
                gi = reviews.get(g.id)
                if gi is None:
                    when = self.current_gate[code].planned_review_at.date() + timedelta(days=120 * (g.order - self.current_gate[code].order))
                    pct = 0
                else:
                    when = (gi.actual_review_at or gi.planned_review_at).date()
                    pct = 100 if gi.decision != "pending" else 0
                name = s.next_milestone if gi is not None and gi.decision == "pending" else f"{g.name} review"
                acts.append({"uid": uid, "name": name, "wbs": f"{code}-GATES", "start": when, "finish": when,
                             "milestone": True, "pct": pct, "float": base_float if pct < 100 else 0, "critical": pct < 100,
                             "preds": [prev_uid] if prev_uid else [], "constraint": 0, "lag": 0})
                prev_uid = uid
                uid += 1
            for wp in self.packages[code]:
                ws = wp.window_start or start
                we = wp.window_end or ws + timedelta(days=60)
                for phase, (a, b) in (("design", (-60, -10)), ("install", (0, 0)), ("test", (5, 25))):
                    st = ws + timedelta(days=a) if phase != "install" else ws
                    fn = ws + timedelta(days=b) if phase == "design" else (we if phase == "install" else we + timedelta(days=b))
                    pct = 100 if fn < self.today else (0 if st > self.today else 50)
                    crit = wp.discipline in ("asrs", "fire", "refrigeration", "slab") and pct < 100
                    acts.append({"uid": uid, "name": f"{wp.name} - {phase}", "wbs": wp.code, "start": st, "finish": fn,
                                 "milestone": False, "pct": pct,
                                 "float": base_float if crit else base_float + self.rng.randint(5, 30), "critical": crit,
                                 "preds": [uid - 1] if phase != "design" else [1], "constraint": 0, "lag": 0})
                    uid += 1
            acts.append({"uid": uid, "name": "Project complete", "wbs": f"{code}-GATES",
                         "start": max(a["finish"] for a in acts), "finish": max(a["finish"] for a in acts),
                         "milestone": True, "pct": 0, "float": base_float + 20, "critical": False,
                         "preds": [a["uid"] for a in acts if not a["milestone"] and a["name"].endswith("test")],
                         "constraint": 0, "lag": 0})
            work = [a for a in acts if not a["milestone"]]
            if "leads" in failing:
                work[1]["lag"] = -2
            if "hard_constraints" in failing:
                for act in work[: max(2, len(work) // 8)]:
                    act["constraint"] = 2
            if "logic" in failing:
                for act in work[2:6]:
                    act["preds"] = []
            if "high_float" in failing:
                for act in work[-4:]:
                    act["float"] = 60
            self.write_mspdi(folder / f"{code}.xml", code, s.name, acts)
        return folder

    def float_now(self, code: str) -> float:
        return float(self.pf.FLOAT_NOW.get(code, 12))

    def write_mspdi(self, path: Path, code: str, name: str, acts: list[dict[str, Any]]) -> None:
        mins = 480 * 10
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<Project xmlns="http://schemas.microsoft.com/project">',
                 f"<Name>{code} {name}</Name>", f"<StatusDate>{self.today.isoformat()}T08:00:00</StatusDate>", "<Tasks>"]
        for a in acts:
            preds = "".join(
                f"<PredecessorLink><PredecessorUID>{p}</PredecessorUID><Type>1</Type><LinkLag>{int(a['lag'] * mins)}</LinkLag></PredecessorLink>"
                for p in a["preds"])
            base_fin = a["finish"]
            lines.append(
                f"<Task><UID>{a['uid']}</UID><ID>{a['uid']}</ID><Name>{_x(a['name'])}</Name><WBS>{a['wbs']}</WBS>"
                f"<Start>{a['start'].isoformat()}T08:00:00</Start><Finish>{a['finish'].isoformat()}T17:00:00</Finish>"
                f"<Milestone>{int(a['milestone'])}</Milestone><Critical>{int(a['critical'])}</Critical>"
                f"<PercentComplete>{a['pct']}</PercentComplete><TotalSlack>{int(a['float'] * mins)}</TotalSlack>"
                f"<FreeSlack>0</FreeSlack><ConstraintType>{a['constraint']}</ConstraintType>{preds}"
                f"<Baseline><Number>0</Number><Finish>{base_fin.isoformat()}T17:00:00</Finish></Baseline></Task>")
        lines += ["</Tasks>", "</Project>"]
        path.write_text("\n".join(lines), encoding="utf-8")

    def prior_snapshots(self) -> list[m.ScheduleSnapshot]:
        """A schedule sync 8 days ago so float erosion has a reference."""
        out = []
        for code, proj in self.projects.items():
            out.append(m.ScheduleSnapshot(
                id=new_id("SS"), project_id=proj.id, conn_id="xer-demo", data_date=self.now - timedelta(days=8),
                synced_at=self.now - timedelta(days=8),
                min_float_critical=self.float_now(code) - self.float_change.get(code, 0.0),
                dcma_passed=14 - len(self.dcma_fail.get(code, [])), activity_count=0, changed=True))
        return out

    def write_planner_fixture(self) -> Path:
        """A 212-task plan for the portfolio's planner project; 38 tasks map by label or bucket ( M1)."""
        r = self.rng
        pcode = self.pf.PLANNER_PROJECT
        gi = self.current_gate[pcode]
        dels = [d.id for d in gi.deliverables]
        wps = [wp.code for wp in self.packages[pcode]]
        tasks = []
        for i in range(212):
            if i < 22:
                labels, bucket = [dels[i % len(dels)]], "Gate items"
                title = f"{gi.deliverables[i % len(dels)].name}: sub-task {i // len(dels) + 1}"
            elif i < 38:
                labels, bucket = [], wps[i % len(wps)]
                title = f"{self.packages[pcode][i % len(wps)].name}: coordination item {i}"
            else:
                labels, bucket = [], r.choice(["General", "Meetings", "Admin", "Site visits"])
                title = r.choice(["Book site visit", "Prepare SteerCo slides", "Update contact list", "Order PPE",
                                  "Review meeting notes", "Archive old drawings", "Plan team lunch"]) + f" #{i}"
            due = self.today + timedelta(days=r.randint(-30, 60))
            tasks.append({"id": f"pl-{i:03d}", "title": title, "bucket": bucket, "labels": labels,
                          "due": at(due, 17).isoformat(), "done": due < self.today and r.random() < 0.7,
                          "owner_email": self.user(self.specs[pcode].pm).email})
        path = self.data_dir / "planner-dc.json"
        path.write_text(json.dumps({"plans": {f"{pcode} programme": tasks}}, indent=1), encoding="utf-8")
        return path

    def write_costs(self) -> Path:
        """Monthly cost rows per project: actuals, estimate revisions, contingency draws."""
        folder = self.data_dir / "cost"
        folder.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        for code, proj in self.projects.items():
            s = self.specs[code]
            start = proj.start_date or self.today
            last_gate = max((g.actual_review_at.date() for g in self.gates
                             if g.project_id == proj.id and g.actual_review_at), default=start)
            drift = self.estimate_drift.get(code, self.rng.uniform(-0.01, 0.03))
            rows.append({"project_code": code, "wbs": f"{code}", "kind": "estimate", "amount": round(s.capex, 2),
                         "currency": s.currency, "posted_at": last_gate.isoformat(), "ref": "EST-GATE", "note": "estimate at last gate"})
            rows.append({"project_code": code, "wbs": f"{code}", "kind": "estimate", "amount": round(s.capex * (1 + drift), 2),
                         "currency": s.currency, "posted_at": (self.today - timedelta(days=3)).isoformat(),
                         "ref": "EST-CUR", "note": "current estimate"})
            months = max(1, (self.today - start).days // 30)
            spend = s.capex * s.time_elapsed * 0.6
            for k in range(months):
                d = start + timedelta(days=30 * k + 28)
                rows.append({"project_code": code, "wbs": self.packages[code][k % len(self.packages[code])].code,
                             "kind": "actual", "amount": round(spend / months, 2), "currency": s.currency,
                             "posted_at": d.isoformat(), "ref": f"INV-{code[-2:]}{k:03d}", "note": ""})
            share = self.contingency_share.get(code, s.time_elapsed * self.rng.uniform(0.5, 1.0))
            drawn = proj.contingency_amount * share
            if drawn > 0:
                rows.append({"project_code": code, "wbs": code, "kind": "contingency_draw", "amount": round(drawn, 2),
                             "currency": s.currency, "posted_at": (self.today - timedelta(days=20)).isoformat(),
                             "ref": f"CD-{code[-2:]}", "note": "contingency draw"})
        rows += self.cost_rows
        path = folder / f"cost_{self.today:%Y%m%d}.csv"
        cols = ["project_code", "wbs", "kind", "amount", "currency", "posted_at", "ref", "note"]
        with path.open("w", encoding="utf-8", newline="") as f:
            import csv

            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in cols})
        return folder

    # --- overnight inbound, corpus, priors ---------------------------------------------------------

    def scripted_ids(self) -> set[str]:
        """Commitments a case script controls; overnight fixtures must not change them."""
        keys = {(self.projects[code].id, self.current_gate[code].id, d) for code, ds in self.scripted.items() for d in ds}
        return {c.id for c in self.all_commitments if (c.project_id, c.gate_instance_id, c.deliverable_id) in keys}

    def overnight(self) -> None:
        """Replies and document changes that arrived since yesterday's chases (unprocessed)."""
        r = self.rng
        scripted_ids = self.scripted_ids()
        open_cs = [c for c in self.all_commitments if c.status in (S.open, S.confirmed, S.overdue)
                   and c.last_chase_at and c.id not in self.silent_overrides and c.id not in scripted_ids]
        r.shuffle(open_cs)
        for i, c in enumerate(open_cs[:48]):
            party = next(p for p in self.parties.values() if p.id == c.party_id)
            if party.behaviour == "silent":
                continue
            lang = self.lang(party)
            intent = r.choices(["CONFIRM", "FLAG", "QUESTION", "DELEGATE", "OOO"], [60, 14, 10, 8, 8])[0]
            new_due = c.due_at.date() + timedelta(days=r.randint(7, 21))
            structured = r.random() < 0.45
            text = texts.reply_text(r, intent, lang, c.title, c.due_at.date(), new_due if intent == "FLAG" else None,
                                    texts.reason(r, lang), r.choice(["Ana Ruiz", "Tom Peeters"]), structured)
            with_ref = r.random() < 0.75
            step = max(1, c.chase_count)
            self.overnight_mail.append({
                "id": f"night-{i:03d}", "sender": party.contacts[0].email, "to": ["gatewright@example.com"],
                "subject": f"RE: [{c.code}/{step}] {c.title}" if with_ref else f"RE: {c.title}",
                "text": text + (f"\n\n> Sent by Gatewright | ref {c.code}/{step}" if with_ref else ""),
                "received_at": (self.now - timedelta(hours=r.randint(1, 14))).isoformat(),
                "thread_id": f"t-{c.code}" if with_ref else None,
                "headers": {"x-gw-ref": f"{c.code}/{step}"} if with_ref and r.random() < 0.5 else {}})
        for j in range(8):
            self.overnight_mail.append({
                "id": f"night-noise-{j}", "sender": f"news{j}@vendor-news.{DEMO_DOMAIN}", "to": ["gatewright@example.com"],
                "subject": "Newsletter", "text": r.choice(texts.PROSE["UNRELATED"]["en"]),
                "received_at": (self.now - timedelta(hours=r.randint(1, 14))).isoformat(), "headers": {}})
        scripted = self.scripted_ids()
        subs = [c for c in self.all_commitments if c.status in (S.open, S.overdue, S.flagged) and c.gate_instance_id
                and c.id not in scripted and c.id not in self.silent_overrides]
        r.shuffle(subs)
        for k, c in enumerate(subs[:6]):
            code = self.code_of(c)
            dt = texts.doc_type_for(c.deliverable_id, "document")
            name = texts.doc_name(r, code, c.deliverable_id, dt, k % 3 != 2)
            self.overnight_docs[f"{code}/{name}"] = {
                "name": name, "text": texts.doc_text(r, dt, c.title, c.due_at.date()),
                "modified": (self.now - timedelta(hours=r.randint(2, 12))).isoformat()}

    def corpus(self) -> Path:
        """~2,000 labelled items for training and the F1 gate."""
        r = random.Random(f"corpus:{self.cfg.seed}")
        folder = self.data_dir / "corpus"
        folder.mkdir(parents=True, exist_ok=True)
        cs = [c for c in self.all_commitments if c.kind in ("gate_deliverable", "action", "tender_item")]
        parties = {p.id: p for p in self.parties.values()}
        items = []
        n_msg = int(self.cfg.corpus_size * 0.7)
        for i in range(self.cfg.corpus_size):
            c = r.choice(cs)
            party = parties[c.party_id]
            code = self.code_of(c)
            pspec = self.pspec[party.id]
            lang = pspec.lang if pspec.lang != "en" and r.random() < 0.5 else "en"
            received = (c.original_due_at - timedelta(days=r.randint(0, 6))).isoformat()
            if i < n_msg:
                intent = r.choices(["CONFIRM", "FLAG", "QUESTION", "DELEGATE", "OOO", "DECLINE", "UNRELATED"],
                                   [34, 22, 10, 10, 9, 5, 10])[0]
                structured = intent in ("CONFIRM", "FLAG") and r.random() < 0.35
                new_due = c.original_due_at.date() + timedelta(days=r.randint(5, 40))
                why = texts.reason(r, lang)
                delegate = r.choice(["Ana Ruiz", "Tom Peeters", "Lena Vogel"])
                text = texts.reply_text(r, intent, lang, c.title, c.original_due_at.date(),
                                        new_due if intent == "FLAG" else None, why, delegate, structured)
                with_ref = intent != "UNRELATED" and r.random() < 0.55
                subject = (f"RE: [{c.code}/{r.randint(1, 3)}] {c.title}" if with_ref else
                           (f"RE: {c.title}" if intent != "UNRELATED" else r.choice(["Newsletter", "Invitation", "News"])))
                items.append({"id": f"cx-{i:04d}", "kind": "message", "lang": lang, "subject": subject, "text": text,
                              "received_at": received,
                              "from": party.contacts[0].email, "project": code, "intent": intent,
                              "structured": structured, "commitment": c.code if intent != "UNRELATED" else None,
                              "entities": {"new_due_date": new_due.isoformat() if intent == "FLAG" else None,
                                           "reason": why if intent == "FLAG" else None,
                                           "person": delegate if intent == "DELEGATE" else None}})
            else:
                dt = r.choices(list(texts.DOC_TYPES), [14, 10, 10, 8, 10, 8, 6, 8, 12, 6, 8])[0]
                did = c.deliverable_id if dt != "invoice" else "invoice"
                name = texts.doc_name(r, code, did, dt, r.random() < 0.6)
                items.append({"id": f"cx-{i:04d}", "kind": "document", "lang": "en", "name": name, "received_at": received,
                              "text": texts.doc_text(r, dt, c.title, c.original_due_at.date()), "project": code,
                              "doc_type": dt, "commitment": c.code if dt not in ("invoice", "other") else None})
        path = folder / "corpus.jsonl"
        path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items), encoding="utf-8")
        return path

    def priors(self) -> Path:
        """~600 synthetic prior feature rows, flagged synthetic=1."""
        import pandas as pd

        r = random.Random(f"priors:{self.cfg.seed}")
        rows = []
        kinds = ["prompt", "slow", "silent", "delegating", "flagger"]
        pkinds = ["vendor", "authority", "internal_team", "insurer", "utility", "contractor"]
        for _ in range(self.cfg.priors_size):
            beh = r.choices(kinds, [45, 25, 10, 10, 10])[0]
            pk = r.choice(pkinds)
            prof = BEHAVIOUR[beh]
            chases = r.randint(0, 3)
            silence_rate = prof[1] * r.uniform(0.7, 1.3)
            replies = sum(1 for _ in range(chases) if r.random() > silence_rate)
            silence = 0 if replies == chases else r.randint(1, 10) * (2 if beh == "silent" else 1)
            flagged = int(replies > 0 and r.random() < prof[2])
            base = {"prompt": 0.10, "slow": 0.40, "silent": 0.72, "delegating": 0.28, "flagger": 0.55}[beh]
            p = min(0.95, base + 0.08 * (pk == "authority") + 0.25 * flagged + 0.02 * silence)
            label = 1.0 if r.random() < p else 0.0
            state = {"days_to_due": 7, "deliverable_type": r.choice(["permit", "design", "test", "estimate", "ops", "action"]),
                     "deliverable_mandatory": r.randint(0, 1), "weight": r.choice([0, 5, 10, 15]),
                     "gate_id": r.choice(["fel1", "fel2", "fel3", "execute", "none"]), "gate_model_id": "fel-v3",
                     "project_archetype": r.choice(["greenfield", "brownfield", "extension"]),
                     "country": r.choice(["NL", "ES", "PE", "FR"]), "party_kind": pk,
                     "party_median_reply_days": prof[0], "party_silence_rate": silence_rate,
                     "party_flag_rate": prof[2], "party_sample_n": r.randint(5, 50), "chases_sent": chases,
                     "replies": replies, "silence_days": silence, "escalation_level": min(chases, 2),
                     "owner_open_items": r.randint(2, 14), "owner_overdue_items": r.randint(0, 4),
                     "open_dependencies_on_this": 0, "changes_since_last_gate": r.randint(0, 6),
                     "estimate_class": r.randint(2, 5), "month_of_year": r.randint(1, 12),
                     "vendor_lead_time_class": r.choice(["short", "medium", "long"]), "evidence_submitted": 0,
                     "days_since_last_schedule_sync": r.randint(0, 7), "float_on_path": None,
                     "is_permit": int(pk == "authority"), "is_insurer": int(pk == "insurer"),
                     "prior_slips_same_party_same_type": r.randint(0, 3), "flagged": flagged, "synthetic": 1}
            rows.append({**feat.build(state), "label": label})
        path = self.data_dir / "priors.parquet"
        pd.DataFrame(rows).to_parquet(path, index=False)
        return path

    def fill_inbound(self, target: int) -> None:
        """Newsletters and unrelated mail over 24 months until the history holds ~target items."""
        r = self.rng
        have = self.store.count(m.InboundItem) + len(self.overnight_mail) + len(self.overnight_docs)
        noise = []
        for i in range(max(0, target - have)):
            d = self.today - timedelta(days=r.randint(1, 730))
            lang = r.choice(["en", "en", "en", "nl", "es", "fr", "pt"])
            noise.append(m.InboundItem(
                id=new_id("InboundItem"), conn_id="mail-shared", external_id=f"hist-noise-{i}", kind="message",
                received_at=at(d, r.randint(6, 20)),
                payload={"from": f"info@news{r.randint(1, 40)}.{DEMO_DOMAIN}", "subject": "News",
                         "text": r.choice(texts.PROSE["UNRELATED"].get(lang) or texts.PROSE["UNRELATED"]["en"]), "lang": lang},
                processed_at=at(d, 21), classification={"intent": "UNRELATED", "confidence": 1.0, "source": "history"}))
        self.store.insert_many(noise, actor="system:demo", kind="history")

    # --- flush -----------------------------------------------------------------------------------

    def flush_base(self) -> None:
        st = self.store
        st.insert_many(list(self.users.values()), actor="system:demo")
        st.insert_many(list(self.parties.values()), actor="system:demo")
        st.insert_many(self.lessons, actor="system:demo")
        st.insert_many(list(self.projects.values()), actor="system:demo")
        st.insert_many([wp for wps in self.packages.values() for wp in wps], actor="system:demo")
        st.insert_many(self.gates, actor="system:demo", kind="history")
        st.insert_many(self.commitments, actor="system:demo", kind="history")
        st.insert_many(self.evidence, actor="system:demo", kind="history")
        st.insert_many(self.chases, actor="system:demo", kind="history")
        st.insert_many(self.inbound, actor="system:demo", kind="history")
        self.features_all = list(self.features)
        st.insert_many(self.risks, actor="system:demo")
        st.insert_many(self.decisions, actor="system:demo")
        self.all_commitments = list(self.commitments)
        self.chases, self.inbound, self.evidence, self.features = [], [], [], []

    def flush_features(self) -> None:
        """Write history feature rows with the party profile measured from the generated chase history.

        Live scoring reads `Party.response_profile`, which the signals module measures from chases;
        training rows must use the same measurement, not the behaviour constants.
        """
        import bisect

        from gw.signals.core import Ctx, update_party_profiles

        update_party_profiles(Ctx.load(self.store))
        parties = {p.id: p for p in self.store.all(m.Party)}
        cs = {c.id: c for c in self.store.all(m.Commitment)}
        slips: dict[tuple[str, str], list[datetime]] = {}
        closed: dict[tuple[str, str], list[datetime]] = {}
        for c in cs.values():  # same definition as live_rows(): closed, and slipped, by party and type
            if c.closed_at:
                key = (c.party_id, feat.deliverable_type(c.deliverable_id, str(c.kind)))
                closed.setdefault(key, []).append(c.closed_at)
                if c.closed_at > c.original_due_at + DAY:
                    slips.setdefault(key, []).append(c.closed_at)
        for v in (*slips.values(), *closed.values()):
            v.sort()
        for fr in self.features_all:
            c = cs[fr.target_id]
            prof = parties[c.party_id].response_profile
            key = (c.party_id, feat.deliverable_type(c.deliverable_id, str(c.kind)))
            prior = slips.get(key, [])
            n_closed = bisect.bisect_left(closed.get(key, []), fr.as_of)
            fr.features.update({"party_median_reply_days": float(prof.median_reply_days),
                                "party_silence_rate": float(prof.silence_rate),
                                "party_flag_rate": float(prof.flag_rate), "party_sample_n": float(prof.sample_n),
                                "prior_slips_same_party_same_type": float(bisect.bisect_left(prior, fr.as_of)),
                                "prior_slip_rate_same_party_same_type":
                                    bisect.bisect_left(prior, fr.as_of) / n_closed if n_closed else -1.0})
        self.store.insert_many(self.features_all, actor="system:demo", kind="history")

    def flush_more(self) -> None:
        st = self.store
        st.insert_many(self.evidence, actor="system:demo", kind="history")
        st.insert_many(self.chases, actor="system:demo", kind="history")
        st.insert_many(self.inbound, actor="system:demo", kind="history")
        self.features_all += self.features
        self.all_commitments += self.commitments
        self.chases, self.inbound, self.evidence, self.features = [], [], [], []


def _x(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class DemoResult:
    projects: int
    parties: int
    commitments: int
    inbound_items: int
    data_dir: Path
    sync: dict[str, Any] = field(default_factory=dict)


def connections(data_dir: Path, pf: ModuleType | None = None) -> list[dict[str, Any]]:
    """Demo connections: real file adapters where files exist, fakes backed by fixtures elsewhere."""
    pf = pf or dc_portfolio
    d = data_dir.as_posix()
    conns: list[dict[str, Any]] = [
        {"id": "xer-demo", "contract": "ScheduleSource", "adapter": "xer_file", "name": "MSPDI drop folder (demo)",
         "config": {"folder": f"{d}/schedules"}, "scopes": ["read"], "owner": "pmo", "sync_every_minutes": 30},
        {"id": "planner-dc", "contract": "PlanSource", "adapter": "fake_plan", "name": "Planner, DC programme (fixture)",
         "config": {"fixture": f"{d}/planner-dc.json",
                    "projects": {f"{pf.PLANNER_PROJECT} programme": pf.PLANNER_PROJECT}},
         "scopes": ["read", "comment"], "owner": "pmo", "mapping": {"label_to_deliverable": True, "bucket_to_package": True}},
        {"id": "docs-cp", "contract": "DocumentSource", "adapter": "fake_documents", "name": "Document library (fixture)",
         "config": {"fixture": f"{d}/documents.json"}, "scopes": ["read", "link"], "owner": "pmo"},
        {"id": "mail-shared", "contract": "MessageChannel", "adapter": "fake_mail", "name": "Shared mailbox (fixture + outbox folder)",
         "config": {"fixture": f"{d}/mail.json", "outbox_dir": f"{d}/outbox"}, "scopes": ["read", "send_as", "post_cards"],
         "owner": "pmo", "sync_every_minutes": 5},
        {"id": "cost-drop", "contract": "CostSource", "adapter": "csv_drop", "name": "Cost export drop folder",
         "config": {"folder": f"{d}/cost"}, "scopes": ["read"], "owner": "l.jansen", "sync_every_minutes": 1440},
        {"id": "cal-gates", "contract": "CalendarSource", "adapter": "fake_calendar", "name": "Gate review calendar (fixture)",
         "config": {"fixture": f"{d}/calendar.json"}, "scopes": ["read", "create_events"], "owner": "pmo"},
        {"id": "local-models", "contract": "ModelProvider", "adapter": "local_models", "name": "Local models (hashing embeddings, rules)",
         "config": {}, "scopes": ["infer"], "owner": "pmo"},
        {"id": "tender-room", "contract": "DocumentSource", "adapter": "fake_documents", "name": "Tender room (fixture)",
         "config": {"fixture": f"{d}/tender-room.json", "role": "tender"}, "scopes": ["read"], "owner": "procurement"},
        {"id": "rag-eng", "contract": "RetrievalSource", "adapter": "fake_retrieval", "name": "Engineering library (fixture)",
         "config": {"fixture": f"{d}/rag.json"}, "scopes": ["read"], "owner": "engineering"},
    ]
    return [c for c in conns if pf.HAS_SCHEDULES or c["contract"] != "ScheduleSource"]


def build(store: Store, today: date | None = None, cfg: DemoConfig | None = None,
          data_dir: str | Path = "demo-data", portfolio: str = "dc") -> DemoResult:
    """Build the synthetic portfolio into `store`."""
    from gw.demo.cases import BASE, CASES
    from gw.harness.registry import Registry

    pf = portfolio_module(portfolio)
    cfg = cfg or DemoConfig(cases=pf.CASES, target_commitments=pf.TARGET_COMMITMENTS,
                            target_inbound=pf.TARGET_INBOUND, corpus_size=pf.CORPUS_SIZE, priors_size=pf.PRIORS_SIZE)
    today = today or date.today()
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    clock.freeze(today)
    ids.seed(cfg.seed * 1_000_003 + today.toordinal())
    store.listeners_enabled = False
    try:
        g = Gen(store, today, cfg, data_dir, pf)
        store.set_meta("today", g.now.isoformat())
        store.set_meta("demo_seed", str(cfg.seed))
        store.set_meta("demo_portfolio", pf.NAME)
        store.set_meta("demo_cases", ",".join(cfg.cases))
        store.set_meta("data_dir", data_dir.resolve().as_posix())
        g.build_fixed()
        if pf.NAME == "dc":
            BASE.prepare(g)
        for key in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"):
            if key in cfg.cases:
                CASES[key].prepare(g)
        g.build_projects()
        g.current_gates()
        for key in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"):
            if key in cfg.cases:
                CASES[key].apply(g)
        g.flush_base()
        # fixtures and files, then the real harness pulls them
        if pf.HAS_SCHEDULES:
            g.write_schedules()
            store.insert_many(g.prior_snapshots(), actor="system:demo", kind="history")
        g.write_planner_fixture()
        g.write_costs()
        (data_dir / "mail.json").write_text(json.dumps({"inbound": []}), encoding="utf-8")
        (data_dir / "documents.json").write_text(json.dumps({"docs": {}}), encoding="utf-8")
        (data_dir / "mail.json").write_text(json.dumps({"inbound": g.overnight_mail}, indent=1), encoding="utf-8")
        (data_dir / "documents.json").write_text(json.dumps({"docs": g.overnight_docs}, indent=1), encoding="utf-8")
        if pf.NAME == "dc":
            g.calendar.append({"id": "out-1", "title": "Outage window: DC-03 dock closure for yard resurfacing",
                           "start": at(today + timedelta(days=30)).isoformat(), "end": at(today + timedelta(days=37)).isoformat(),
                           "kind": "outage"})
        (data_dir / "calendar.json").write_text(json.dumps({"events": g.calendar}, indent=1), encoding="utf-8")
        from gw.demo import tender as tender_room

        tender_room.write(data_dir / "tender-room.json", tender_room.build(
            [w for ws in g.packages.values() for w in ws], {p.id: p for p in g.parties.values()}, cfg.seed,
            g.now.isoformat()))
        (data_dir / "rag.json").write_text(json.dumps({"hits": [
            {"id": "eng-001", "title": "House rule: ESFR design requires roof layout", "snippet": "Insurer reviews ESFR design only with the roof layout and PV array plan.", "url": "fake://rag/eng-001"},
            {"id": "eng-002", "title": "Slab flatness DIN 15185 for VNA aisles", "snippet": "Flatness class for VNA trucks per DIN 15185 part 2; TR34 DM classes.", "url": "fake://rag/eng-002"},
            {"id": "eng-003", "title": "Racking package template", "snippet": "Sections: scope, loads, seismic, inspection, warranty, spares, training.", "url": "fake://rag/eng-003"},
        ]}, indent=1), encoding="utf-8")
        reg = Registry(store)
        for spec in connections(data_dir, pf):
            reg.add(spec, actor="system:demo")
        sync = {}
        for cid in (("xer-demo",) if pf.HAS_SCHEDULES else ()) + ("planner-dc", "cost-drop"):
            sync[cid] = reg.sync(cid).model_dump()
        # work-package commitments up to the target count, then history inbound noise
        g.pad(cfg.target_commitments)
        g.flush_more()
        g.overnight()
        (data_dir / "mail.json").write_text(json.dumps({"inbound": g.overnight_mail}, indent=1), encoding="utf-8")
        (data_dir / "documents.json").write_text(json.dumps({"docs": g.overnight_docs}, indent=1), encoding="utf-8")
        g.fill_inbound(cfg.target_inbound)
        for cid in ("mail-shared", "docs-cp"):
            sync[cid] = reg.sync(cid).model_dump()
        g.flush_features()
        g.corpus()
        g.priors()
        # party response profiles sample sizes from the chase history
        store.log("system:demo", "Portfolio", "demo", "demo_built", {"seed": cfg.seed, "today": today.isoformat(),
                                                                     "cases": list(cfg.cases)})
    finally:
        store.listeners_enabled = True
        ids.seed(None)
    return DemoResult(projects=store.count(m.Project), parties=store.count(m.Party),
                      commitments=store.count(m.Commitment), inbound_items=store.count(m.InboundItem),
                      data_dir=data_dir, sync=sync)


def business_day_back(d: date, n: int, country: str) -> date:
    return add_business_days(d, -n, country)
