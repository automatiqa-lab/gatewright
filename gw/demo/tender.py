"""Synthetic tender room: package specifications and bids for packages in a tender state.

Feeds the tender agent through a DocumentSource that the inbox does not
ingest (`config.role: tender`). Vendors and figures are invented.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import yaml

from gw import paths
from gw.record import models as m

OTHER_BIDDERS = {"asrs": ["Liftrax AG", "Cubestore Systems"], "racking": ["Stellar Racking", "Nordrack AB"]}


def build(packages: list[m.WorkPackage], parties: dict[str, m.Party], seed: int, now_iso: str) -> dict[str, Any]:
    """{"docs": {key: {name, text, modified}}} for every package in a tender state."""
    rng = random.Random(f"tender:{seed}")
    tmpl = yaml.safe_load((paths.STANDARDS_DATA / "package_templates.yaml").read_text(encoding="utf-8")) or {}
    docs: dict[str, dict[str, str]] = {}
    for wp in packages:
        if wp.tender_state == "none":
            continue
        sections = list(tmpl.get(wp.discipline) or tmpl["default"])
        drop = set(rng.sample(sections, k=2))  # two sections missing, as on the GW-320 mock-up
        spec = [f"{wp.code} {wp.name} - package specification (synthetic)", ""]
        for i, s in enumerate([x for x in sections if x not in drop], 1):
            spec += [f"{i}. {s.capitalize()}", f"Requirements for {s} of the {wp.name.lower()} package.", ""]
        docs[f"{wp.code}/spec"] = {"name": f"{wp.code}_package_spec.txt", "text": "\n".join(spec), "modified": now_iso}
        incumbent = parties.get(wp.party_id or "")
        bidders = ([incumbent.name] if incumbent else []) + OTHER_BIDDERS.get(wp.discipline, ["Bidder A", "Bidder B"])
        for b in bidders:
            lines = [f"Bid by {b} for {wp.code} (synthetic)", ""]
            for s in sections:
                lines.append(f"{s}: {rng.choices(['compliant', 'partial', 'non-compliant'], [70, 20, 10])[0]}")
            lines += ["", f"price: {rng.randint(18, 30) * 100_000} EUR"]
            slug = b.replace(" ", "-")
            docs[f"{wp.code}/bid/{slug}"] = {"name": f"{wp.code}_bid_{slug}.txt", "text": "\n".join(lines),
                                             "modified": now_iso}
    return {"docs": docs}


def write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
