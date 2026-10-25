"""Retrieval: own index of the record plus federation.

Chunk = one entity (commitment, decision, risk, lesson, agent output, gate model).
The index is built from the record on demand (cheap at portfolio scale) with the
local representation; RetrievalSource connections are searched in parallel and
merged by normalised score with source tags. Bodies from federated sources are
never ingested: hits carry snippets and links only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer

from gw.record import models as m
from gw.record.store import Store


@dataclass
class Doc:
    id: str
    kind: str
    title: str
    text: str
    url: str


def documents(store: Store) -> list[Doc]:
    projects = {p.id: p.code for p in store.all(m.Project)}
    docs: list[Doc] = []
    for c in store.all(m.Commitment):
        docs.append(Doc(c.id, "commitment", f"{c.code} {c.title}", f"{projects.get(c.project_id, '')} {c.title} {c.deliverable_id} {' '.join(c.tags)}",
                        f"/commitments/{c.code}"))
    for d in store.all(m.Decision):
        docs.append(Doc(d.id, "decision", d.title, f"{projects.get(d.project_id, '')} {d.title} {d.chosen} {d.rationale}", f"/projects/{projects.get(d.project_id)}"))
    for r in store.all(m.Risk):
        docs.append(Doc(r.id, "risk", r.title, f"{projects.get(r.project_id, '')} {r.title} {r.cause} {r.effect}", f"/projects/{projects.get(r.project_id)}"))
    for lesson in store.all(m.Lesson):
        docs.append(Doc(lesson.id, "lesson", f"{lesson.code} {lesson.title}", f"{lesson.title} {lesson.text} {' '.join(lesson.tags)}", "/standards/lessons"))
    for o in store.find(m.AgentOutput, output_type=["GateAssessment", "ReviewPackDraft"]):
        text = o.payload.get("says") or o.payload.get("markdown") or ""
        docs.append(Doc(o.id, "agent_output", o.payload.get("title", o.output_type), text, "/agents"))
    for gmr in store.all(m.GateModelRevision):
        docs.append(Doc(f"{gmr.model_id}@{gmr.rev}", "gate_model", gmr.model_id, gmr.yaml_text,
                        f"/standards/gatemodels?model={gmr.model_id}"))
    return docs


def _embedder(store: Store) -> tuple[str, Any]:
    """The configured local embedding provider, else the offline hashing default."""
    from gw.harness.registry import Registry

    reg = Registry(store)
    for conn in reg.list("ModelProvider"):
        prov = reg.get(conn.id).adapter
        if getattr(prov, "kind", "") == "embedding" and not prov.is_external():
            return f"{conn.adapter}:{conn.id}", prov.embed
    from gw.harness.adapters.models import LocalModels

    return "local_models:hashing-1024", LocalModels(dim=1024).embed


def refresh_index(store: Store) -> dict[str, int]:
    """Bring the persisted index up to date: embed new or changed entities, drop deleted ones."""
    import hashlib

    from gw.record import clock

    model, embed = _embedder(store)
    docs = {d.id: d for d in documents(store)}
    have = {r.id: r for r in store.all(m.SearchDoc)}
    stale = []
    for d in docs.values():
        h = hashlib.sha256(f"{d.title}\n{d.text}".encode()).hexdigest()
        row = have.get(d.id)
        if row is None or row.text_hash != h or row.model != model:
            stale.append((d, h))
    vectors = embed([f"{d.title} {d.text}" for d, _ in stale]) if stale else []
    new: list[m.SearchDoc] = []
    changed: list[m.SearchDoc] = []
    for (d, h), v in zip(stale, vectors, strict=True):
        doc = m.SearchDoc(id=d.id, kind=d.kind, title=d.title, text=d.text[:2000], url=d.url, text_hash=h,
                          model=model, vector=[round(float(x), 5) for x in v], updated_at=clock.now())
        (changed if d.id in have else new).append(doc)
    store.insert_many(new, actor="system:retrieval", kind="indexed")
    for doc in changed:
        store.update(doc, actor="system:retrieval", kind="reindexed")
    gone = [rid for rid in have if rid not in docs]
    if gone:
        import sqlalchemy as sa

        from gw.record.tables import ENTITY_TABLES

        t = ENTITY_TABLES[m.SearchDoc]
        with store.tx() as conn:
            conn.execute(sa.delete(t).where(t.c.id.in_(gone)))
    return {"indexed": len(new), "reindexed": len(changed), "removed": len(gone), "total": len(docs)}


def search_local(store: Store, query: str, k: int = 10, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """Cosine over the persisted index; falls back to an in-memory TF-IDF pass when nothing is indexed yet."""
    import numpy as np

    rows = [r for r in store.all(m.SearchDoc) if not kinds or r.kind in kinds]
    if rows and query.strip():
        model, embed = _embedder(store)
        rows = [r for r in rows if r.model == model and r.vector]
        if rows:
            q = np.asarray(embed([query])[0], dtype=np.float32)
            mat = np.asarray([r.vector for r in rows], dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0)
            sims = (mat @ q) / np.where(norms == 0, 1.0, norms)
            order = sims.argsort()[::-1][:k]
            return [{"id": rows[i].id, "kind": rows[i].kind, "title": rows[i].title, "snippet": rows[i].text[:200],
                     "url": rows[i].url, "score": round(float(sims[i]), 4), "source": "local",
                     "citation": rows[i].id} for i in order if sims[i] > 0]
    docs = [d for d in documents(store) if not kinds or d.kind in kinds]
    if not docs or not query.strip():
        return []
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
    mat = vec.fit_transform([f"{d.title} {d.text}".lower() for d in docs])
    sims = (mat @ vec.transform([query.lower()]).T).toarray().ravel()
    order = sims.argsort()[::-1][:k]
    return [{"id": docs[i].id, "kind": docs[i].kind, "title": docs[i].title, "snippet": docs[i].text[:200],
             "url": docs[i].url, "score": round(float(sims[i]), 4), "source": "local", "citation": docs[i].id}
            for i in order if sims[i] > 0]


def search(store: Store, registry: Any, query: str, k: int = 10, sources: list[str] | None = None) -> list[dict[str, Any]]:
    """Fan out to the local index and every RetrievalSource; merge by normalised score."""
    groups: list[list[dict[str, Any]]] = []
    if not sources or "local" in sources:
        groups.append(search_local(store, query, k))
    for conn in registry.list("RetrievalSource"):
        if sources and conn.id not in sources:
            continue
        try:
            hits = registry.get(conn.id).search(query, k, {})
        except Exception:
            continue
        groups.append([{"id": h.id, "kind": "external", "title": h.title, "snippet": h.snippet[:300], "url": h.url,
                        "score": h.score, "source": conn.id, "citation": h.url or h.id} for h in hits])
    merged: list[dict[str, Any]] = []
    for g in groups:
        top = max((h["score"] for h in g), default=0) or 1.0
        for h in g:
            merged.append({**h, "score_norm": round(h["score"] / top, 4)})
    merged.sort(key=lambda h: -h["score_norm"])
    return merged[:k]
