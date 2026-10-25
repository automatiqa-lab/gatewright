"""S2 - insurer rejects the ESFR design until the roof PV layout is submitted (DC-01; earlier DC-08).

Exercises evidence rejection, lesson L-041 and the gatekeeper lever.
"""

from __future__ import annotations

from datetime import timedelta

from gw.demo.cases._util import drop_chases, reply
from gw.demo.generator import Gen, at
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    return None


def apply(g: Gen) -> None:
    c = g.commitment("DC-01", "fire_strategy")
    insurer = g.party("insurer")
    drop_chases(g, c)
    ev = m.Evidence(id=new_id("Evidence"), commitment_id=c.id, kind=m.EvidenceKind.document,
                    uri="fake://docs-cp/DC-01_fire_strategy_revA.pdf", title="DC-01_fire_strategy_revA.pdf",
                    revision="A", sha256="5" * 64, size=2_400_000, received_at=at(g.today - timedelta(days=9), 14),
                    source_conn_id="docs-cp",
                    rejected_reason="insurer will not review the ESFR design until the roof PV layout is submitted")
    g.evidence.append(ev)
    c.evidence_id = ev.id
    new_due = g.today + timedelta(days=21)
    reply(g, c, insurer, 6, "FLAG",
          "We cannot sign off the ESFR design before we have seen the roof PV array layout. "
          f"Realistic date for the approval is {new_due.isoformat()}.",
          step=2, entities={"new_due_date": new_due.isoformat(), "reason": "roof PV layout required first"})
    c.status = m.CommitmentStatus.flagged
    c.due_at = at(new_due, 17)
    c.escalation_level = 2
    lesson = next(x.id for x in g.lessons if x.code == "L-041")
    g.risks.append(m.Risk(id=new_id("Risk"), project_id=g.projects["DC-01"].id,
                          title="Insurer approval of ESFR design blocks FID", cause="roof PV layout not submitted",
                          effect="FEL 3 review at risk", owner_user_id=g.user("r.bakker").id,
                          linked_commitment_ids=[c.id], linked_lesson_ids=[lesson]))
