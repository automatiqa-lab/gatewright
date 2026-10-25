"""S9 - one HSE reviewer across DC-01, DC-04, DC-09. Exercises shared_person dependencies and reviewer silence."""

from __future__ import annotations

from gw.demo.cases._util import action, drop_chases, script, silent_chases
from gw.demo.generator import Gen


def prepare(g: Gen) -> None:
    script(g, "DC-04", "fire_strategy", status="", force_status="open", due=6, owner="r.bakker", party="hse")


def apply(g: Gen) -> None:
    hse = g.party("hse")
    a = action(g, "DC-09", "HSE review of slab pour method statement", "r.bakker", "hse", 5, created_days_ago=20,
               deliverable="hse_review")
    a.tags = ["safety"]
    silent_chases(g, a, hse, [6, 3])
    c = g.commitment("DC-04", "fire_strategy")
    drop_chases(g, c)
    silent_chases(g, c, hse, [5, 2])
