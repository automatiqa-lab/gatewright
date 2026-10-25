"""Deterministic first stages of the semantic pipeline.steps 1-2.

gw-ref resolution, structured reply lines, OOO auto-replies, file-name patterns,
and regex extraction of dates, reasons and delegates. Rules cap at 0.95.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

RULE_CAP = 0.95
REF = re.compile(r"\b(C-\d{4})(?:/(\d+))?\b")
STRUCT_CONFIRM = re.compile(r"^\s*confirm\b(?!\w)", re.I)
STRUCT_FLAG = re.compile(r"^\s*flag\s*[-:–]\s*(\d{4}-\d{2}-\d{2})\s*(.*)$", re.I | re.S)
OOO = re.compile(r"(out of (the )?office|automatic reply|auto-?reply|on leave|afwezig|fuera de la oficina|"
                 r"absent jusqu|fora do escrit)", re.I)
FILE_PATTERN = re.compile(r"^(?P<project>[A-Z]{2,4}-\d{2})_(?P<deliverable>[a-z0-9_]+?)_rev(?P<rev>[A-Z0-9]+)\.\w+$")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS.update({"january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8,
               "september": 9, "october": 10, "november": 11, "december": 12})
DATE_RES = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
                r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+(\d{4}))?\b", re.I), "dmon"),
]
REASON = re.compile(r"(?:reason|motivo|raison|reden|because of|because|due to|vanwege|por|en raison de)\s*[:\-]?\s*([^.\n]+)", re.I)
DELEGATE = re.compile(r"(?:contact|contacte a|contacter|contate|over to|handed .*? to|neem .*? contact op met)\s+"
                      r"([A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+)+)", re.S)
DELEGATE2 = re.compile(r"^([A-Z][a-zà-ÿ]+\s+[A-Z][a-zà-ÿ]+) is responsible", re.M)
AMOUNT = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+)\s?(EUR|USD|€|\$)", re.I)


@dataclass
class RuleResult:
    intent: str | None = None
    confidence: float = 0.0
    ref: str | None = None
    entities: dict[str, Any] = field(default_factory=dict)
    structured: bool = False


def find_ref(*texts: str | None) -> tuple[str | None, int | None]:
    """The gw-ref token (`C-1187/3`) from headers, subject or footer."""
    for t in texts:
        if not t:
            continue
        mt = REF.search(t)
        if mt:
            return mt.group(1), int(mt.group(2)) if mt.group(2) else None
    return None, None


def parse_dates(text: str, today: date | None = None) -> list[date]:
    today = today or date.today()
    out: list[date] = []
    for rx, kind in DATE_RES:
        for mt in rx.finditer(text):
            try:
                if kind == "iso":
                    d = date(int(mt.group(1)), int(mt.group(2)), int(mt.group(3)))
                elif kind == "dmy":
                    d = date(int(mt.group(3)), int(mt.group(2)), int(mt.group(1)))
                else:
                    mon = MONTHS[mt.group(2).lower()[:3]] if mt.group(2).lower()[:3] in MONTHS else MONTHS[mt.group(2).lower()]
                    year = int(mt.group(3)) if mt.group(3) else today.year
                    d = date(year, mon, int(mt.group(1)))
                    if not mt.group(3) and d < today - timedelta(days=60):
                        d = date(year + 1, mon, int(mt.group(1)))
                out.append(d)
            except (ValueError, KeyError):
                continue
    return out


def extract(text: str, today: date | None = None) -> dict[str, Any]:
    """Regex extraction of the entity list; values are validated later against the record."""
    ents: dict[str, Any] = {}
    dates = parse_dates(text, today)
    if dates:
        ents["date"] = [d.isoformat() for d in dates]
        ents["new_due_date"] = max(dates).isoformat()
    mt = REASON.search(text)
    if mt:
        ents["reason"] = mt.group(1).strip()[:200]
    mt = DELEGATE.search(text) or DELEGATE2.search(text)
    if mt:
        ents["person"] = mt.group(1).strip()
    mt = AMOUNT.search(text)
    if mt:
        ents["amount"] = mt.group(1)
        ents["currency"] = {"€": "EUR", "$": "USD"}.get(mt.group(2), mt.group(2).upper())
    ref, _ = find_ref(text)
    if ref:
        ents["commitment_ref"] = ref
    return ents


def apply_rules(subject: str, text: str, today: date | None = None) -> RuleResult:
    """Structured reply lines and auto-replies ( step 2)."""
    body = (text or "").strip()
    first = body.splitlines()[0] if body else ""
    mt = STRUCT_FLAG.match(first)
    if mt:
        ents = {"new_due_date": mt.group(1), "reason": mt.group(2).strip() or None}
        return RuleResult("FLAG", RULE_CAP, entities=ents, structured=True)
    if STRUCT_CONFIRM.match(first) and len(first) <= 30:
        return RuleResult("CONFIRM", RULE_CAP, structured=True)
    if OOO.search(subject or "") or OOO.search(body[:200]):
        return RuleResult("OOO", RULE_CAP)
    return RuleResult()


def parse_filename(name: str) -> dict[str, str] | None:
    """`<project>_<deliverable>_rev<X>` naming pattern ( step 2)."""
    mt = FILE_PATTERN.match(name or "")
    return mt.groupdict() if mt else None


def valid_new_due(value: str | None, today: date) -> date | None:
    """A new due date must parse and lie within 400 days."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None
    return d if today - timedelta(days=30) <= d <= today + timedelta(days=400) else None
