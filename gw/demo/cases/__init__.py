"""Scripted cases played over the history.

Each case module has `prepare(g)` (scripts current-gate states before they are
generated) and `apply(g)` (injects events, inbound items, risks and decisions after).
"""

from __future__ import annotations

from types import ModuleType

from gw.demo.cases import base, s1, s2, s3, s4, s5, s6, s7, s8, s9

CASES: dict[str, ModuleType] = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6, "S7": s7, "S8": s8, "S9": s9}
BASE = base
