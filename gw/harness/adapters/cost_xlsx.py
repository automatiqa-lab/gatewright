"""xlsx cost register CostSource.with the columns of schemas/cost-csv.md.

Config: path (an .xlsx), sheet (optional; first sheet by default). Rows dedupe on
(project_code, wbs, kind, posted_at, amount), the same key as `csv_drop`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gw.harness.contracts import CostRowIn, Health, SyncResult
from gw.harness.registry import register
from gw.record import models as m

COLUMNS = ["project_code", "wbs", "kind", "amount", "currency", "posted_at", "ref", "note"]
KINDS = {"commitment", "actual", "contingency_draw", "estimate"}


def _date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if hasattr(v, "date"):
        return v.date()  # type: ignore[no-any-return]
    return date.fromisoformat(str(v)[:10])


@register("xlsx_register")
class XlsxRegister:
    def __init__(self, conn: m.Connection) -> None:
        self.path = Path(conn.config.get("path", "./drop/cost.xlsx"))
        self.sheet = conn.config.get("sheet") or 0

    def get_rows(self, since: str | None) -> list[CostRowIn]:
        import pandas as pd

        df = pd.read_excel(self.path, sheet_name=self.sheet)
        missing = [c for c in COLUMNS[:6] if c not in df.columns]
        if missing:
            raise ValueError(f"{self.path.name}: missing columns {missing}")
        out: list[CostRowIn] = []
        seen: set[tuple[Any, ...]] = set()
        for r in df.to_dict("records"):
            if str(r["kind"]) not in KINDS or pd.isna(r["amount"]):
                continue
            posted = _date(r["posted_at"])
            if since and posted.isoformat() <= since:
                continue
            key = (str(r["project_code"]), str(r["wbs"]), str(r["kind"]), posted, float(r["amount"]))
            if key in seen:
                continue
            seen.add(key)
            ref = r.get("ref")
            note = r.get("note")
            out.append(CostRowIn(project_code=key[0], wbs=key[1], kind=key[2], amount=key[4], currency=str(r["currency"]),
                                 posted_at=posted, ref=None if ref is None or pd.isna(ref) else str(ref),
                                 note=None if note is None or pd.isna(note) else str(note)))
        return out

    def sync(self, since: str | None) -> SyncResult:
        rows = self.get_rows(since)
        cur = max((r.posted_at.isoformat() for r in rows), default=since)
        return SyncResult(pulled=len(rows), cursor=cur or datetime.now(UTC).date().isoformat())

    def health(self) -> Health:
        return Health(status="ok" if self.path.exists() else "down", detail=str(self.path))
