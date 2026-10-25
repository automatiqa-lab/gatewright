"""S7 - dock count changed after scope freeze (DC-03). Exercises the change_order document type and estimate drift."""

from __future__ import annotations

from datetime import timedelta

from gw.demo.cases._util import action
from gw.demo.generator import Gen, at
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    g.estimate_drift["DC-03"] = 0.08


def apply(g: Gen) -> None:
    a = action(g, "DC-03", "Re-issue estimate after dock count change (48 -> 56 doors)", "l.jansen", "finance", 9,
               created_days_ago=10, deliverable="estimate_reissue")
    a.tags = ["cost"]
    g.inbound.append(m.InboundItem(
        id=new_id("InboundItem"), conn_id="docs-cp", external_id="case-co-dc03", kind="document",
        received_at=at(g.today - timedelta(days=10), 10),
        payload={"name": "CO-03-014 dock count change.pdf", "ref": {"conn_id": "docs-cp", "external_id": "CO-03-014"}},
        processed_at=at(g.today - timedelta(days=10), 11), commitment_id=a.id,
        classification={"doc_type": "change_order", "confidence": 0.93, "source": "case",
                        "entities": {"amount": "1,450,000", "currency": "EUR"}}))
