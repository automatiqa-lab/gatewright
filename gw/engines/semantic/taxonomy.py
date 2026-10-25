"""The semantic taxonomy as data, with revisions.

The shipped file `standards/data/taxonomy.yaml` is revision 0. Edits from GW-310 are validated
and stored as revisions in the record (meta key `taxonomy_revs`) with author and diff; the
pipeline reads the current revision on every run.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

import yaml

from gw import paths
from gw.errors import Unprocessable
from gw.record import clock
from gw.record.store import Store

REQUIRED_INTENTS = {"CONFIRM", "FLAG", "OOO", "UNRELATED"}  # produced by the rules and the reply block
REQUIRED_DOC_TYPES = {"other"}


def shipped_text() -> str:
    return (paths.STANDARDS_DATA / "taxonomy.yaml").read_text(encoding="utf-8")


def revisions(store: Store) -> list[dict[str, Any]]:
    return list(json.loads(store.get_meta("taxonomy_revs") or "[]"))


def current_text(store: Store) -> str:
    revs = revisions(store)
    return str(revs[-1]["yaml"]) if revs else shipped_text()


def current(store: Store | None = None) -> dict[str, Any]:
    """Current taxonomy as a mapping (the shipped file when there is no store or no revision)."""
    data = yaml.safe_load(current_text(store) if store is not None else shipped_text()) or {}
    return dict(data)


def validate(text: str) -> dict[str, Any]:
    """Raise Unprocessable with a JSON pointer on the first problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise Unprocessable(f"YAML error: {e}", "") from e
    if not isinstance(data, dict):
        raise Unprocessable("taxonomy must be a mapping", "")
    intents = data.get("reply_intents")
    docs = data.get("document_types")
    if not isinstance(intents, list) or not intents:
        raise Unprocessable("reply_intents must be a non-empty list", "/reply_intents")
    if not isinstance(docs, list) or not docs:
        raise Unprocessable("document_types must be a non-empty list", "/document_types")
    for i, x in enumerate(intents):
        if not isinstance(x, str) or not x.isupper() or not x.replace("_", "").isalpha():
            raise Unprocessable(f"intent {x!r} must be an upper-case word", f"/reply_intents/{i}")
    for i, x in enumerate(docs):
        if not isinstance(x, str) or x != x.lower():
            raise Unprocessable(f"document type {x!r} must be lower case", f"/document_types/{i}")
    if len(set(intents)) != len(intents):
        raise Unprocessable("reply_intents must be unique", "/reply_intents")
    if len(set(docs)) != len(docs):
        raise Unprocessable("document_types must be unique", "/document_types")
    missing = REQUIRED_INTENTS - set(intents)
    if missing:
        raise Unprocessable(f"reply_intents must keep {sorted(missing)} (rules and the reply block emit them)",
                            "/reply_intents")
    if REQUIRED_DOC_TYPES - set(docs):
        raise Unprocessable("document_types must keep 'other'", "/document_types")
    for key in ("ignored_document_types", "extract_for_doc_types"):
        bad = [x for x in data.get(key) or [] if x not in docs]
        if bad:
            raise Unprocessable(f"{key} names unknown document types {bad}", f"/{key}")
    bad = [x for x in data.get("extract_for_intents") or [] if x not in intents]
    if bad:
        raise Unprocessable(f"extract_for_intents names unknown intents {bad}", "/extract_for_intents")
    return dict(data)


def save(store: Store, text: str, author: str, note: str = "") -> dict[str, Any]:
    """Validate and store a new revision with its diff; one event."""
    validate(text)
    revs = revisions(store)
    prev = current_text(store)
    rev = (revs[-1]["rev"] + 1) if revs else 1
    diff = "".join(difflib.unified_diff(prev.splitlines(True), text.splitlines(True), f"rev {rev - 1}", f"rev {rev}"))
    entry = {"rev": rev, "yaml": text, "author": author, "note": note, "diff": diff, "at": clock.now().isoformat()}
    revs.append(entry)
    store.set_meta("taxonomy_revs", json.dumps(revs))
    store.log(author if ":" in author else f"user:{author}", "Taxonomy", f"rev{rev}", "revised", {"note": note})
    return entry
