"""S3 - slab flatness fails at SAT (DC-09 history). Exercises the slab -> MHE sequence and lesson L-017."""

from __future__ import annotations

from datetime import timedelta

from gw.demo.generator import Gen, at
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    return None


def apply(g: Gen) -> None:
    wps = {wp.discipline: wp for wp in g.packages["DC-09"]}
    slab, racking = wps["slab"], wps["racking"]
    slab.window_start = g.today + timedelta(days=20)
    slab.window_end = g.today + timedelta(days=50)
    racking.window_start = g.today + timedelta(days=40)  # racking starts before the slab is tested
    racking.window_end = g.today + timedelta(days=75)
    dc01_slab = next(wp for wp in g.packages["DC-01"] if wp.discipline == "slab")
    dc01_slab.window_start = g.today + timedelta(days=28)  # one SlabTec crew, two VNA slabs in the same weeks
    dc01_slab.window_end = g.today + timedelta(days=45)
    g.decisions.append(m.Decision(
        id=new_id("Decision"), project_id=g.projects["DC-09"].id, title="Slab flatness becomes a gate item",
        decided_by_user_id=g.user("k.peeters").id, decided_at=at(g.today - timedelta(days=120)),
        options_considered=["test at SAT only", "accept spec at FEL 2 and test before racking"],
        chosen="accept spec at FEL 2 and test before racking",
        rationale="flatness failure at SAT on the earlier slab cost six weeks (lesson L-017)"))
