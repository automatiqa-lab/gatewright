"""S4 - ASRS vendor lead time +4-8 weeks (DC-02). Exercises FLAG parsing, float erosion and delay forecasts."""

from __future__ import annotations

from datetime import timedelta

from gw.demo.cases._util import action, drop_chases, reply, script
from gw.demo.generator import Gen, at
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    script(g, "DC-02", "sat_reports", status="", force_status="open", due=16, party="shuttleworks")
    g.float_change["DC-02"] = -12.0


def apply(g: Gen) -> None:
    sw = g.party("shuttleworks")
    c = g.commitment("DC-02", "sat_reports")
    drop_chases(g, c)
    new_due = g.today + timedelta(days=51)
    reply(g, c, sw, 1, "FLAG", f"FLAG - {new_due.isoformat()} shuttle carriers delayed at our supplier, lead time +5 weeks",
          entities={"new_due_date": new_due.isoformat(), "reason": "shuttle carriers delayed"})
    c.status = m.CommitmentStatus.flagged
    c.due_at = at(new_due, 17)
    items = (("Shuttle carrier delivery confirmation", 6), ("FAT report shuttle lift", 9),
             ("Revised installation sequence", 12), ("Spare parts list", 20), ("Rail system delivery", 8),
             ("Lift motors delivery", 10), ("Controls cabinet FAT", 14), ("Carrier charging units", 18))
    for i, (title, days) in enumerate(items):
        a = action(g, "DC-02", title, "c.marchand", "shuttleworks", days, created_days_ago=40, deliverable="asrs_supply")
        a.tags = ["vendors"]
        moved = g.today + timedelta(days=days + 28)
        reply(g, a, sw, 2 + i * 3, "FLAG", f"FLAG - {moved.isoformat()} supplier capacity",
              entities={"new_due_date": moved.isoformat(), "reason": "supplier capacity"})
        a.status = m.CommitmentStatus.flagged
        a.due_at = at(moved, 17)
    g.risks.append(m.Risk(id=new_id("Risk"), project_id=g.projects["DC-02"].id, title="ASRS lead time moves start-up",
                          cause="shuttle carriers delayed at the vendor's supplier", effect="SAT and start-up +5 weeks",
                          owner_user_id=g.user("c.marchand").id, status=m.RiskStatus.mitigating,
                          linked_commitment_ids=[c.id]))
