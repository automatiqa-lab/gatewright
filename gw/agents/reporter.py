"""Reporter agent.: lagging SteerCo report (markdown) and a JSON payload."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gw.agents.base import AgentOutput, Snapshot
from gw.governance.core import readiness_score
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store


class Reporter:
    name = "reporter"
    output_type = "Report"

    def __init__(self, period: str = "weekly") -> None:
        self.period = period
        self.summary = ""

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        days = 7 if self.period == "weekly" else 30
        now = clock.now()
        since = now - timedelta(days=days)
        cost: dict[str, dict[str, float]] = {}
        for r in store.all(m.CostRow):
            c = cost.setdefault(r.project_code, {"actual": 0.0, "estimate": 0.0, "contingency_draw": 0.0})
            if r.kind == "actual":
                c["actual"] += r.amount
            elif r.kind == "estimate":
                c["estimate"] = r.amount
            elif r.kind == "contingency_draw":
                c["contingency_draw"] += r.amount
        chases = [c for c in store.all(m.Chase) if c.status == "sent" and c.sent_at and c.sent_at >= since]
        gates = [g for g in store.all(m.GateInstance) if g.decided_at and g.decided_at >= since]
        return Snapshot(seq=store.last_seq(), policies=policies, data={
            "now": now, "since": since, "projects": store.find(m.Project, order_by="code"), "cost": cost,
            "chases": chases, "gates": gates, "readiness": readiness_score(store, policies)})

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        d = snap.data
        rows = []
        for p in d["projects"]:
            c = d["cost"].get(p.code, {})
            slip = (p.forecast_finish - p.baseline_finish).days if p.forecast_finish and p.baseline_finish else None
            rows.append({"project": p.code, "name": p.name, "capex": p.capex_amount, "currency": p.capex_currency,
                         "actual_to_date": round(c.get("actual", 0.0), 0), "estimate": c.get("estimate"),
                         "contingency_drawn": round(p.contingency_drawn, 0), "finish_vs_baseline_days": slip})
        replied = sum(1 for c in d["chases"] if c.replied_at)
        payload = {"period": self.period, "from": d["since"].date().isoformat(), "to": d["now"].date().isoformat(),
                   "projects": rows, "gates_decided": [{"gate_instance_id": g.id, "gate": g.gate_id,
                                                        "decision": g.decision} for g in d["gates"]],
                   "chases_sent": len(d["chases"]), "chases_replied": replied,
                   "response_rate": round(replied / len(d["chases"]), 3) if d["chases"] else None,
                   "automation_readiness": d["readiness"]}
        md = [f"# {self.period.title()} report {payload['from']} - {payload['to']}", "",
              "| Project | Capex | Actual to date | Estimate | Contingency drawn | Finish vs baseline |",
              "|---|---|---|---|---|---|"]
        for r in rows:
            slip = r["finish_vs_baseline_days"]
            slip_txt = "" if slip is None else f"{slip:+d} d"
            md.append(f"| {r['project']} | {r['currency']} {r['capex'] / 1e6:.1f}m | {r['actual_to_date'] / 1e6:.1f}m | "
                      f"{(r['estimate'] or 0) / 1e6:.1f}m | {r['contingency_drawn'] / 1e3:.0f}k | {slip_txt} |")
        md += ["", f"Chases sent: {payload['chases_sent']}, replied: {replied}. Gates decided: {len(d['gates'])}."]
        payload["markdown"] = "\n".join(md)
        self.summary = f"{self.period} report, {len(rows)} projects"
        return [AgentOutput(output_type="Report", title=f"{self.period} report", payload=payload,
                            citations=[p.id for p in d["projects"]])]
