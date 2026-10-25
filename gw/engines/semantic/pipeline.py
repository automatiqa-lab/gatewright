"""Semantic pipeline: inbound item -> classification, link, entities, proposed changes.

: ref resolution, rules, classifier heads,
candidate linking by similarity within the project plus party match, extraction,
confidence = min(head, link), and the auto-apply / review-queue decision.
The engine never writes commitments; it returns ProposedChanges for the service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from gw.engines.semantic import rules
from gw.engines.semantic.classifier import Heads, doc_text, intent_text
from gw.record import models as m


@dataclass
class ProposedChange:
    kind: str  # set_status | link_evidence | retarget_chaser | create_risk | drop
    commitment_id: str | None = None
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Classification:
    kind: str  # message | document
    label: str  # intent or doc type
    head_confidence: float
    link_confidence: float
    commitment_id: str | None
    candidates: list[dict[str, Any]]
    entities: dict[str, Any]
    source: str  # ref | rule | classifier
    changes: list[ProposedChange] = field(default_factory=list)
    ai_fields: list[str] = field(default_factory=list)  # entities a language model extracted (Art. 50)

    @property
    def confidence(self) -> float:
        if self.label in ("UNRELATED", "OOO", "invoice") and self.commitment_id is None:
            return self.head_confidence
        return min(self.head_confidence, self.link_confidence)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "intent" if self.kind == "message" else "doc_type": self.label,
                "confidence": round(self.confidence, 3), "head_confidence": round(self.head_confidence, 3),
                "link_confidence": round(self.link_confidence, 3), "commitment_id": self.commitment_id,
                "candidates": self.candidates, "entities": self.entities, "source": self.source,
                "changes": [c.__dict__ for c in self.changes], **self._disclosure()}

    def _disclosure(self) -> dict[str, Any]:
        from gw.governance.disclosure import envelope

        env = envelope(["extracted_fields"], drafted=len(self.ai_fields), total=len(self.entities),
                       review_state="unreviewed")
        return {"ai_generated": env, "ai_fields": self.ai_fields} if env else {}


class Linker:
    """Candidate commitments by text similarity over titles, restricted by party and open-at-time."""

    def __init__(self, commitments: list[m.Commitment], parties: list[m.Party], projects: list[m.Project]) -> None:
        self.cs = commitments
        self.by_code = {c.code: c for c in commitments}
        self.email_party = {ct.email.lower(): p.id for p in parties for ct in p.contacts}
        self.project_code = {p.id: p.code for p in projects}
        self.code_project = {p.code: p.id for p in projects}
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4), sublinear_tf=True)
        self.mat = self.vec.fit_transform([c.title.lower() for c in commitments]) if commitments else None

    def by_ref(self, code: str | None) -> m.Commitment | None:
        return self.by_code.get(code or "")

    def by_file(self, project: str, deliverable: str, at: datetime) -> m.Commitment | None:
        pid = self.code_project.get(project)
        hits = [c for c in self.cs if c.project_id == pid and c.deliverable_id == deliverable]
        if not hits:
            return None
        return min(hits, key=lambda c: abs((c.original_due_at - at).total_seconds()))

    def candidates(self, text: str, sender: str | None, at: datetime | None, project: str | None = None,
                   k: int = 3) -> list[tuple[m.Commitment, float]]:
        if self.mat is None:
            return []
        party = self.email_party.get((sender or "").lower())
        pid = self.code_project.get(project or "")
        idx = []
        for i, c in enumerate(self.cs):
            if party and c.party_id != party:
                continue
            if pid and c.project_id != pid:
                continue
            if at is not None:
                if c.created_at > at + timedelta(days=3):
                    continue
                if c.closed_at and c.closed_at < at - timedelta(days=10):
                    continue
            idx.append(i)
        if not idx:
            return []
        q = self.vec.transform([text.lower()])
        sims = (self.mat[idx] @ q.T).toarray().ravel()
        scored = []
        for j, i in enumerate(idx):
            c = self.cs[i]
            s = float(sims[j])
            if at is not None:
                s -= min(abs((c.original_due_at - at).days), 120) / 2000  # date proximity breaks ties
            scored.append((c, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


def link_confidence(scored: list[tuple[m.Commitment, float]]) -> float:
    if not scored:
        return 0.0
    if len(scored) == 1:
        return 0.9 if scored[0][1] > 0.15 else 0.5
    margin = scored[0][1] - scored[1][1]
    return float(np.clip(0.45 + 3.0 * margin + 0.3 * scored[0][1], 0.0, 0.9))


class Pipeline:
    """One pass over one inbound item. Pure: returns a Classification with proposed changes."""

    def __init__(self, heads: Heads | None, linker: Linker, today: date, auto_apply: float = 0.8,
                 llm: Any = None, extract_for: tuple[str, ...] = ("FLAG", "QUESTION", "DELEGATE"),
                 taxonomy: dict[str, Any] | None = None) -> None:
        """`taxonomy` is the current revision; labels outside it fall back to UNRELATED / other."""
        from gw.engines.semantic.taxonomy import current

        tax = taxonomy or current()
        self.heads = heads
        self.linker = linker
        self.today = today
        self.auto_apply = auto_apply
        self.llm = llm
        self.intents = set(tax.get("reply_intents") or [])
        self.doc_types = set(tax.get("document_types") or [])
        self.ignored = set(tax.get("ignored_document_types") or [])
        self.extract_for = tuple(tax.get("extract_for_intents") or extract_for)
        self.extract_docs = set(tax.get("extract_for_doc_types") or [])

    def message(self, subject: str, text: str, sender: str | None, at: datetime | None,
                headers: dict[str, str] | None = None, thread_ref: str | None = None) -> Classification:
        headers = headers or {}
        ref, _ = rules.find_ref(headers.get("x-gw-ref"), thread_ref, subject, text)
        rr = rules.apply_rules(subject, text, self.today)
        if rr.intent:
            label, head_conf, source = rr.intent, rr.confidence, "rule"
        elif self.heads is not None:
            label, head_conf = self.heads.intent.predict([intent_text(subject, text)])[0]
            source = "classifier"
            if self.intents and label not in self.intents:
                label, head_conf = "UNRELATED", min(head_conf, 0.5)
        else:
            label, head_conf, source = "UNRELATED", 0.3, "none"
        ents = dict(rr.entities)
        ai_fields: list[str] = []
        if label in self.extract_for:
            for k, v in rules.extract(text, self.today).items():
                ents.setdefault(k, v)
            if self.llm is not None:
                try:
                    got = self.llm.complete(
                        "Extract new_due_date (YYYY-MM-DD), reason and person from this reply. "
                        "Use only what the text says.\n\n" + text,
                        {"type": "object", "properties": {"new_due_date": {"type": "string"},
                                                          "reason": {"type": "string"}, "person": {"type": "string"}}})
                    for k, v in (got or {}).items():
                        if v and k not in ents:
                            ents[k] = v
                            ai_fields.append(k)
                except Exception:
                    pass  # extraction adds no confidence; failure costs nothing
        cands: list[dict[str, Any]] = []
        commitment: m.Commitment | None = self.linker.by_ref(ref)
        if commitment is not None:
            link_conf = 1.0
            cands = [{"commitment_id": commitment.id, "code": commitment.code, "title": commitment.title, "score": 1.0}]
            source = source + "+ref"
        else:
            scored = self.linker.candidates(f"{subject} {text}", sender, at)
            link_conf = link_confidence(scored)
            cands = [{"commitment_id": c.id, "code": c.code, "title": c.title, "score": round(s, 3)} for c, s in scored]
            commitment = scored[0][0] if scored else None
        if label in ("UNRELATED",):
            commitment, link_conf = None, 1.0
        cl = Classification("message", label, head_conf, link_conf, commitment.id if commitment else None, cands, ents, source,
                            ai_fields=ai_fields)
        cl.changes = self._changes(cl, commitment)
        return cl

    def document(self, name: str, text: str, at: datetime | None, sender: str | None = None) -> Classification:
        fp = rules.parse_filename(name)
        if self.heads is not None:
            label, head_conf = self.heads.doc_type.predict([doc_text(name, text)])[0]
            source = "classifier"
            if self.doc_types and label not in self.doc_types:
                label, head_conf = "other", min(head_conf, 0.5)
        else:
            label, head_conf, source = "other", 0.3, "none"
        ents = rules.extract(text, self.today) if label in self.extract_docs else {}
        commitment = None
        cands: list[dict[str, Any]] = []
        link_conf = 0.0
        if fp:
            commitment = self.linker.by_file(fp["project"], fp["deliverable"], at or datetime.now().astimezone())
            if commitment is not None:
                link_conf = rules.RULE_CAP
                source += "+filename"
                ents["revision"] = fp["rev"]
        if commitment is None:
            project = None
            for code in self.linker.code_project:
                if code[-2:] in name.replace("_", "-").split("-"):
                    project = code
            scored = self.linker.candidates(f"{name} {text}", sender, at, project)
            link_conf = link_confidence(scored)
            cands = [{"commitment_id": c.id, "code": c.code, "title": c.title, "score": round(s, 3)} for c, s in scored]
            commitment = scored[0][0] if scored else None
        else:
            cands = [{"commitment_id": commitment.id, "code": commitment.code, "title": commitment.title, "score": 0.95}]
        if label in self.ignored:
            commitment, link_conf = None, 1.0
        cl = Classification("document", label, head_conf, link_conf, commitment.id if commitment else None, cands, ents, source)
        if commitment is not None and label not in self.ignored | {"other"}:
            cl.changes = [ProposedChange("link_evidence", commitment.id, {"name": name, "doc_type": label})]
        elif label == "change_order" and commitment is not None:
            cl.changes.append(ProposedChange("create_risk", commitment.id, {"text": f"change order {name}"}))
        else:
            cl.changes = [ProposedChange("drop")]
        return cl

    def _changes(self, cl: Classification, c: m.Commitment | None) -> list[ProposedChange]:
        if cl.label in ("UNRELATED", "OOO") or c is None:
            return [ProposedChange("drop", c.id if c else None, {"label": cl.label})]
        if cl.label == "CONFIRM":
            return [ProposedChange("set_status", c.id, {"status": "confirmed"})]
        if cl.label == "FLAG":
            nd = rules.valid_new_due(cl.entities.get("new_due_date"), self.today)
            if nd is None:
                cl.link_confidence = min(cl.link_confidence, 0.5)  # no valid date -> a person decides
                return [ProposedChange("set_status", c.id, {"status": "flagged", "new_due": None,
                                                            "reason": cl.entities.get("reason")})]
            return [ProposedChange("set_status", c.id, {"status": "flagged", "new_due": nd.isoformat(),
                                                        "reason": cl.entities.get("reason")})]
        if cl.label == "DELEGATE":
            return [ProposedChange("retarget_chaser", c.id, {"delegate": cl.entities.get("person")})]
        if cl.label == "DECLINE":
            return [ProposedChange("create_risk", c.id, {"text": f"{c.code} declined by the party"})]
        return [ProposedChange("drop", c.id, {"label": cl.label, "note": "question for the owner"})]
