"""S5 - grid connection late (DC-01 history). Exercises a utility party and a realised risk with a contingency draw."""

from __future__ import annotations

from datetime import timedelta

from gw.demo.generator import Gen, at
from gw.record import models as m
from gw.record.ids import new_id


def prepare(g: Gen) -> None:
    return None


def apply(g: Gen) -> None:
    p = g.projects["DC-01"]
    lesson = next(x.id for x in g.lessons if x.code == "L-022")
    g.risks.append(m.Risk(id=new_id("Risk"), project_id=p.id, title="Grid connection capacity late (2.4 MVA)",
                          cause="grid operator queue", effect="temporary power needed for site works",
                          owner_user_id=g.user("j.smits").id, status=m.RiskStatus.realised,
                          contingency_reserved=180_000, contingency_drawn=120_000, linked_lesson_ids=[lesson]))
    g.decisions.append(m.Decision(
        id=new_id("Decision"), project_id=p.id, title="Hire a genset until the grid connection is live",
        decided_by_user_id=g.user("m.devries").id, decided_at=at(g.today - timedelta(days=60)),
        options_considered=["wait for the grid", "hire genset"], chosen="hire genset",
        rationale="site works cannot wait eight weeks", cost_of_delay_note="EUR 120k against 8 weeks"))
    g.cost_rows.append({"project_code": "DC-01", "wbs": "DC-01-WP06", "kind": "contingency_draw", "amount": 120000,
                        "currency": "EUR", "posted_at": (g.today - timedelta(days=55)).isoformat(),
                        "ref": "CD-GENSET", "note": "genset hire (S5)"})
