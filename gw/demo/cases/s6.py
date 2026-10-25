"""S6 - WMS integration late (DC-02). Exercises systems exposure and go-live risk."""

from __future__ import annotations

from gw.demo.cases._util import action, silent_chases
from gw.demo.generator import Gen
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    return None


def apply(g: Gen) -> None:
    nf = g.party("nordflow")
    for title, due, chased in (("WCS interface test with shuttle controller", -2, [12, 8, 4]),
                               ("I/O list update for shuttle lifts", 3, [9, 5]),
                               ("WMS go-live cut-over plan", 8, [6, 2]),
                               ("WCS emulation run with throughput report", 1, [11, 7, 3]),
                               ("Interface control document sign-off", 4, [10, 6]),
                               ("Label printer and scanner integration test", 6, [8, 4, 1]),
                               ("Data migration dry run", 10, [5, 2])):
        a = action(g, "DC-02", title, "c.marchand", "nordflow", due, created_days_ago=45, deliverable="wms_integration")
        a.tags = ["systems"]
        silent_chases(g, a, nf, chased)
        a.status = m.CommitmentStatus.overdue if due < 0 else m.CommitmentStatus.open
    g.risks.append(m.Risk(id=new_id("Risk"), project_id=g.projects["DC-02"].id,
                          title="WMS integration late threatens go-live", cause="integrator capacity",
                          effect="go-live after ASRS SAT", owner_user_id=g.user("e.visser").id))
