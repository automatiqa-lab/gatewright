"""Signal functions, party response profiles and the exposure matrix.

Every signal is a pure function of the record
(and, for forecast_slip, of the forecast table). Each computation writes a row with
`inputs` so the war room can show why. Missing inputs are absent, never estimated:
a project run from a task list has no float or DCMA signals.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from gw.policies.store import Policies
from gw.policies.store import current as current_policies
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store
from gw.signals.calendars import business_days_between, extra_from
from gw.signals.readiness import score_instance

S = m.CommitmentStatus
ACTIVE = {S.open, S.flagged, S.overdue}
OPEN = {S.open, S.flagged, S.overdue, S.confirmed}

# direction and anchor for normalisation: anchor is the "nothing wrong" value
LOWER_IS_WORSE = {"chase_response_rate": 1.0, "vendor_response": 1.0, "dcma_pass": 14.0, "float_erosion": 0.0,
                  "site_float_erosion": 0.0}


def norm(signal_id: str, value: float, thresholds: tuple[float, float] | None) -> float | None:
    """Map a value to 0-100 with warn -> 50 and crit -> 85, linear between and beyond."""
    if thresholds is None:
        return None
    warn, crit = thresholds
    if signal_id in LOWER_IS_WORSE:
        a = LOWER_IS_WORSE[signal_id]
        x, w, c = a - value, a - warn, a - crit
    else:
        x, w, c = value, warn, crit
    if w <= 0 or c <= w:
        return 0.0
    if x <= 0:
        return 0.0
    if x <= w:
        return 50.0 * x / w
    if x <= c:
        return 50.0 + 35.0 * (x - w) / (c - w)
    return min(100.0, 85.0 + 35.0 * (x - c) / (c - w))


def level(signal_id: str, value: float | None, thresholds: tuple[float, float] | None) -> str:
    n = norm(signal_id, value, thresholds) if value is not None else None
    if n is None:
        return "none"
    return "crit" if n >= 85 else "warn" if n >= 50 else "ok"


@dataclass
class Ctx:
    """Everything a recompute needs, loaded once."""

    store: Store
    pol: Policies
    now: datetime
    projects: list[m.Project]
    parties: dict[str, m.Party]
    commitments: list[m.Commitment]
    chases: list[m.Chase]
    gates: dict[str, m.GateInstance]
    evidence: dict[str, m.Evidence]
    deps: list[m.Dependency]
    packages: dict[str, m.WorkPackage]
    snapshots: dict[str, list[m.ScheduleSnapshot]]
    cost: dict[str, list[m.CostRow]]
    forecast: dict[str, float]
    rows: list[m.SignalRow] = field(default_factory=list)

    @classmethod
    def load(cls, store: Store) -> Ctx:
        snaps: dict[str, list[m.ScheduleSnapshot]] = defaultdict(list)
        for s in store.find(m.ScheduleSnapshot, order_by="synced_at"):
            snaps[s.project_id].append(s)
        cost: dict[str, list[m.CostRow]] = defaultdict(list)
        for r in store.find(m.CostRow, order_by="posted_at"):
            cost[r.project_code].append(r)
        return cls(
            store=store, pol=current_policies(store), now=clock.now(), projects=store.find(m.Project, order_by="code"),
            parties={p.id: p for p in store.all(m.Party)}, commitments=store.all(m.Commitment),
            chases=store.all(m.Chase), gates={g.id: g for g in store.all(m.GateInstance)},
            evidence={e.id: e for e in store.all(m.Evidence)}, deps=store.all(m.Dependency),
            packages={w.id: w for w in store.all(m.WorkPackage)}, snapshots=snaps, cost=cost,
            forecast={f.target_id: f.p for f in store.find(m.ForecastRow, target="slip", order_by="created_at")})

    def th(self, sid: str) -> tuple[float, float] | None:
        return self.pol.thresholds(sid)

    def emit(self, sid: str, scope: str, scope_id: str, value: float | None, inputs: dict[str, Any],
             threshold: tuple[float, float] | None = None) -> float | None:
        if value is None:
            return None
        t = threshold if threshold is not None else self.th(sid)
        self.rows.append(m.SignalRow(signal_id=sid, scope=scope, scope_id=scope_id, computed_at=self.now,
                                     value=float(value), threshold=list(t) if t else None, inputs=inputs))
        return float(value)


# --- per-signal functions ------------------------------------------------------------------


def silence_days(ctx: Ctx, c: m.Commitment) -> int:
    """Business days since the first unanswered chase."""
    if c.status not in ACTIVE or c.first_unanswered_chase_at is None:
        return 0
    party = ctx.parties.get(c.party_id)
    return business_days_between(c.first_unanswered_chase_at, ctx.now, party.country if party else "NL",
                                 extra_from(ctx.pol))


def party_silence(ctx: Ctx, chases: list[m.Chase]) -> tuple[int, str | None]:
    """Party scope: business days since the first chase sent after the party's last reply."""
    sent = [c for c in chases if c.status == "sent" and c.sent_at]
    if not sent:
        return 0, None
    last_reply = max((c.replied_at for c in sent if c.replied_at), default=None)
    after = [c.sent_at for c in sent if c.sent_at is not None and not c.replied_at
             and (last_reply is None or c.sent_at > last_reply)]
    if not after:
        return 0, None
    first = min(after)
    party = ctx.parties.get(sent[0].party_id)
    return business_days_between(first, ctx.now, party.country if party else "NL"), first.isoformat()


def due_in_n(ctx: Ctx, cs: list[m.Commitment]) -> tuple[int, int, list[str]]:
    n = int(ctx.pol.get("signals.due_window_days", 5))
    lim = ctx.now + timedelta(days=n)
    due = [c for c in cs if c.due_at <= lim and c.status in ACTIVE]
    unconf = [c for c in due if c.status == S.open and c.last_reply_at is None]
    return len(due), len(unconf), [c.code for c in unconf][:20]


def response_rate(ctx: Ctx, chases: list[m.Chase], days: int) -> tuple[float | None, int, int]:
    since = ctx.now - timedelta(days=days)
    sent = [c for c in chases if c.status == "sent" and c.sent_at and c.sent_at >= since]
    if not sent:
        return None, 0, 0
    replied = 0
    for ch in sent:
        if ch.replied_at:
            party = ctx.parties.get(ch.party_id)
            within = int(ctx.pol.get("signals.reply_within_business_days", 2))
            if business_days_between(ch.sent_at, ch.replied_at,  # type: ignore[arg-type]
                                     party.country if party else "NL") <= within:
                replied += 1
    return replied / len(sent), replied, len(sent)


def float_erosion(ctx: Ctx, p: m.Project) -> tuple[float | None, dict[str, Any]]:
    snaps = ctx.snapshots.get(p.id) or []
    if not snaps:
        return None, {}
    now = snaps[-1]
    ref = [s for s in snaps if s.synced_at <= now.synced_at - timedelta(days=7)]
    if not ref:
        return None, {"reason": "no sync 7 days ago"}
    return now.min_float_critical - ref[-1].min_float_critical, {
        "float_now": now.min_float_critical, "float_ref": ref[-1].min_float_critical,
        "ref_synced": ref[-1].synced_at.isoformat(), "conn": now.conn_id}


def estimate_drift(ctx: Ctx, p: m.Project) -> tuple[float | None, dict[str, Any]]:
    est = [r for r in ctx.cost.get(p.code, []) if r.kind == "estimate"]
    if len(est) < 1:
        return None, {}
    passed = [g.decided_at for g in ctx.gates.values() if g.project_id == p.id and g.decided_at
              and g.decision in ("pass", "pass_with_actions")]
    last = max(passed).date() if passed else None
    base = [r for r in est if last is None or r.posted_at <= last] or est[:1]
    b, cur = base[-1].amount, est[-1].amount
    return (cur - b) / b if b else None, {"estimate_at_gate": b, "estimate_now": cur, "currency": est[-1].currency}


def contingency_burn(ctx: Ctx, p: m.Project) -> tuple[float | None, dict[str, Any]]:
    if not p.contingency_amount or not p.start_date or not p.baseline_finish:
        return None, {}
    total = (p.baseline_finish - p.start_date).days
    elapsed = (ctx.now.date() - p.start_date).days
    time_share = min(1.0, max(0.0, elapsed / total)) if total > 0 else 1.0
    drawn = p.contingency_drawn / p.contingency_amount
    return drawn - time_share, {"drawn_share": round(drawn, 3), "time_share": round(time_share, 3),
                                "drawn": p.contingency_drawn, "contingency": p.contingency_amount}


def gap_share(gi: m.GateInstance | None, cs: list[m.Commitment], tag: str, submitted: float) -> float | None:
    """Share of tagged deliverable weight not credited in the current gate."""
    if gi is None:
        return None
    by = {c.deliverable_id: c for c in cs if c.gate_instance_id == gi.id and c.kind == "gate_deliverable"}
    num = den = 0.0
    for d in gi.deliverables:
        if tag not in d.tags:
            continue
        c = by.get(d.id)
        if c is not None and c.status == S.waived:
            continue
        credit = 1.0 if c and c.status == S.accepted else submitted if c and c.status == S.submitted else 0.0
        num += d.weight * (1 - credit)
        den += d.weight
    return num / den if den else None


def weights_or_equal(cs: list[m.Commitment]) -> list[float]:
    """ forecast_slip: weight by deliverable `weight`; equal weights when none carries one."""
    ws = [float(c.weight) for c in cs]
    return ws if sum(ws) > 0 else [1.0] * len(cs)


def mean_or_none(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


# --- recompute -------------------------------------------------------------------------------


def update_party_profiles(ctx: Ctx) -> None:
    """Response profile per party over the last 180 days. Derived, written only here."""
    since = ctx.now - timedelta(days=180)
    by_party: dict[str, list[m.Chase]] = defaultdict(list)
    for ch in ctx.chases:
        if ch.status == "sent" and ch.sent_at and ch.sent_at >= since:
            by_party[ch.party_id].append(ch)
    for pid, chs in by_party.items():
        party = ctx.parties[pid]
        delays = [business_days_between(c.sent_at, c.replied_at, party.country) for c in chs if c.replied_at and c.sent_at]
        silent = sum(1 for c in chs if c.sent_at is not None
                     and (c.replied_at is None or business_days_between(c.sent_at, c.replied_at, party.country) > 5))
        replies = [c for c in chs if c.replied_at]
        prof = m.ResponseProfile(median_reply_days=float(statistics.median(delays)) if delays else 0.0,
                                 silence_rate=round(silent / len(chs), 3),
                                 flag_rate=round(sum(1 for c in replies if c.reply_intent == "FLAG") / len(replies), 3) if replies else 0.0,
                                 sample_n=len(chs))
        if prof != party.response_profile:
            party.response_profile = prof
            ctx.store.update(party, actor="system:signals", kind="profile")


def recompute(store: Store) -> list[m.SignalRow]:
    """Full pass: every signal for every scope, the exposure matrix, cached readiness. Writes rows."""
    ctx = Ctx.load(store)
    update_party_profiles(ctx)
    pol = ctx.pol
    submitted = float(pol.get("gates.submitted_counts_as", 0.5))
    by_project: dict[str, list[m.Commitment]] = defaultdict(list)
    for c in ctx.commitments:
        by_project[c.project_id].append(c)
    chases_by_project: dict[str, list[m.Chase]] = defaultdict(list)
    for ch in ctx.chases:
        chases_by_project[ch.project_id].append(ch)
    silence = {c.id: silence_days(ctx, c) for c in ctx.commitments if c.status in ACTIVE}
    for cid, v in silence.items():
        if v:
            th = ctx.th("silence_days")
            ctx.emit("silence_days", "commitment", cid, v, {"business_days": v},
                     (th[0] if th else 3.0, float(pol.get("escalation.silence_days_to_l3", 5))))
    for cid, pv in ctx.forecast.items():
        ctx.emit("forecast_slip", "commitment", cid, pv, {"model": "forecast table"})

    # portfolio and party scopes
    n_due, n_unconf, codes = due_in_n(ctx, ctx.commitments)
    ctx.emit("due_in_n", "portfolio", "portfolio", n_due, {"window_days": pol.get("signals.due_window_days", 5)}, None)
    ctx.emit("unconfirmed_due_in_n", "portfolio", "portfolio", n_unconf, {"commitments": codes})
    for days in (int(pol.get("signals.lookback_days", 30)),):
        rate, rep, sent = response_rate(ctx, ctx.chases, days)
        r7, rep7, sent7 = response_rate(ctx, ctx.chases, 7)
        ctx.emit("chase_response_rate", "portfolio", "portfolio", rate,
                 {"replied_30d": rep, "sent_30d": sent, "rate_7d": r7, "replied_7d": rep7, "sent_7d": sent7})
    queue = sum(1 for i in store.find(m.InboundItem, review_state="pending"))
    ctx.emit("review_queue_depth", "portfolio", "portfolio", queue, {"pending": queue})
    chases_by_party: dict[str, list[m.Chase]] = defaultdict(list)
    for ch in ctx.chases:
        chases_by_party[ch.party_id].append(ch)
    for pid, _party in ctx.parties.items():
        cs = [c for c in ctx.commitments if c.party_id == pid]
        d, u, codes_p = due_in_n(ctx, cs)
        if d:
            ctx.emit("due_in_n", "party", pid, d, {}, None)
        rate, rep, sent = response_rate(ctx, chases_by_party[pid], int(pol.get("signals.lookback_days", 30)))
        ctx.emit("chase_response_rate", "party", pid, rate, {"replied": rep, "sent": sent})
        sil, since = party_silence(ctx, chases_by_party[pid])
        if sil:
            ctx.emit("silence_days", "party", pid, sil, {"unanswered_since": since})

    # project scope
    weights = pol.exposure_weights()
    for p in ctx.projects:
        cs = by_project[p.id]
        gi = ctx.gates.get(p.current_gate_instance_id or "")
        score = score_instance(store, gi, submitted, ctx.forecast) if gi else None
        if gi and score:
            if abs(gi.readiness - score.readiness) > 1e-9:
                gi.readiness = score.readiness
                store.update(gi, actor="system:signals", kind="readiness", payload={"readiness": score.readiness})
            days = (gi.planned_review_at - ctx.now).days
            ctx.emit("gate_readiness", "gate", gi.id, score.readiness,
                     {"project": p.code, "gate": gi.gate_id, "threshold": gi.threshold, "review_in_days": days,
                      "level": "crit" if score.readiness < gi.threshold and days <= int(ctx.pol.get("signals.gate_horizon_days", 30)) else
                      "warn" if score.readiness < gi.threshold else "ok"}, (gi.threshold, gi.threshold - 0.15))
            ctx.emit("gate_at_risk", "project", p.id, float(score.readiness < gi.threshold and days <= int(ctx.pol.get("signals.gate_horizon_days", 30))),
                     {"readiness": round(score.readiness, 3), "threshold": gi.threshold, "review_in_days": days}, None)
        d, u, codes_p = due_in_n(ctx, cs)
        ctx.emit("due_in_n", "project", p.id, d, {}, None)
        ctx.emit("unconfirmed_due_in_n", "project", p.id, u, {"commitments": codes_p})
        fe, fe_in = float_erosion(ctx, p)
        ctx.emit("float_erosion", "project", p.id, fe, fe_in)
        snaps = ctx.snapshots.get(p.id) or []
        changed = [s for s in snaps if s.changed]
        if changed:
            ctx.emit("schedule_staleness", "project", p.id, (ctx.now - changed[-1].synced_at).days,
                     {"last_change": changed[-1].synced_at.isoformat()})
            if snaps[-1].dcma_passed is not None:
                ctx.emit("dcma_pass", "project", p.id, snaps[-1].dcma_passed, {"conn": snaps[-1].conn_id})
        ed, ed_in = estimate_drift(ctx, p)
        ctx.emit("estimate_drift", "project", p.id, ed, ed_in)
        cb, cb_in = contingency_burn(ctx, p)
        ctx.emit("contingency_burn", "project", p.id, cb, cb_in)
        if gi and (gi.planned_review_at - ctx.now).days <= int(ctx.pol.get("signals.gate_horizon_days", 30)):
            share = gap_share(gi, cs, "ops", 0.0)
            ctx.emit("ops_readiness_gap", "project", p.id, share, {"gate": gi.gate_id})
        open_due30 = [c for c in cs if c.status in OPEN and c.due_at <= ctx.now + timedelta(days=int(ctx.pol.get("signals.gate_horizon_days", 30))) and c.id in ctx.forecast]
        if open_due30:
            w = weights_or_equal(open_due30)
            ctx.emit("forecast_slip", "project", p.id,
                     sum(ctx.forecast[c.id] * wi for c, wi in zip(open_due30, w, strict=True)) / sum(w),
                     {"commitments": len(open_due30)})
        # exposure
        inputs = exposure_inputs(ctx, p, cs, gi, chases_by_project[p.id], silence, submitted)
        dims = (store_dims(store, p) or list(weights))
        for dim in dims:
            val, drivers = exposure_cell(ctx, weights.get(dim, {}), inputs)
            ctx.emit(f"exposure.{dim}", "project", p.id, val, {"drivers": drivers}, None)
    _trend(store, ctx)
    store.write_signals(ctx.rows)
    return ctx.rows


def store_dims(store: Store, p: m.Project) -> list[str]:
    from gw.standards import gatemodels as gm

    try:
        return gm.get(store, p.gate_model_id, p.gate_model_rev).dimensions
    except Exception:
        return []


def exposure_inputs(ctx: Ctx, p: m.Project, cs: list[m.Commitment], gi: m.GateInstance | None,
                    chases: list[m.Chase], silence: dict[str, int], submitted: float) -> dict[str, tuple[float, str]]:
    """Derived inputs for the exposure dimensions; each value carries a short 'why'."""
    out: dict[str, tuple[float, str]] = {}
    parties = ctx.parties
    active = [c for c in cs if c.status in ACTIVE]
    auth = [c for c in active if parties[c.party_id].kind == "authority"]
    if auth:
        worst = max(auth, key=lambda c: silence.get(c.id, 0))
        out["authority_silence"] = (float(silence.get(worst.id, 0)),
                                    f"{parties[worst.party_id].name} silent {silence.get(worst.id, 0)} bd on {worst.code}")
    n = int(ctx.pol.get("signals.due_window_days", 5))
    permit = [c for c in active if "permits" in c.tags and c.last_reply_at is None
              and c.due_at <= ctx.now + timedelta(days=n)]
    if gi is not None and any("permits" in d.tags for d in gi.deliverables):
        out["permit_unconfirmed"] = (float(len(permit)), f"{len(permit)} permit items unconfirmed, due in {n} d")
    for tag, key in (("design", "design_gap"), ("systems", "systems_gap"), ("safety", "hse_status"),
                     ("people", "people_gap"), ("customers", "customers_gap"), ("compliance", "compliance_gap")):
        g = gap_share(gi, cs, tag, submitted)
        if g is not None:
            out[key] = (g, f"{g:.0%} of {tag} deliverable weight not accepted")
    rej = [e for e in ctx.evidence.values() if e.rejected_reason and e.received_at >= ctx.now - timedelta(days=int(ctx.pol.get("signals.lookback_days", 30)))
           and e.commitment_id in {c.id for c in cs}]
    out["evidence_rejections_30d"] = (float(len(rej)), f"{len(rej)} evidence rejections in 30 d")
    vendor_kinds = {"vendor", "contractor"}
    vch = [ch for ch in chases if parties[ch.party_id].kind in vendor_kinds]
    rate, rep, sent = response_rate(ctx, vch, int(ctx.pol.get("signals.lookback_days", 30)))
    if rate is not None:
        out["vendor_response"] = (rate, f"vendors answered {rep}/{sent} chases within 2 bd (30 d)")
        replies = [ch for ch in vch if ch.replied_at and ch.sent_at and ch.sent_at >= ctx.now - timedelta(days=int(ctx.pol.get("signals.lookback_days", 30)))]
        if replies:
            fr = sum(1 for ch in replies if ch.reply_intent == "FLAG") / len(replies)
            out["vendor_flag_rate"] = (fr, f"{fr:.0%} of vendor replies were FLAG")
    vf = [ctx.forecast[c.id] for c in active if parties[c.party_id].kind in vendor_kinds and c.id in ctx.forecast]
    if vf:
        out["vendor_forecast_slip"] = (sum(vf) / len(vf), f"mean P(slip) {sum(vf) / len(vf):.2f} on vendor items")
    sf = [ctx.forecast[c.id] for c in active if "systems" in c.tags and c.id in ctx.forecast]
    if sf:
        out["integrator_forecast_slip"] = (sum(sf) / len(sf), f"mean P(slip) {sum(sf) / len(sf):.2f} on systems items")
    fe, fe_in = float_erosion(ctx, p)
    if fe is not None:
        out["float_erosion"] = (fe, f"critical float {fe:+.0f} d since {fe_in.get('ref_synced', '')[:10]}")
        out["site_float_erosion"] = out["float_erosion"]
    snaps = ctx.snapshots.get(p.id) or []
    if snaps:
        out["schedule_staleness"] = (float((ctx.now - max(s.synced_at for s in snaps if s.changed)).days)
                                     if any(s.changed for s in snaps) else 30.0, "days since schedule change")
        if snaps[-1].dcma_passed is not None:
            out["dcma_pass"] = (float(snaps[-1].dcma_passed), f"DCMA {snaps[-1].dcma_passed}/14")
    ed, _ = estimate_drift(ctx, p)
    if ed is not None:
        out["estimate_drift"] = (ed, f"estimate {ed:+.1%} since last gate")
    cb, cb_in = contingency_burn(ctx, p)
    if cb is not None:
        out["contingency_burn"] = (cb, f"contingency {cb_in['drawn_share']:.0%} drawn at {cb_in['time_share']:.0%} time")
    if gi is not None:
        g = gap_share(gi, cs, "ops", 0.0)
        if g is not None:
            out["ops_readiness_gap"] = (g, f"{g:.0%} of ops deliverables not started")
    hse = [c for c in active if "safety" in c.tags]
    if hse:
        out["reviewer_silence"] = (float(max(silence.get(c.id, 0) for c in hse)), "HSE reviewer silence (bd)")
    my_wps = {w for w, pk in ctx.packages.items() if pk.project_id == p.id}
    deps = [d for d in ctx.deps if d.from_work_package_id in my_wps or d.to_work_package_id in my_wps]
    out["deps_unreviewed"] = (float(sum(1 for d in deps if d.status == "proposed")), "dependency proposals not reviewed")
    out["deps_at_risk"] = (float(sum(1 for d in deps if d.status == "accepted")), "accepted dependencies")
    due30 = [c for c in active if c.id in ctx.forecast and c.due_at <= ctx.now + timedelta(days=int(ctx.pol.get("signals.gate_horizon_days", 30)))]
    if due30:
        ws = weights_or_equal(due30)
        val = sum(ctx.forecast[c.id] * w for c, w in zip(due30, ws, strict=True)) / sum(ws)
        out["forecast_slip"] = (val, f"P(slip) {val:.2f}, weighted mean over {len(due30)} items due in 30 d")
    return out


def exposure_cell(ctx: Ctx, weights: dict[str, float], inputs: dict[str, tuple[float, str]]) -> tuple[float | None, list[dict[str, Any]]]:
    """Weighted blend over available inputs; weights renormalise over what is present."""
    num = den = 0.0
    drivers: list[dict[str, Any]] = []
    for sid, w in weights.items():
        if sid not in inputs:
            continue
        val, why = inputs[sid]
        n = norm(sid, val, ctx.th(sid))
        if n is None:
            continue
        num += w * n
        den += w
        drivers.append({"signal": sid, "value": round(val, 3), "norm": round(n, 1), "weight": w, "why": why})
    if den == 0:
        return None, []
    drivers.sort(key=lambda d: -float(d["norm"]) * float(d["weight"]))
    return max(0.0, min(100.0, num / den)), drivers


def _trend(store: Store, ctx: Ctx) -> None:
    """trend_7d = value - value 7 days ago, same scope."""
    ref_time = ctx.now - timedelta(days=7)
    for row in ctx.rows:
        hist = store.signal_history(row.signal_id, row.scope, row.scope_id)
        old = [h for h in hist if h.computed_at <= ref_time]
        row.trend_7d = row.value - old[-1].value if old else None


def commitment_load(store: Store, weeks: int = 12) -> dict[str, Any]:
    """load[party][ISO week] for the next 12 weeks, with silent/slow tags."""
    now = clock.now()
    start = (now - timedelta(days=now.weekday())).date()
    cols = [(start + timedelta(weeks=i)) for i in range(weeks)]
    ctx = Ctx.load(store)
    silent_after = int((ctx.pol.thresholds("silence_days") or (3, 5))[0])
    slow_days = float(ctx.pol.get("signals.slow_reply_days", 5))
    rows: list[dict[str, Any]] = []
    for party in ctx.parties.values():
        counts = [0] * weeks
        for c in ctx.commitments:
            if c.party_id != party.id or c.status not in OPEN:
                continue
            idx = (c.due_at.date() - start).days // 7
            if 0 <= idx < weeks:
                counts[idx] += 1
        if not sum(counts):
            continue
        sil, _ = party_silence(ctx, [ch for ch in ctx.chases if ch.party_id == party.id])
        tags = []
        if sil >= silent_after:
            tags.append("silent")
        if party.response_profile.median_reply_days > slow_days or party.behaviour == "slow" and \
                party.response_profile.median_reply_days >= silent_after:
            tags.append("slow")
        rows.append({"party_id": party.id, "party": party.name, "kind": str(party.kind), "counts": counts,
                     "tags": tags, "silence_days": sil, "total": sum(counts)})
    rows.sort(key=lambda r: (-("silent" in r["tags"]), -r["total"]))
    return {"weeks": [f"W{d.isocalendar()[1]:02d}" for d in cols], "week_starts": [d.isoformat() for d in cols],
            "rows": rows}


def snapshot_automation_readiness(store: Store) -> list[m.SignalRow]:
    """Nightly per-agent automation readiness as signal rows (; scope agent)."""
    from gw.governance.core import readiness_score

    pol = current_policies(store)
    res = readiness_score(store, pol)
    now = clock.now()
    rows = [m.SignalRow(signal_id="automation_readiness", scope="agent", scope_id=agent, computed_at=now,
                        value=float(score), inputs={"window_days": res["window_days"]})
            for agent, score in (res.get("per_agent") or {}).items() if score is not None]
    if res.get("overall") is not None:
        rows.append(m.SignalRow(signal_id="automation_readiness", scope="portfolio", scope_id="portfolio",
                                computed_at=now, value=float(res["overall"]), inputs={"decided": res.get("decided")}))
    store.write_signals(rows)
    return rows
