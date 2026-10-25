"""Gatekeeper agent.

Scores every pending gate instance, names the gaps with a lever each, cites
lessons, and inside `lead_time_days` drafts a review pack. Nothing changes status;
a person stamps.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gw.agents.base import AgentOutput, Snapshot
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store
from gw.signals.readiness import score_instance

# Fixed lever catalogue. Each maps to a forecast what-if lever id.
LEVERS = {
    "escalate_owner": {"label": "Escalate to the owner today", "whatif": "escalate_today"},
    "split_deliverable": {"label": "Split the deliverable and submit the ready part", "whatif": "evidence_submitted"},
    "ask_partial": {"label": "Ask the party for a partial submission", "whatif": "evidence_submitted"},
    "start_from_template": {"label": "Start from a previous project's template", "whatif": "evidence_submitted"},
    "move_review": {"label": "Move the review", "whatif": "move_review"},
}


def lever_for(gap: dict[str, Any], rules: dict[str, Any] | None = None) -> str:
    """The recommended lever for one gap; the thresholds and kinds come from policies.gates.levers."""
    from gw.policies.store import shipped_defaults

    r = rules or (shipped_defaults().get("gates") or {}).get("levers") or {}
    open_days = int(r.get("template_when_open_days_over", 3))
    if gap["status"] == "missing" or (gap["status"] == "open" and gap.get("days_to_due", 0) > open_days
                                      and set(r.get("template_tags", ["ops"])) & set(gap["tags"])):
        return "start_from_template"
    if gap["reason"].startswith("evidence rejected") or gap["status"] == "flagged":
        return "split_deliverable"
    if gap.get("party_kind") in set(r.get("partial_party_kinds", [])):
        return "ask_partial"
    if gap["status"] in ("overdue",) or gap.get("days_to_due", 0) < 0:
        return "escalate_owner"
    return "escalate_owner"


def lessons_for(gaps: list[dict[str, Any]], lessons: list[m.Lesson]) -> list[m.Lesson]:
    words: set[str] = set()
    for g in gaps:
        words |= set(g["tags"]) | set(g["deliverable_id"].split("_"))
        if g.get("party_kind"):
            words.add(str(g["party_kind"]))
        words |= set(g["reason"].lower().replace(",", " ").split())
    scored = [(len(set(lesson.tags) & words), lesson) for lesson in lessons]
    return [lesson for s, lesson in sorted(scored, key=lambda x: -x[0]) if s >= 1][:3]


def says(code: str, gate: str, score: Any, gaps: list[dict[str, Any]], lessons: list[m.Lesson],
         review_date: str) -> str:
    """The two-sentence 'Gatekeeper says' from the structured assessment only (ids cited)."""
    mand = [g for g in gaps if g["mandatory"]]
    lead = mand[0] if mand else (gaps[0] if gaps else None)
    if lead is None:
        return f"{code}/{gate}: all deliverables accepted; readiness {score.readiness:.0%}. Recommend {score.recommendation}."
    count = len(mand) or len(gaps)
    kind = "mandatory item" if mand else "item"
    lesson = f" Same pattern as {lessons[0].code} ({lessons[0].title.lower()})." if lessons else ""
    return (f"{count} {kind}{'s' if count != 1 else ''} block {gate} on {review_date}; the largest is "
            f"{lead['name'].lower()} ({lead['commitment']}, {lead['reason']}).{lesson} "
            f"Recommend {LEVERS[lever_for(lead)]['label'].lower()} for {lead['commitment']}; "
            f"readiness {score.readiness:.0%} of {score.threshold:.0%}.")


def review_pack(project: m.Project, gi: m.GateInstance, score: Any, gaps: list[dict[str, Any]],
                decisions: list[m.Decision], risks: list[m.Risk], lessons: list[m.Lesson],
                p_pass: float | None) -> str:
    lines = [f"# Review pack - {project.code} {gi.name}", "",
             f"Review {gi.planned_review_at:%d %b %Y} | readiness {score.readiness:.0%} of {score.threshold:.0%} | "
             f"recommendation **{score.recommendation.replace('_', ' ')}**"
             + (f" | P(pass on date) {p_pass:.2f} (advisory)" if p_pass is not None else ""), "",
             "## Gaps", ""]
    for g in gaps:
        lines.append(f"- {g['commitment']} {g['name']} - {g['status']}, {g['reason']}"
                     f"{' (mandatory)' if g['mandatory'] else ''}, weight {g['weight']:g}")
    lines += ["", "## Decisions since the last gate", ""]
    lines += [f"- {d.decided_at:%d %b} {d.title}: {d.chosen}" for d in decisions] or ["- none"]
    lines += ["", "## Open risks", ""]
    lines += [f"- {r.title} ({r.status})" for r in risks if r.status in ("open", "mitigating", "realised")] or ["- none"]
    lines += ["", "## Lessons cited", ""]
    lines += [f"- {lesson.code} {lesson.title}" for lesson in lessons] or ["- none"]
    lines += ["", "Draft by the Gatekeeper agent. A project manager issues it; the gate is stamped by the decision body."]
    return "\n".join(lines)


class Gatekeeper:
    name = "gatekeeper"
    output_type = "GateAssessment"

    def __init__(self, project_code: str | None = None, p_slip: dict[str, float] | None = None,
                 p_pass: dict[str, float] | None = None) -> None:
        self.project_code = project_code
        self.p_slip = p_slip
        self.p_pass = p_pass or {}
        self.summary = ""

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        from gw.engines.drafting import Drafter

        self.drafter = Drafter(store, policies, "gatekeeper")
        projects = store.find(m.Project, code=self.project_code) if self.project_code else store.all(m.Project)
        submitted = float(policies.get("gates.submitted_counts_as", 0.5))
        p_slip = self.p_slip
        if p_slip is None:
            p_slip = {f.target_id: f.p for f in store.find(m.ForecastRow, target="slip", order_by="created_at")}
        items = []
        lessons = store.all(m.Lesson)
        for p in projects:
            if not p.current_gate_instance_id:
                continue
            gi = store.get(m.GateInstance, p.current_gate_instance_id)
            score = score_instance(store, gi, submitted, p_slip)
            last_gate = max((g.decided_at for g in store.find(m.GateInstance, project_id=p.id) if g.decided_at),
                            default=None)
            decisions = [d for d in store.find(m.Decision, project_id=p.id) if last_gate is None or d.decided_at >= last_gate]
            items.append({"project": p, "gi": gi, "score": score, "decisions": decisions,
                          "risks": store.find(m.Risk, project_id=p.id), "lessons": lessons})
        return Snapshot(seq=store.last_seq(), policies=policies, data={"items": items, "now": clock.now()})

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        outs: list[AgentOutput] = []
        now = snap.data["now"]
        for it in snap.data["items"]:
            p, gi, score = it["project"], it["gi"], it["score"]
            gaps = score.gaps
            for g in gaps:
                g["lever"] = lever_for(g, snap.policies.get("gates.levers"))
                g["lever_label"] = LEVERS[g["lever"]]["label"]
            lessons = lessons_for(gaps, it["lessons"])
            text = says(p.code, gi.name, score, gaps, lessons, f"{gi.planned_review_at:%d %b}")
            generated: list[str] = []
            drafting: dict[str, Any] = {}
            drafter = getattr(self, "drafter", None)
            if drafter is not None:
                draft = drafter.rewrite(text, purpose="gate readiness note for the project manager",
                                        facts={"project": p.code, "gate": gi.name, "readiness": f"{score.readiness:.0%}",
                                               "threshold": f"{score.threshold:.0%}"},
                                        keep=[p.code] if p.code in text else [])
                text, drafting = draft.text, draft.audit()
                generated = ["says"] if draft.generated else []
            cites = [gi.id] + [g["commitment_id"] for g in gaps if g["commitment_id"]] + [lesson.id for lesson in lessons]
            outs.append(AgentOutput(
                output_type="GateAssessment", title=f"{p.code}/{gi.gate_id} assessment",
                payload={"project": p.code, "gate": gi.gate_id, "gate_instance_id": gi.id, **score.as_dict(),
                         "says": text, "lessons": [{"code": x.code, "title": x.title, "id": x.id} for x in lessons],
                         "p_pass": self.p_pass.get(gi.id), **({"drafting": drafting} if drafting else {})},
                citations=cites, generated=generated))
            if (gi.planned_review_at - timedelta(days=gi.lead_time_days)).date() <= now.date() <= gi.planned_review_at.date():
                md = review_pack(p, gi, score, gaps, it["decisions"], it["risks"], lessons, self.p_pass.get(gi.id))
                outs.append(AgentOutput(output_type="ReviewPackDraft", title=f"{p.code}/{gi.gate_id} review pack",
                                        payload={"project": p.code, "gate": gi.gate_id, "gate_instance_id": gi.id,
                                                 "markdown": md}, citations=cites, approval_kind="gate_pack"))
        self.tokens_used = getattr(getattr(self, "drafter", None), "tokens", 0)
        self.summary = f"{sum(o.output_type == 'GateAssessment' for o in outs)} gates scored, " \
                       f"{sum(o.output_type == 'ReviewPackDraft' for o in outs)} packs drafted"
        return outs
