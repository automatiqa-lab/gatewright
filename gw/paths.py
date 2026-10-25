"""Locations of shipped data: schemas, example gate models and policies.

From a checkout these are the repo's `schemas/` and `examples/`; from an installed wheel they are
the copies packaged under `gw/_shipped/` (see `[tool.hatch.build.targets.wheel.force-include]`).
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
_SHIPPED = PACKAGE / "_shipped"


def _pick(name: str) -> Path:
    repo = ROOT / name
    return repo if repo.exists() else _SHIPPED / name


SCHEMAS = _pick("schemas")
EXAMPLES = _pick("examples")
GATEMODELS = EXAMPLES / "gatemodels"
POLICIES_DEFAULT = EXAMPLES / "policies.yaml"
STANDARDS_DATA = PACKAGE / "standards" / "data"
