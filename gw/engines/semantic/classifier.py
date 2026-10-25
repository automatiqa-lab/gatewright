"""Classifier heads for reply intent and document type.step 3 and

Default representation is local and offline: character and word n-gram hashing
(multilingual by construction). When an embedding provider is configured
(Ollama bge-m3, sentence-transformers) its dense vectors are appended. One
logistic regression per head; revisions are promoted on an F1 gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import HashingVectorizer, TfidfTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
from sklearn.model_selection import train_test_split

from gw.record import clock
from gw.record.store import Store


def intent_text(subject: str, text: str) -> str:
    return f"{subject or ''}\n{text or ''}"


def doc_text(name: str, text: str) -> str:
    return f"{name or ''}\n{(text or '')[:2000]}"


class Head:
    """One classification head: hashing n-grams (+ optional dense embeddings) -> logistic regression."""

    def __init__(self, embed: Any = None) -> None:
        self.char = HashingVectorizer(analyzer="char_wb", ngram_range=(2, 5), n_features=2**16, alternate_sign=False)
        self.word = HashingVectorizer(analyzer="word", ngram_range=(1, 2), n_features=2**15, alternate_sign=False,
                                      token_pattern=r"(?u)\b\w+\b")
        self.tfidf = TfidfTransformer(sublinear_tf=True)
        self.clf = LogisticRegression(max_iter=2000, C=8.0, class_weight="balanced")
        self.embed = embed
        self.classes_: list[str] = []

    def _x(self, texts: list[str], fit: bool = False) -> Any:
        low = [t.lower() for t in texts]
        x = sparse.hstack([self.char.transform(low), self.word.transform(low)]).tocsr()
        x = self.tfidf.fit_transform(x) if fit else self.tfidf.transform(x)
        if self.embed is not None:
            dense = np.asarray(self.embed(texts), dtype=np.float32)
            x = sparse.hstack([x, sparse.csr_matrix(dense)]).tocsr()
        return x

    def fit(self, texts: list[str], labels: list[str]) -> Head:
        self.clf.fit(self._x(texts, fit=True), labels)
        self.classes_ = [str(c) for c in self.clf.classes_]
        return self

    def predict_proba(self, texts: list[str]) -> list[dict[str, float]]:
        p = self.clf.predict_proba(self._x(texts))
        return [{c: float(v) for c, v in zip(self.classes_, row, strict=True)} for row in p]

    def predict(self, texts: list[str]) -> list[tuple[str, float]]:
        return [max(d.items(), key=lambda kv: kv[1]) for d in self.predict_proba(texts)]

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["embed"] = None  # providers are re-attached at load
        return state


@dataclass
class Heads:
    intent: Head
    doc_type: Head
    rev: int = 1
    metrics: dict[str, Any] = field(default_factory=dict)


def load_corpus(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def training_sets(corpus: list[dict[str, Any]], examples: list[Any]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    intents = [(intent_text(x.get("subject", ""), x["text"]), x["intent"]) for x in corpus if x["kind"] == "message"]
    docs = [(doc_text(x.get("name", ""), x.get("text", "")), x["doc_type"]) for x in corpus if x["kind"] == "document"]
    for e in examples:
        (intents if e.head == "intent" else docs).append((e.text, e.label))
    return intents, docs


def _fit_eval(pairs: list[tuple[str, str]], seed: int, embed: Any) -> tuple[Head, float, Head]:
    texts = [t for t, _ in pairs]
    labels = [lab for _, lab in pairs]
    xtr, xte, ytr, yte = train_test_split(texts, labels, test_size=0.2, random_state=seed, stratify=labels)
    head = Head(embed).fit(xtr, ytr)
    f1 = float(f1_score(yte, [lab for lab, _ in head.predict(xte)], average="macro"))
    full = Head(embed).fit(texts, labels)
    return head, f1, full


def models_dir(store: Store) -> Path:
    base = store.get_meta("data_dir")
    d = Path(base) if base else Path("gw-models")
    d = d / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def revisions(store: Store) -> list[dict[str, Any]]:
    raw = store.get_meta("semantic_revs")
    return list(json.loads(raw)) if raw else []


def train(store: Store, corpus_path: Path, embed: Any = None, seed: int = 7, max_drop: float = 0.02) -> dict[str, Any]:
    """Fit both heads, record held-out F1, promote unless F1 drops by more than `max_drop`."""
    from gw.record import models as m

    corpus = load_corpus(corpus_path)
    examples = store.all(m.SemanticExample)
    intents, docs = training_sets(corpus, examples)
    if not intents or not docs:
        raise ValueError("no training data: run `gw demo` or provide a corpus")
    _, f1_i, intent = _fit_eval(intents, seed, embed)
    _, f1_d, doc = _fit_eval(docs, seed, embed)
    revs = revisions(store)
    rev = (revs[-1]["rev"] + 1) if revs else 1
    current = next((r for r in reversed(revs) if r.get("promoted")), None)
    promoted = current is None or (f1_i >= current["f1_intent"] - max_drop and f1_d >= current["f1_doc_type"] - max_drop)
    path = models_dir(store) / f"semantic-rev{rev}.joblib"
    joblib.dump(Heads(intent, doc, rev, {"f1_intent": f1_i, "f1_doc_type": f1_d}), path)
    entry = {"rev": rev, "f1_intent": round(f1_i, 4), "f1_doc_type": round(f1_d, 4), "n_intent": len(intents),
             "n_doc": len(docs), "examples": len(examples), "promoted": promoted, "path": path.as_posix(),
             "at": clock.now().isoformat()}
    revs.append(entry)
    store.set_meta("semantic_revs", json.dumps(revs))
    store.log("system:semantic", "Classifier", f"semantic-rev{rev}", "trained", entry)
    return entry


def load_current(store: Store, embed: Any = None) -> Heads | None:
    revs = [r for r in revisions(store) if r.get("promoted")]
    if not revs:
        return None
    heads: Heads = joblib.load(revs[-1]["path"])
    heads.intent.embed = embed
    heads.doc_type.embed = embed
    return heads
