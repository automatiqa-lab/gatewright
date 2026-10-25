"""Prediction engine: context, scoring, calibration, explanations, what-if.

Advisory only: the engine reads the record and
writes nothing but forecast, feature and calibration rows through `ForecastWriter`
(a runtime guard). It never changes a status and never sends anything.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gw.engines.forecast import features as feat
from gw.errors import Forbidden
from gw.harness.adapters.tabular import CatBoostAdapter, MitraAdapter, frame
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store
from gw.signals.calendars import business_days_between

ALLOWED_WRITES = (m.ForecastRow, m.CalibrationRow, m.FeatureRow)
WHATIF: dict[str, dict[str, Any]] = {
    "escalate_today": {"label": "Escalate today", "set": {"silence_days": 0, "escalation_level": 3}},
    "evidence_submitted": {"label": "Partial or split submission agreed", "set": {"evidence_submitted": 1, "flagged": 0}},
    "confirm_date": {"label": "Party confirms the date", "set": {"replies": "+1", "silence_days": 0, "flagged": 0}},
    "move_review": {"label": "Move the review 14 days", "set": {"days_to_due": "+14"}},
}


class ForecastWriter:
    """Runtime guard: the only way the engine writes, and only forecast tables."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def insert_many(self, rows: list[Any]) -> None:
        if any(not isinstance(r, ALLOWED_WRITES) for r in rows):
            raise Forbidden("the forecast engine writes only forecast tables", limit="forecast_advisory")
        if rows:
            self._store.insert_many(rows, actor="agent:forecast", kind="forecast")


def priors_path(store: Store) -> Path | None:
    base = store.get_meta("data_dir")
    p = Path(base) / "priors.parquet" if base else None
    return p if p and p.exists() else None


def matured(fr: m.FeatureRow, now: Any) -> bool:
    """A label is usable once its outcome is decidable: due + 1 d has passed.

    Earlier rows are censored - only early (on-time) closures could carry a label - and bias the
    newest slice towards no-slip.
    """
    due = fr.as_of + timedelta(days=float(fr.features.get("days_to_due", 0)))
    return fr.label is not None and due + timedelta(days=1) < now


def context(store: Store, pol: Policies) -> pd.DataFrame:
    """Closed commitments (label known) in the last 24 months, newest first, plus synthetic priors."""
    now = clock.now()
    rows = []
    for fr in store.find(m.FeatureRow, target="slip"):
        if not matured(fr, now) or fr.as_of < now - timedelta(days=730):
            continue
        rows.append({**fr.features, "label": fr.label, "delay_days": fr.delay_days or 0.0, "_as_of": fr.as_of})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("_as_of", ascending=False)
    cap = int(pol.get("forecast.context_max_rows", 10000))
    if pol.get("forecast.include_synthetic_priors", True):
        pp = priors_path(store)
        if pp is not None:
            pri = pd.read_parquet(pp)
            pri["delay_days"] = pri.get("delay_days", pd.Series([0.0] * len(pri)))
            df = pd.concat([df, pri], ignore_index=True)
    return df.head(cap).reset_index(drop=True)


@dataclass
class Live:
    commitment: m.Commitment
    row: dict[str, Any]


def live_rows(store: Store) -> list[Live]:
    """Feature rows for open commitments from the record now (same definition as history)."""
    now = clock.now()
    parties = {p.id: p for p in store.all(m.Party)}
    projects = {p.id: p for p in store.all(m.Project)}
    gates = {g.id: g for g in store.all(m.GateInstance)}
    chases_by: dict[str, list[m.Chase]] = defaultdict(list)
    for ch in store.all(m.Chase):
        chases_by[ch.commitment_id].append(ch)
    cs = store.all(m.Commitment)
    open_cs = [c for c in cs if c.status in ("open", "flagged", "overdue", "confirmed", "submitted")]
    owner_open: dict[str, int] = defaultdict(int)
    owner_overdue: dict[str, int] = defaultdict(int)
    for c in open_cs:
        owner_open[c.owner_user_id] += 1
        if c.status == "overdue":
            owner_overdue[c.owner_user_id] += 1
    prior_slips: dict[tuple[str, str], int] = defaultdict(int)
    prior_closed: dict[tuple[str, str], int] = defaultdict(int)
    for c in cs:
        if c.closed_at:
            key = (c.party_id, feat.deliverable_type(c.deliverable_id, str(c.kind)))
            prior_closed[key] += 1
            if c.closed_at > c.original_due_at + timedelta(days=1):
                prior_slips[key] += 1
    deps_on: dict[str, int] = defaultdict(int)
    for d in store.find(m.Dependency, status=["proposed", "accepted"]):
        deps_on[d.to_work_package_id] += 1
    snaps = {s.project_id: s for s in store.find(m.ScheduleSnapshot, order_by="synced_at")}
    out = []
    for c in open_cs:
        party = parties[c.party_id]
        project = projects[c.project_id]
        gi = gates.get(c.gate_instance_id or "")
        chs = [ch for ch in chases_by[c.id] if ch.status == "sent" and ch.sent_at and ch.sent_at <= now]
        vtag = ""
        silence = business_days_between(c.first_unanswered_chase_at, now, party.country) if c.first_unanswered_chase_at else 0
        prof = party.response_profile
        snap = snaps.get(c.project_id)
        dt = feat.deliverable_type(c.deliverable_id, str(c.kind))
        state = {
            "days_to_due": (c.due_at.date() - now.date()).days, "deliverable_type": dt,
            "deliverable_mandatory": int(c.mandatory), "weight": c.weight, "gate_id": gi.gate_id if gi else "none",
            "gate_model_id": project.gate_model_id, "project_archetype": str(project.archetype),
            "country": project.country, "party_kind": str(party.kind),
            "party_median_reply_days": prof.median_reply_days, "party_silence_rate": prof.silence_rate,
            "party_flag_rate": prof.flag_rate, "party_sample_n": prof.sample_n, "chases_sent": len(chs),
            "replies": sum(1 for ch in chs if ch.replied_at), "silence_days": silence,
            "escalation_level": c.escalation_level, "owner_open_items": owner_open[c.owner_user_id],
            "owner_overdue_items": owner_overdue[c.owner_user_id],
            "open_dependencies_on_this": deps_on.get(c.work_package_id or "", 0),
            "changes_since_last_gate": 0, "estimate_class": project.estimate_class,
            "month_of_year": c.due_at.month, "vendor_lead_time_class": feat.LEAD_TIME_CLASS.get(vtag, "short"),
            "evidence_submitted": int(c.status == "submitted"),
            "days_since_last_schedule_sync": (now - snap.synced_at).days if snap else None,
            "float_on_path": None, "is_permit": int(party.kind == "authority"), "is_insurer": int(party.kind == "insurer"),
            "prior_slips_same_party_same_type": prior_slips[(c.party_id, dt)],
            "prior_slip_rate_same_party_same_type": (prior_slips[(c.party_id, dt)] / prior_closed[(c.party_id, dt)]
                                                     if prior_closed[(c.party_id, dt)] else -1.0),
            "flagged": int(c.status == "flagged"), "synthetic": 0,
        }
        out.append(Live(c, feat.build(state)))
    return out


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> tuple[float, list[dict[str, Any]]]:
    """Expected calibration error with equal-width bins and the reliability table."""
    edges = np.linspace(0, 1, bins + 1)
    table = []
    err = 0.0
    for i in range(bins):
        mask = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if not mask.any():
            continue
        conf, acc = float(p[mask].mean()), float(y[mask].mean())
        err += mask.mean() * abs(conf - acc)
        table.append({"bin": i, "lo": round(edges[i], 2), "hi": round(edges[i + 1], 2), "n": int(mask.sum()),
                      "predicted": round(conf, 3), "observed": round(acc, 3)})
    return float(err), table


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


_WHATIF_FITS: dict[tuple[Any, ...], Any] = {}


class Engine:
    """One scoring run. Model selection: configured model unless unavailable or failing calibration."""

    def __init__(self, store: Store, pol: Policies) -> None:
        self.store = store
        self.pol = pol
        self.writer = ForecastWriter(store)
        self.degraded: str | None = None
        configured = str(pol.get("forecast.model", "catboost"))
        self.adapter: Any = CatBoostAdapter()
        candidates = self.candidates()
        by_id = {a.model_id.split("@")[0]: a for a in candidates}
        preferred = by_id.get(configured)
        if preferred is None and configured != "catboost":
            self.degraded = f"{configured} unavailable or retired; choosing among available models"
        best = self._best_calibrated(candidates)
        if preferred is not None and (best is None or best is preferred or not self._clearly_better(best, preferred)):
            self.adapter = preferred
        elif best is not None:
            self.adapter = best
        if self.degraded and isinstance(self.adapter, CatBoostAdapter):
            self.degraded = f"{configured} unavailable or retired; using catboost fallback"

    def candidates(self) -> list[Any]:
        """Healthy, not retired tabular models: registered providers (GPU box, local TabICL, opted-in
        TabPFN), local Mitra, CatBoost. A provider is a candidate only if its connection exists."""
        from gw.harness.registry import Registry

        out: list[Any] = []
        reg = Registry(self.store)
        for conn in reg.list("ModelProvider"):
            if conn.adapter not in ("remote_tabular", "tabicl_v2", "tabpfn"):
                continue
            prov = reg.get(conn.id).adapter
            if prov.is_external():  # feature rows stay on our own machines (hard limit 3)
                continue
            if prov.health().status == "ok" and not self._retired(prov.model_id):
                out.append(prov)
        if not any(a.model_id.startswith("mitra-v2") for a in out):
            local = MitraAdapter()
            if local.health().status == "ok" and not self._retired("mitra-v2"):
                out.append(local)
        out.append(CatBoostAdapter())
        return out

    def _latest_ece(self, model_id: str) -> float | None:
        rows = self.store.find(m.CalibrationRow, model_id=model_id, order_by="-computed_at", limit=1)
        return rows[0].ece if rows else None

    def _best_calibrated(self, candidates: list[Any]) -> Any:
        scored = [(e, a) for a in candidates if (e := self._latest_ece(a.model_id)) is not None]
        return min(scored, key=lambda x: x[0])[1] if scored else None

    def _clearly_better(self, best: Any, preferred: Any) -> bool:
        """Switch away from the configured model only for a margin of 0.01 ECE (selection noise)."""
        b, p = self._latest_ece(best.model_id), self._latest_ece(preferred.model_id)
        return b is not None and p is not None and p - b > 0.01  # unmeasured is not worse

    def _retired(self, model_id: str) -> bool:
        rows = self.store.find(m.CalibrationRow, model_id=model_id, order_by="-computed_at", limit=1)
        return bool(rows) and rows[0].ece > float(self.pol.get("forecast.retire_if_ece_above", 0.08))

    @property
    def model_id(self) -> str:
        return str(self.adapter.model_id)

    def _xy(self, ctx: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
        return frame(ctx[feat.FEATURES].to_dict("records"), feat.FEATURES), ctx["label"].to_numpy()

    def predict(self, ctx: pd.DataFrame, rows: list[dict[str, Any]]) -> np.ndarray:
        """Predict with the selected model; a runtime failure falls back to CatBoost."""
        X, y = self._xy(ctx)
        q = frame(rows, feat.FEATURES)
        try:
            return np.asarray(self.adapter.tabular_fit_predict(X, y, q, "binary").p)
        except Exception as e:
            if isinstance(self.adapter, CatBoostAdapter):
                raise
            self.degraded = f"{self.adapter.model_id} failed ({type(e).__name__}); using catboost fallback"
            self.adapter = CatBoostAdapter()
            return np.asarray(self.adapter.tabular_fit_predict(X, y, q, "binary").p)

    def delays(self, ctx: pd.DataFrame, rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
        slipped = ctx[ctx["label"] > 0]
        if len(slipped) < 30:
            z = np.zeros(len(rows))
            return z, z
        X = frame(slipped[feat.FEATURES].to_dict("records"), feat.FEATURES)
        y = slipped["delay_days"].to_numpy()
        q = frame(rows, feat.FEATURES)
        p50 = np.asarray(CatBoostAdapter(iterations=200).tabular_fit_predict(X, y, q, "quantile_0.5").p)
        p90 = np.asarray(CatBoostAdapter(iterations=200).tabular_fit_predict(X, y, q, "quantile_0.9").p)
        return np.maximum(p50, 0), np.maximum(p90, p50)

    def drivers(self, ctx: pd.DataFrame, rows: list[dict[str, Any]], base: np.ndarray, top: int = 3) -> list[list[dict[str, Any]]]:
        """Signed contribution per feature: p(row) - p(row with the feature at its context typical value)."""
        typical = {f: (ctx[f].mode().iloc[0] if f in feat.CATEGORICAL else float(ctx[f].median())) for f in feat.FEATURES}
        cand = [f for f in feat.FEATURES if f != "synthetic"]
        variants = []
        for r in rows:
            for f in cand:
                v = dict(r)
                v[f] = typical[f]
                variants.append(v)
        X, y = self._xy(ctx)
        model = getattr(self.adapter, "last_model", None)
        if model is not None and isinstance(self.adapter, CatBoostAdapter):
            pv = model.predict_proba(frame(variants, feat.FEATURES))[:, 1]
        else:
            pv = np.asarray(self.adapter.tabular_fit_predict(X, y, frame(variants, feat.FEATURES), "binary").p)
        out = []
        for i, r in enumerate(rows):
            contrib = []
            for j, f in enumerate(cand):
                delta = float(base[i] - pv[i * len(cand) + j])
                if abs(delta) > 1e-4:
                    contrib.append({"feature": f, "value": r[f], "contribution": round(delta, 3)})
            contrib.sort(key=lambda d: -abs(d["contribution"]))
            out.append(contrib[:top])
        return out

    def whatif(self, ctx: pd.DataFrame, row: dict[str, Any], levers: list[str]) -> tuple[float, float]:
        after = dict(row)
        for lever in levers:
            for k, v in WHATIF[lever]["set"].items():
                after[k] = float(after[k]) + float(v[1:]) if isinstance(v, str) and v.startswith("+") else v
        if isinstance(self.adapter, CatBoostAdapter):
            # what-ifs reuse one fit per model and context instead of refitting on every click
            key = (self.model_id, len(ctx), int(ctx["label"].sum()), _hash(ctx[feat.FEATURES].tail(50).to_dict("records")))
            fitted = _WHATIF_FITS.get(key)
            if fitted is None:
                self.predict(ctx, [row])
                fitted = getattr(self.adapter, "last_model", None)
                if fitted is not None:
                    _WHATIF_FITS.clear()  # one context at a time; the record moves on
                    _WHATIF_FITS[key] = fitted
            if fitted is not None:
                p = fitted.predict_proba(frame([row, after], feat.FEATURES))[:, 1]
                return float(p[0]), float(p[1])
        p = self.predict(ctx, [row, after])
        return float(p[0]), float(p[1])

    def run(self, run_id: str) -> dict[str, Any]:
        """Score every open commitment and pending gate; write forecast rows (advisory)."""
        ctx = context(self.store, self.pol)
        live = live_rows(self.store)
        if ctx.empty or not live:
            return {"scored": 0, "model": self.model_id}
        rows = [lv.row for lv in live]
        p = self.predict(ctx, rows)
        p50, p90 = self.delays(ctx, rows)
        drv = self.drivers(ctx, rows, p)
        now = clock.now()
        ctx_hash = _hash(ctx[feat.FEATURES].to_dict("records"))
        out = []
        p_by_c: dict[str, float] = {}
        for i, lv in enumerate(live):
            p_by_c[lv.commitment.id] = float(p[i])
            out.append(m.ForecastRow(id=new_id("FC"), target="slip", target_id=lv.commitment.id, run_id=run_id,
                                     model_id=self.model_id, context_size=len(ctx),
                                     feature_hash=_hash([ctx_hash, lv.row]), p=round(float(p[i]), 4),
                                     p50_days=round(float(p50[i]), 1), p90_days=round(float(p90[i]), 1),
                                     drivers=drv[i], created_at=now))
        # gate instances: P(pass on date) by simulation over deliverable slip probabilities
        for gi in self.store.find(m.GateInstance, decision="pending"):
            pg = gate_pass_probability(self.store, gi, p_by_c)
            out.append(m.ForecastRow(id=new_id("FC"), target="gate_pass_on_date", target_id=gi.id, run_id=run_id,
                                     model_id=self.model_id, context_size=len(ctx), feature_hash=ctx_hash,
                                     p=round(pg, 4), created_at=now))
        # project mean over commitments due in 30 d weighted by weight
        by_p: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for lv in live:
            c = lv.commitment
            if c.due_at <= now + timedelta(days=30):
                by_p[c.project_id].append((p_by_c[c.id], c.weight or 1.0))
        for pid, xs in by_p.items():
            out.append(m.ForecastRow(id=new_id("FC"), target="project_slip", target_id=pid, run_id=run_id,
                                     model_id=self.model_id, context_size=len(ctx), feature_hash=ctx_hash,
                                     p=round(sum(a * w for a, w in xs) / sum(w for _, w in xs), 4), created_at=now))
        self._replace_latest(out)
        return {"scored": len(live), "gates": sum(1 for r in out if r.target == "gate_pass_on_date"),
                "model": self.model_id, "context_rows": len(ctx), "degraded": self.degraded,
                "above_flag": int(sum(1 for x in p if x > float(self.pol.get("forecast.flag_above", 0.35))))}

    def _replace_latest(self, rows: list[m.ForecastRow]) -> None:
        """Keep history: older rows stay; reads take the latest run per target."""
        self.writer.insert_many(rows)

    def backtest(self, holdout: float = 0.2, all_models: bool = True) -> dict[str, Any]:
        """Time-based split over matured history: Brier, ECE (10 bins) and reliability per model ( M5).

        Every available model is calibrated on the same split and stored, so selection reads recorded
        calibration rather than one model's luck on one slice.
        """
        now = clock.now()
        rows = [fr for fr in self.store.find(m.FeatureRow, target="slip") if matured(fr, now)]
        rows.sort(key=lambda fr: fr.as_of)
        cut = int(len(rows) * (1 - holdout))
        train, test = rows[:cut], rows[cut:]
        ctx = pd.DataFrame([{**fr.features, "label": fr.label} for fr in train])
        if self.pol.get("forecast.include_synthetic_priors", True):
            pp = priors_path(self.store)
            if pp is not None:
                ctx = pd.concat([ctx, pd.read_parquet(pp)], ignore_index=True)
        y = np.asarray([fr.label for fr in test], dtype=float)
        X, yc = self._xy(ctx)
        q = frame([fr.features for fr in test], feat.FEATURES)
        models = self.candidates() if all_models else [self.adapter]
        results: dict[str, Any] = {}
        cals = []
        for adapter in models:
            try:
                p = np.asarray(adapter.tabular_fit_predict(X.copy(), yc, q.copy(), "binary").p)
            except Exception as e:
                results[adapter.model_id] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
                continue
            brier = float(np.mean((p - y) ** 2))
            e_, table = ece(p, y)
            cals.append(m.CalibrationRow(id=new_id("CAL"), model_id=adapter.model_id, computed_at=clock.now(),
                                         n=len(test), brier=round(brier, 4), ece=round(e_, 4), table=table))
            results[adapter.model_id] = {"brier": round(brier, 4), "ece": round(e_, 4), "table": table}
        self.writer.insert_many(cals)
        main: dict[str, Any] = results.get(self.model_id) or next((v for v in results.values() if "ece" in v), {})
        return {"model": self.model_id, "n_train": len(ctx), "n_test": len(test), "brier": main.get("brier"),
                "ece": main.get("ece"), "base_rate": round(float(y.mean()), 3) if len(y) else None,
                "table": main.get("table", []), "models": {k: {kk: vv for kk, vv in v.items() if kk != "table"}
                                                           for k, v in results.items()}}


def gate_pass_probability(store: Store, gi: m.GateInstance, p_by_c: dict[str, float], draws: int = 2000) -> float:
    """P(readiness >= threshold and all mandatory accepted at the review date), simulated."""
    cs = {c.deliverable_id: c for c in store.find(m.Commitment, gate_instance_id=gi.id) if c.kind == "gate_deliverable"}
    rng = random.Random(gi.id)
    passed = 0
    for _ in range(draws):
        num = den = 0.0
        ok = True
        for d in gi.deliverables:
            c = cs.get(d.id)
            if c is not None and c.status == "waived":
                continue
            if c is not None and c.status == "accepted":
                got = 1.0
            elif c is None:
                got = 0.0
            else:
                p_ok = 1.0 - p_by_c.get(c.id, 0.5)
                if c.due_at > gi.planned_review_at:
                    p_ok *= 0.15  # a date already past the review is rarely pulled back in
                got = 1.0 if rng.random() < p_ok else 0.0
            num += d.weight * got
            den += d.weight
            ok = ok and (got >= 1.0 or not d.mandatory)
        if den and num / den >= gi.threshold and ok:
            passed += 1
    return passed / draws


def latest(store: Store, target: str) -> dict[str, m.ForecastRow]:
    """Latest forecast row per target id."""
    out: dict[str, m.ForecastRow] = {}
    for r in store.find(m.ForecastRow, target=target, order_by="created_at"):
        out[r.target_id] = r
    return out
