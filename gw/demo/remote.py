"""Seed a masked live M365 account with synthetic documents and replies, then sync them back.

Exercises the real Graph adapters end to end ( test strategy: contract tests against
recorded or live fixtures). Everything sent is generated here from the demo record; nothing is
read from or sent to a real party. Needs scope `seed_demo` on both connections.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from gw.demo import texts
from gw.record import clock
from gw.record import models as m
from gw.surfaces.service import Service


@dataclass
class SeedResult:
    documents: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    synced: dict[str, Any] = field(default_factory=dict)
    processed: dict[str, Any] = field(default_factory=dict)


def seed(svc: Service, mail_conn: str = "mail-live", docs_conn: str = "docs-live", n_docs: int = 6,
         n_msgs: int = 10, seed_value: int = 7, sync: bool = True) -> SeedResult:
    """Upload synthetic documents and deliver synthetic replies for open gate commitments."""
    rng = random.Random(f"remote:{seed_value}:{clock.today()}")
    st = svc.store
    projects = {p.id: p.code for p in st.all(m.Project)}
    parties = {p.id: p for p in st.all(m.Party)}
    cands = [c for c in st.all(m.Commitment) if c.status in ("open", "confirmed", "overdue") and c.gate_instance_id]
    rng.shuffle(cands)
    res = SeedResult()
    docs = svc.registry.get(docs_conn)
    for c in cands[:n_docs]:
        code = projects[c.project_id]
        dt = texts.doc_type_for(c.deliverable_id, "document")
        name = texts.doc_name(rng, code, c.deliverable_id, dt, True).replace(".pdf", ".txt")
        body = (f"SYNTHETIC DEMO DOCUMENT - Gatewright\n{code} {c.title}\n\n"
                + texts.doc_text(rng, dt, c.title, c.due_at.date())).encode("utf-8")
        docs.upload(name, body)
        res.documents.append(name)
    mail = svc.registry.get(mail_conn)
    for c in cands[n_docs:n_docs + n_msgs]:
        party = parties[c.party_id]
        intent = rng.choices(["CONFIRM", "FLAG", "QUESTION", "DELEGATE"], [55, 25, 10, 10])[0]
        nd = c.due_at.date().fromordinal(c.due_at.date().toordinal() + rng.randint(7, 21))
        text = texts.reply_text(rng, intent, "en", c.title, c.due_at.date(), nd if intent == "FLAG" else None,
                                texts.reason(rng, "en"), "Ana Ruiz", rng.random() < 0.5)
        ref = f"{c.code}/{max(1, c.chase_count)}"
        subject = f"RE: [{ref}] {c.title}"
        mail.deliver_to_self(subject, text + f"\n\n(synthetic demo reply on behalf of {party.name})",
                             {"x-gw-ref": ref, "x-gw-party": party.contacts[0].email if party.contacts else ""})
        res.messages.append(subject)
    if sync:
        user = svc.user(None)
        res.synced = {k: v for conn in (docs_conn, mail_conn) for k, v in svc.harness_sync(user, conn).items()}
        res.processed = svc.process_inbound(trigger="manual")
    return res
