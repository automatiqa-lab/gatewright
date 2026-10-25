"""Model drafting for agent text.

Agents keep producing their text from templates over the record; that template is the source of
truth. When `policies.models.drafting.connection` names a chat connection and the agent is listed,
the template is handed to that model for wording only, and the result is accepted only if it keeps
every fact: required lines verbatim, no code, number, date, address or link that the template and
facts do not contain, no leftover redaction placeholder, a sane length and a clean stop. Anything
else (no connection, provider down, budget spent, refusal, failed check) falls back to the template,
and the payload records why. Accepted text is declared model-written, so it carries the Art. 50
envelope.

The engine never names a provider or a model: the connection's adapter does.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gw.harness.contracts import ChatMessage, ChatRequest

PROMPT = (Path(__file__).resolve().parents[1] / "agents" / "prompts" / "drafting.md").read_text(encoding="utf-8")
PLACEHOLDER = re.compile(r"<[A-Z]+_\d+>")
SCAFFOLD = ("purpose:", "facts from the record", "keep verbatim", "<draft>", "</draft>")
FACT_TOKENS = [
    re.compile(r"\b[A-Z]{1,5}-\d{2,5}(?:/\d+)?\b"),  # commitment, project and lesson codes, refs
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail addresses
    re.compile(r"https?://\S+"),  # links
    re.compile(r"\b\d+(?:[.,:]\d+)*\s?%?"),  # numbers, dates, times, percentages
]
CODE = FACT_TOKENS[0]
NUMBER = re.compile(r"(?<![\w/-])\d[\d,.:]*(?:\s?%)?")
MONTH = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                   r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", re.I)
WEEKDAY = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b", re.I)
NEGATION = re.compile(r"\b(?:not|no|never|without|none|nor|cannot)\b|n't\b", re.I)
NAME = re.compile(r"(?<![.!?:\n]\s)(?<!^)\b[A-Z][a-z]{2,}\b", re.M)


def _number(tok: str) -> str:
    """'02' == '2', '1,200' == '1200', '45 %' == '45%'."""
    t = tok.replace(" ", "").rstrip(".,:")
    pct = t.endswith("%")
    t = t.rstrip("%")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", t):
        t = t.replace(",", "")
    if re.fullmatch(r"\d+", t):
        t = str(int(t))
    return t + ("%" if pct else "")


def facts_of(text: str) -> Counter[str]:
    """The countable facts of a text: codes, addresses, links, numbers, months and weekdays."""
    found: Counter[str] = Counter()
    for code in CODE.findall(text):
        found["code:" + code] += 1
    for pat, kind in ((FACT_TOKENS[1], "email"), (FACT_TOKENS[2], "url")):
        for tok in pat.findall(text):
            found[f"{kind}:{tok.lower().rstrip('.,;')}"] += 1
    without_codes = CODE.sub(" ", FACT_TOKENS[2].sub(" ", FACT_TOKENS[1].sub(" ", text)))
    for tok in NUMBER.findall(without_codes):
        if _number(tok):
            found["num:" + _number(tok)] += 1
    for mt in MONTH.finditer(text):
        found["month:" + mt.group(1)[:3].lower()] += 1
    for mt in WEEKDAY.finditer(text):
        found["day:" + mt.group(1)[:3].lower()] += 1
    return found


@dataclass
class Draft:
    text: str
    generated: bool
    reason: str
    tokens: int = 0
    checks: list[str] = field(default_factory=list)

    def audit(self) -> dict[str, Any]:
        return {"model_written": self.generated, "reason": self.reason, "tokens": self.tokens}


def check(candidate: str, template: str, facts: str, keep: list[str]) -> list[str]:
    """Every reason the candidate may not replace the template (empty list = accepted).

    Facts are compared as counts, not as substrings: nothing the template states may be dropped,
    nothing beyond the template and the facts may be added, codes keep their order, the number of
    negations is unchanged, and no new name appears."""
    problems: list[str] = []
    text = candidate.strip()
    if not text:
        return ["empty"]
    if not 0.35 * len(template) <= len(text) <= 1.8 * len(template) + 200:
        problems.append(f"length {len(text)} vs template {len(template)}")
    for line in keep:
        if line and line not in text:
            problems.append(f"missing kept line: {line[:40]}")
    if PLACEHOLDER.search(text):
        problems.append("unrestored placeholder")
    low = text.lower()
    echoed = [x for x in SCAFFOLD if x in low and x not in template.lower()]
    if echoed:
        problems.append(f"prompt echoed: {echoed[0]}")
    tmpl, cand = facts_of(template), facts_of(text)
    allowed = tmpl + facts_of(facts)
    for tok in sorted((tmpl - cand).keys()):
        problems.append(f"dropped fact: {tok.split(':', 1)[1][:30]}")
    for tok in sorted((cand - allowed).keys()):
        problems.append(f"new fact: {tok.split(':', 1)[1][:30]}")
    tmpl_codes = CODE.findall(template)
    cand_codes = [c for c in CODE.findall(text) if c in tmpl_codes]
    if len(cand_codes) == len(tmpl_codes) and cand_codes != tmpl_codes:
        problems.append("codes reordered")
    if len(NEGATION.findall(text)) != len(NEGATION.findall(template)):
        problems.append("negation changed")
    known = (template + " " + facts).lower()
    for name in sorted({n for n in NAME.findall(text) if n.lower() not in known}):
        problems.append(f"new name: {name}")
    return problems


class Drafter:
    """One per agent run. `rewrite` never raises; it returns the template on any failure."""

    def __init__(self, store: Any, policies: Any, agent: str) -> None:
        self.agent = agent
        cfg = dict(policies.get("models.drafting", {}) or {})
        self.conn_id = cfg.get("connection")
        self.enabled = bool(self.conn_id) and agent in (cfg.get("agents") or [])
        self.max_tokens = int(cfg.get("max_tokens", 900))
        self.effort = cfg.get("effort", "low")
        self.cfg = cfg
        self.tokens = 0
        # stop calling the model before the run's own time budget is at risk; templates cover the rest
        self.deadline = time.monotonic() + 0.7 * float(policies.get("agents.budgets.seconds", 120))
        self._session: Any = None
        self._unavailable: str | None = None
        if self.enabled:
            budget = int(policies.get(f"agents.budgets.tokens.{agent}", 0) or 0)
            if budget <= 0:  # a zero budget means this agent may not use a model at all
                self.enabled, self._unavailable = False, "no token budget"
                return
            try:
                from gw.engines.models import chat_gateway

                self._session = chat_gateway(store, str(self.conn_id)).session(budget_tokens=budget)
            except Exception as e:  # unknown connection, not a chat model: templates it is
                self._unavailable = f"{type(e).__name__}: {str(e)[:120]}"

    def rewrite(self, template: str, facts: dict[str, Any] | None = None, keep: list[str] | None = None,
                purpose: str = "message") -> Draft:
        if not self.enabled:
            return Draft(template, False, "drafting off")
        if self._session is None:
            return Draft(template, False, f"model unavailable ({self._unavailable})")
        if time.monotonic() > self.deadline:
            return Draft(template, False, "time budget spent")
        facts_text = json.dumps(facts or {}, default=str, ensure_ascii=False)
        keep = [k for k in (keep or []) if k]
        user = (f"Purpose: {purpose}\nFacts from the record (JSON): {facts_text}\n"
                + ("Keep verbatim:\n" + "\n".join(f"- {k}" for k in keep) + "\n" if keep else "")
                + f"<draft>\n{template}\n</draft>")
        try:
            res = self._session.chat(ChatRequest(system=PROMPT, messages=[ChatMessage(role="user", content=user)],
                                                 max_tokens=self.max_tokens, effort=self.effort))
        except Exception as e:  # provider down, budget spent, hard limit refused
            return Draft(template, False, f"{type(e).__name__}: {str(e)[:120]}")
        spent = res.usage.input_tokens + res.usage.output_tokens
        self.tokens += spent
        if res.stop != "end":
            return Draft(template, False, f"model stopped: {res.stop}", spent)
        candidate = res.text.strip().replace("—", " - ").replace("–", "-")
        wrapped = re.search(r"<draft>\s*(.*?)\s*</draft>", candidate, re.S)
        if wrapped:  # some models answer inside the tags they were given
            candidate = wrapped.group(1).strip()
        problems = check(candidate, template, facts_text, keep)
        if problems:
            return Draft(template, False, "rejected: " + "; ".join(problems[:4]), spent, problems)
        return Draft(candidate, True, "accepted", spent)
