"""EU AI Act Article 50 marking for model-generated output (schema `automatiqa-disclosure/1`).

Rules, from the lab's canonical source (wording copied verbatim into data/disclosure.yaml):
- mark only what a model wrote; template text, signals, forecasts and retrieved content stay
  unmarked, and an absent envelope is a truthful statement that nothing was generated;
- marking is idempotent, keyed off `schema`;
- wording is never a literal at the call site;
- the model is never named in visible output (the optional `model` field stays off).

Agents declare model-written payload keys with `AgentOutput.generated`; the stored
`m.AgentOutput` turns that declaration into the envelope, so no construction path skips it.
"""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from gw.record import clock

SCHEMA = "automatiqa-disclosure/1"
SYSTEM = "gatewright"
WORDING = Path(__file__).parent / "data" / "disclosure.yaml"
REVIEW_STATES = ("accepted", "unreviewed", "mixed", "none")


@lru_cache(maxsize=1)
def wording() -> dict[str, Any]:
    """The disclosure strings (loaded once)."""
    with open(WORDING, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{WORDING}: schema {data.get('schema')!r}, expected {SCHEMA}")
    return dict(data)


def envelope(scope: list[str], drafted: int = 1, total: int = 0, review_state: str = "none") -> dict[str, Any] | None:
    """The machine-readable marking, or None when nothing was generated."""
    scope = [s for s in scope if s]
    if drafted <= 0 or not scope:
        return None
    env: dict[str, Any] = {"value": True, "scope": scope, "schema": SCHEMA, "system": SYSTEM,
                           "review_state": review_state if review_state in REVIEW_STATES else "none",
                           "items_drafted": drafted, "ts": clock.now().isoformat(timespec="seconds")}
    if total:
        env["items_total"] = total
    return env


def is_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("schema") == SCHEMA and obj.get("value") is True


def marked(payload: dict[str, Any]) -> bool:
    return is_envelope(payload.get("ai_generated"))


def mark(payload: dict[str, Any], env: dict[str, Any] | None) -> dict[str, Any]:
    """Attach the envelope to a JSON payload; a no-op when nothing was generated or already marked."""
    if env is None or marked(payload):
        return payload
    return {"ai_generated": env, **payload}


def set_review_state(payload: dict[str, Any], state: str) -> dict[str, Any]:
    if not marked(payload):
        return payload
    return {**payload, "ai_generated": {**payload["ai_generated"], "review_state": state}}


def front_matter(env: dict[str, Any] | None) -> str:
    """YAML front matter for Markdown exports; empty when nothing was generated."""
    if not env:
        return ""
    return "\n".join(["---", f"generated_at: {env['ts']}", "ai_generated: true",
                      f"ai_scope: [{', '.join(env['scope'])}]", f"ai_schema: {env['schema']}", "---", ""])


def with_front_matter(markdown: str, env: dict[str, Any] | None) -> str:
    if not env or markdown.startswith("---\n") and f"ai_schema: {SCHEMA}" in markdown.split("\n---", 1)[0]:
        return markdown
    return front_matter(env) + markdown


def html_meta(scopes: list[str]) -> str:
    """Meta tags for the document head; empty when nothing was generated."""
    if not scopes:
        return ""
    scope = html.escape(",".join(sorted(set(scopes))), quote=True)
    return (f'<meta name="ai-generated" content="true"><meta name="ai-scope" content="{scope}">'
            f'<meta name="ai-schema" content="{SCHEMA}">')


def readable_line(env: dict[str, Any] | None) -> str:
    """The human-readable line for a message or document that leaves the system (Art. 50(1))."""
    if not env:
        return ""
    w = wording()["document"]
    drafted = int(env.get("items_drafted", 1))
    total = int(env.get("items_total", drafted))
    if env.get("review_state") == "accepted":
        return str(w["drafted"]).format(drafted=drafted, total=total)
    return str(w["unreviewed"]).format(drafted=drafted, pending=drafted)


def with_readable_line(text: str, env: dict[str, Any] | None) -> str:
    line = readable_line(env)
    if not line or line in text:
        return text
    return f"{text.rstrip()}\n\n{line}\n"


def session_notice() -> str:
    """The Art. 50(1) line for a person in a session with an agent."""
    return str(wording()["interactive"]["session_notice"])


def item_chip() -> str:
    return str(wording()["report"]["item_chip"])


def apply_declaration(payload: dict[str, Any], disposition: str) -> dict[str, Any]:
    """Turn an agent's `_generated` declaration into the envelope (called by m.AgentOutput)."""
    scope = payload.get("_generated")
    if scope is None:
        return payload
    rest = {k: v for k, v in payload.items() if k != "_generated"}
    if marked(rest) or not scope:
        return rest
    env = envelope(list(scope), drafted=len(scope),
                   review_state="unreviewed" if disposition in ("draft", "propose") else "none")
    if env and "markdown" in scope and isinstance(rest.get("markdown"), str):
        rest["markdown"] = with_front_matter(rest["markdown"], env)
    return mark(rest, env)


def find_scopes(obj: Any, depth: int = 0) -> list[str]:
    """Every scope of every envelope inside a JSON-like value (for HTTP headers and HTML meta)."""
    if depth > 8:
        return []
    if is_envelope(obj):
        return list(obj.get("scope") or [])
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += find_scopes(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += find_scopes(v, depth + 1)
    return out
