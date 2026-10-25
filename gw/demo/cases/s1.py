"""S1 - permit office silent (DC-04 environmental agency, DC-05 municipality).

Exercises the chaser ladder, the L3 approval, permits exposure and the forecast.
"""

from __future__ import annotations

from gw.demo.cases._util import drop_chases, script, silent_chases
from gw.demo.generator import Gen
from gw.record import models as m
from gw.record.ids import new_id

SILENT = (("DC-04", "environmental_permit", [14, 10, 8]),
          ("DC-04", "building_permit", [7, 4]),
          ("DC-05", "occupancy_permit", [9, 6]))


def prepare(g: Gen) -> None:
    script(g, "DC-04", "environmental_permit", status="", force_status="overdue", due=-1, party="env-ES", silent=True)
    script(g, "DC-04", "building_permit", status="", force_status="open", due=4, party="muni-ES", silent=True)
    script(g, "DC-05", "occupancy_permit", status="", force_status="open", due=6, party="muni-PE", silent=True)
    g.float_change["DC-04"] = -7.0
    g.float_change["DC-05"] = -5.0


def apply(g: Gen) -> None:
    parties = {p.id: p for p in g.parties.values()}
    for project, deliv, days in SILENT:
        c = g.commitment(project, deliv)
        drop_chases(g, c)
        silent_chases(g, c, parties[c.party_id], days)
    for project, deliv, title in (("DC-04", "environmental_permit", "Environmental permit not issued before FEL 3 review"),
                                  ("DC-05", "occupancy_permit", "Occupancy permit delays civil works completion")):
        g.risks.append(m.Risk(id=new_id("Risk"), project_id=g.projects[project].id, title=title,
                              cause="permit office silent", effect="gate review moves",
                              trigger="no reply in 5 business days", owner_user_id=g.projects[project].owner_user_id,
                              linked_commitment_ids=[g.commitment(project, deliv).id]))
