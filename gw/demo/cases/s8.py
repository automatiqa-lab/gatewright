"""S8 - steel price shock (DC-08, DC-05). Exercises the contingency burn signal."""

from __future__ import annotations

from gw.demo.generator import Gen
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    g.contingency_share["DC-08"] = 0.44
    g.contingency_share["DC-05"] = 0.75
    g.estimate_drift["DC-08"] = 0.06
    g.estimate_drift["DC-05"] = 0.04


def apply(g: Gen) -> None:
    for code in ("DC-08", "DC-05"):
        g.risks.append(m.Risk(id=new_id("Risk"), project_id=g.projects[code].id, title="Steel price shock",
                              cause="steel index +18% in 3 months", effect="contingency drawn ahead of time",
                              owner_user_id=g.projects[code].owner_user_id, status=m.RiskStatus.realised,
                              contingency_drawn=g.projects[code].contingency_amount * 0.2))
