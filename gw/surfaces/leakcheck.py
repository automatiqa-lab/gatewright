"""Leak guard for exporting a copy of the code. Two kinds of finding:
- credentials (tokens, private keys, client secrets, API keys) by pattern;
- identifiers of configured live connections (tenant ids, client ids, mailbox addresses, hosts),
  read from the record's connections table and from connections.local.yaml - the patterns are
  the values themselves, so they never have to be written into the repo.
Plus files that must never leave the machine (databases, env files, token caches, demo data) and,
when a terms file is given, any topic the owner keeps private (the list lives in that file only).

`export()` copies an allow-list manifest into a fresh directory and refuses on any finding.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

CREDENTIALS = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "azure_client_secret": re.compile(r"[A-Za-z0-9_~.-]{3}\dQ~[A-Za-z0-9_~.-]{30,}"),
    "refresh_token": re.compile(r"refresh_token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "openai_key": re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    "github_token": re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
}
FORBIDDEN_FILES = re.compile(r"(\.duckdb(\.wal)?$|(^|/)\.env(\..*)?$|connections\.local\.ya?ml$|msal_token_cache|"
                             r"(^|/)demo-data/|(^|/)gw-models/|\.joblib$)")
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
HOST = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/.*)?$", re.I)
SAFE = ("example.com", "example.org", "localhost", "microsoft.com", "microsoftonline.com", "graph.microsoft.com")


@dataclass
class Hit:
    path: str
    line: int
    rule: str


def _safe_host(value: str) -> bool:
    """True only when the host (or e-mail domain) IS a safe domain or a subdomain of one."""
    host = value.lower().split("@")[-1].split("://")[-1].split("/")[0].split(":")[0]
    return any(host == s or host.endswith("." + s) for s in SAFE) and not host.endswith(".onmicrosoft.com")


def _identifying(value: str) -> bool:
    v = value.strip()
    if not v or v.startswith(("$", "<")) or _safe_host(v):
        return False
    return bool(GUID.match(v) or EMAIL.match(v) or (HOST.match(v) and "." in v))


def identifiers(db: str | None = None, local_yaml: Path = Path("connections.local.yaml")) -> set[str]:
    """Identifying values of live connections, from the record and the local YAML (never from the repo)."""
    vals: set[str] = set()
    configs: list[dict[str, object]] = []
    if local_yaml.exists():
        data = yaml.safe_load(local_yaml.read_text(encoding="utf-8")) or {}
        configs += [c.get("config") or {} for c in data.get("connections", [])]
    if db and ("://" in db or Path(db).exists()):
        try:
            from gw.record import models as m
            from gw.record.store import Store

            st = Store(db if "://" in db else "duckdb:///" + Path(db).as_posix())
            configs += [c.config for c in st.all(m.Connection) if not c.adapter.startswith("fake_")]
            st.close()
        except Exception as e:  # a locked or unreadable record must stop the export, not skip the check
            raise RuntimeError(f"leak check cannot read {db} ({type(e).__name__}); stop `gw serve` and retry") from e
    for cfg in configs:
        for v in cfg.values():
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, str) and _identifying(item):
                    vals.add(item.strip())
    return vals


def _files(roots: list[Path]) -> list[Path]:
    out = []
    for root in roots:
        if root.is_file():
            out.append(root)
            continue
        for p in root.rglob("*"):
            if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS):
                out.append(p)
    return out


def load_terms(path: Path | None) -> re.Pattern[str] | None:
    """Private topic terms, one per line ('#' comments), from a file kept outside the exported tree."""
    if path is None or not path.exists():
        return None
    terms = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return re.compile("(" + "|".join(re.escape(t) for t in terms) + ")", re.I) if terms else None


def scan(roots: list[Path], db: str | None = "gw.duckdb", private_terms: re.Pattern[str] | None = None,
         extra_identifiers: set[str] | None = None) -> list[Hit]:
    """Findings in the given files or directories."""
    ids = identifiers(db) | (extra_identifiers or set())
    id_res = [(v, re.compile(re.escape(v), re.I)) for v in ids]
    hits: list[Hit] = []
    for f in _files(roots):
        rel = f.as_posix()
        if FORBIDDEN_FILES.search(rel):
            hits.append(Hit(rel, 0, "forbidden file"))
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in CREDENTIALS.items():
                if rx.search(line):
                    hits.append(Hit(rel, n, f"credential: {name}"))
            for v, rx in id_res:
                if rx.search(line):
                    hits.append(Hit(rel, n, f"identifier of a live connection ({v[:3]}...)"))
            if private_terms is not None and private_terms.search(line):
                hits.append(Hit(rel, n, "private topic"))
    return hits


def read_manifest(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def export(src: Path, dest: Path, manifest: Path, db: str | None = "gw.duckdb", terms: Path | None = None) -> list[Hit]:
    """Copy the allow-listed paths into a fresh `dest`; on any finding, delete `dest` and return the hits."""
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} is not empty; export into a fresh directory")
    dest.mkdir(parents=True, exist_ok=True)
    for entry in read_manifest(manifest):
        exclude = entry.startswith("!")
        rel = entry.lstrip("!")
        target = dest / rel
        if exclude:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            continue
        s = src / rel
        if s.is_dir():
            shutil.copytree(s, target, ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"))
        elif s.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, target)
    hits = scan([dest], db, private_terms=load_terms(terms))
    if hits:
        shutil.rmtree(dest)
    return hits
