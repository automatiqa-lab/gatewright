"""Tender agent.

PackageCheck: section completeness of a package specification against the discipline
template in standards. BidComparison: technical compliance per bid, with commercial
columns locked until a technical sign-off by two people with different accepting
roles exists. Autonomy: draft. Recording a sign-off is a person's act, never the agent's.

Document bodies are read through the DocumentSource for this run only and never stored.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from gw import paths
from gw.agents.base import AgentOutput, Snapshot
from gw.policies.store import Policies
from gw.record import models as m
from gw.record.store import Store

SIGNOFF_ROLES = {"sme:engineering", "sme:procurement", "sme:systems", "project_manager"}
COMPLIANCE = {"compliant": 1.0, "partial": 0.5, "noncompliant": 0.0}


def templates() -> dict[str, list[str]]:
    return dict(yaml.safe_load((paths.STANDARDS_DATA / "package_templates.yaml").read_text(encoding="utf-8")) or {})


def sections_in(text: str) -> list[str]:
    """Numbered or upper-case headings: '1. Scope', '## Loads and design basis', 'WARRANTY'."""
    found = []
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        mt = re.match(r"^(?:\d+(?:\.\d+)*[.)]?\s+)?([A-Za-z][A-Za-z /&,-]{2,60})$", s)
        if mt and (re.match(r"^\d", s) or line.lstrip().startswith("#") or s.isupper()):
            found.append(mt.group(1).strip().lower())
    return found


def parse_bid(text: str) -> dict[str, Any]:
    """Bid documents: 'requirement: compliant|partial|non-compliant' lines and a 'price: <amount> <ccy>' line."""
    reqs: dict[str, float] = {}
    price = None
    currency = None
    for line in text.splitlines():
        mt = re.match(r"^\s*([A-Za-z][\w /&,-]{2,60}):\s*(compliant|partial|non-?compliant)\b", line, re.I)
        if mt:
            reqs[mt.group(1).strip().lower()] = COMPLIANCE[mt.group(2).lower().replace("-", "")]
        pm = re.match(r"^\s*price:\s*([\d.,]+)\s*([A-Z]{3})?", line, re.I)
        if pm:
            price = float(pm.group(1).replace(",", ""))
            currency = pm.group(2)
    return {"requirements": reqs, "price": price, "currency": currency}


def signoffs(store: Store, wp: m.WorkPackage) -> list[dict[str, Any]]:
    users = {u.id: u for u in store.all(m.User)}
    out = []
    for d in store.find(m.Decision, project_id=wp.project_id):
        if d.title == f"technical sign-off {wp.code}":
            u = users.get(d.decided_by_user_id)
            out.append({"user": u.name if u else d.decided_by_user_id, "role": d.chosen, "at": d.decided_at.isoformat(),
                        "decision_id": d.id})
    return out


def commercial_unlocked(sign: list[dict[str, Any]]) -> bool:
    return len({s["role"] for s in sign}) >= 2


class Tender:
    name = "tender"
    output_type = "PackageCheck"

    def __init__(self, package_code: str, docs_conn: str = "tender-room") -> None:
        self.package_code = package_code
        self.docs_conn = docs_conn
        self.summary = ""

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        from gw.harness.contracts import ExternalRef
        from gw.harness.registry import Registry

        wp = store.find_one(m.WorkPackage, code=self.package_code)
        if wp is None:
            raise ValueError(f"package {self.package_code} not found")
        reg = Registry(store)
        src = reg.get(self.docs_conn)
        changes, _ = src.list_changes(None)
        max_bytes = int(policies.get("semantic.max_doc_bytes", 200000))
        spec_text, bids = "", {}
        for ch in changes:
            name = ch.name
            if not name.startswith(wp.code):
                continue
            text = src.read_text(ExternalRef(conn_id=self.docs_conn, external_id=ch.ref.external_id), max_bytes)
            if "_package_spec" in name:
                spec_text = text
            elif "_bid_" in name:
                bids[name.split("_bid_", 1)[1].rsplit(".", 1)[0].replace("-", " ")] = (name, text)
        return Snapshot(seq=store.last_seq(), policies=policies, data={
            "wp": wp, "spec": spec_text, "bids": bids, "template": templates(), "signoffs": signoffs(store, wp)})

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        d = snap.data
        wp: m.WorkPackage = d["wp"]
        tmpl = [s.lower() for s in d["template"].get(wp.discipline) or d["template"]["default"]]
        found = sections_in(d["spec"])
        rows = [{"section": s, "present": any(s in f or f in s for f in found)} for s in tmpl]
        missing = [r["section"] for r in rows if not r["present"]]
        unlocked = commercial_unlocked(d["signoffs"])
        bids = []
        for vendor, (name, text) in sorted(d["bids"].items()):
            b = parse_bid(text)
            reqs = b["requirements"]
            score = sum(reqs.values()) / len(reqs) if reqs else 0.0
            bids.append({"vendor": vendor, "document": name, "technical": round(score, 3),
                         "requirements": reqs, "gaps": [k for k, v in reqs.items() if v < 1.0],
                         "price": b["price"] if unlocked else None, "currency": b["currency"] if unlocked else None})
        cite = [wp.id]
        self.summary = f"{wp.code}: {len(missing)} sections missing, {len(bids)} bids, commercial " + \
                       ("unlocked" if unlocked else "locked")
        return [
            AgentOutput(output_type="PackageCheck", title=f"{wp.code} package check",
                        payload={"package": wp.code, "discipline": wp.discipline, "sections": rows, "missing": missing},
                        citations=cite),
            AgentOutput(output_type="BidComparison", title=f"{wp.code} bid comparison",
                        payload={"package": wp.code, "bids": bids, "commercial_unlocked": unlocked,
                                 "signoffs": d["signoffs"]}, citations=cite),
        ]
