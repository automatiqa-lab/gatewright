"""Helpers shared by the case scripts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gw.demo.generator import Gen, at
from gw.record import models as m
from gw.record.ids import new_id


def script(g: Gen, project: str, deliverable: str, **kw: Any) -> None:
    g.scripted.setdefault(project, {}).setdefault(deliverable, {}).update(kw)


def reply(g: Gen, c: m.Commitment, party: m.Party, days_ago: int, intent: str, text: str, step: int = 1,
          entities: dict[str, Any] | None = None) -> m.Chase:
    """A sent chase plus its reply, both in the past, already processed."""
    sent = at(g.today - timedelta(days=days_ago + 1), 8)
    got = at(g.today - timedelta(days=days_ago), 11)
    ch = m.Chase(id=new_id("Chase"), commitment_id=c.id, project_id=c.project_id, party_id=party.id, level=min(step, 2),
                 step=step, status="sent", planned_for=sent, sent_at=sent, replied_at=got, reply_intent=intent,
                 gw_ref=f"{c.code}/{step}", to=[party.contacts[0].email], subject=f"[{c.code}/{step}] {c.title}")
    g.chases.append(ch)
    g.inbound.append(m.InboundItem(
        id=new_id("InboundItem"), conn_id="mail-shared", external_id=f"case-{c.code}-{step}-{days_ago}", kind="message",
        received_at=got, payload={"from": party.contacts[0].email, "subject": f"RE: {ch.subject}", "text": text,
                                  "gw_ref": ch.gw_ref, "thread_id": f"t-{c.code}"},
        processed_at=got, commitment_id=c.id,
        classification={"intent": intent, "confidence": 1.0, "source": "case", "entities": entities or {}}))
    c.chase_count = max(c.chase_count, step)
    c.last_chase_at = max(c.last_chase_at or sent, sent)
    c.last_reply_at = got
    c.first_unanswered_chase_at = None
    return ch


def silent_chases(g: Gen, c: m.Commitment, party: m.Party, days_ago: list[int], max_level: int = 2) -> None:
    """Chases with no reply; the first one starts the silence clock."""
    for i, d in enumerate(sorted(days_ago, reverse=True), start=1):
        sent = at(g.today - timedelta(days=d), 8)
        g.chases.append(m.Chase(id=new_id("Chase"), commitment_id=c.id, project_id=c.project_id, party_id=party.id,
                                level=min(i, max_level), step=i, status="sent", planned_for=sent, sent_at=sent,
                                gw_ref=f"{c.code}/{i}", to=[party.contacts[0].email], subject=f"[{c.code}/{i}] {c.title}"))
        if i == 1:
            c.first_unanswered_chase_at = sent
        c.last_chase_at = sent
    c.chase_count = len(days_ago)
    c.escalation_level = min(len(days_ago), max_level)
    c.last_reply_at = None
    g.silent_overrides.add(c.id)


def drop_chases(g: Gen, c: m.Commitment) -> None:
    g.chases = [ch for ch in g.chases if ch.commitment_id != c.id]
    g.inbound = [i for i in g.inbound if i.commitment_id != c.id]


def action(g: Gen, project: str, title: str, owner: str, party_key: str, due_days: int, created_days_ago: int = 30,
           deliverable: str = "action") -> m.Commitment:
    proj = g.projects[project]
    return g.new_commitment(proj, title, deliverable, m.CommitmentKind.action, g.user(owner), g.party(party_key),
                            g.today + timedelta(days=due_days), g.today - timedelta(days=created_days_ago))
