"""Prefixed ULID ids.

The demo generator switches to a seeded source so a seed reproduces the same ids.
"""

from __future__ import annotations

import random

from ulid import ULID

from gw.record import clock

PREFIX = {
    "Project": "P-", "GateInstance": "G-", "WorkPackage": "WP-", "Commitment": "C-",
    "Party": "PT-", "Evidence": "E-", "Risk": "R-", "Decision": "D-", "Signal": "S-",
    "AgentRun": "AR-", "User": "U-", "Dependency": "DEP-", "Approval": "AP-",
    "AgentOutput": "AO-", "Lesson": "L-", "InboundItem": "IN-", "Connection": "CN-",
}

_rng: random.Random | None = None


def seed(value: int | None) -> None:
    """Make id generation deterministic (demo) or random again (None)."""
    global _rng
    _rng = random.Random(value) if value is not None else None


def new_id(kind: str) -> str:
    prefix = PREFIX.get(kind, kind[:2].upper() + "-")
    if _rng is None:
        return prefix + str(ULID())
    ms = int(clock.now().timestamp() * 1000)
    return prefix + str(ULID.from_bytes(ms.to_bytes(6, "big") + _rng.randbytes(10)))
